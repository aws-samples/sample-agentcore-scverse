"""
Compute routing and dispatch for the scverse Gateway.

Determines the correct compute backend (Lambda, Fargate, Step Functions)
based on tool name, file size, and workflow type, then dispatches the
request to the selected backend.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

try:
    from job_utils import (
        get_file_size,
        build_manifest_key,
        SIZE_THRESHOLD_SYNC,
        SIZE_THRESHOLD_FARGATE,
        STORAGE_BUCKET,
    )
except ImportError:
    from gateways.scverse.function.job_utils import (
        get_file_size,
        build_manifest_key,
        SIZE_THRESHOLD_SYNC,
        SIZE_THRESHOLD_FARGATE,
        STORAGE_BUCKET,
    )

# ---------------------------------------------------------------------------
# AWS clients (initialised at module level for Lambda reuse)
# ---------------------------------------------------------------------------
s3_client = boto3.client("s3")
lambda_client = boto3.client("lambda")
ecs_client = boto3.client("ecs")
sfn_client = boto3.client("stepfunctions")

# ---------------------------------------------------------------------------
# Workflow types that always route to Fargate regardless of file size
# ---------------------------------------------------------------------------
HEAVY_WORKFLOW_TYPES = {
    "integration",
    "clustering",
    "differential_expression",
    "spatial",
    "annotation",
    "perturbation",
    "velocity",
}

# ---------------------------------------------------------------------------
# Fargate task resource configs by workflow type
# ---------------------------------------------------------------------------
TASK_CONFIGS = {
    "qc": {"cpu": "4096", "memory": "16384"},
    "preprocessing": {"cpu": "4096", "memory": "16384"},
    "clustering": {"cpu": "4096", "memory": "16384"},
    "differential_expression": {"cpu": "4096", "memory": "16384"},
    "annotation": {"cpu": "4096", "memory": "16384"},
    "perturbation": {"cpu": "4096", "memory": "16384"},
    "integration": {"cpu": "8192", "memory": "30720"},
    "spatial": {"cpu": "8192", "memory": "30720"},
    "velocity": {"cpu": "8192", "memory": "30720"},
    "full_pipeline": {"cpu": "8192", "memory": "30720"},
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class ComputeLambdaThrottled(Exception):
    """Raised when the Compute Lambda is throttled or times out."""
    pass


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------
def determine_compute_backend(
    tool_name: str,
    input_s3_key: str,
    workflow_type: str = None,
) -> tuple[str, str]:
    """Determine the compute backend and execution mode for a request.

    Returns:
        A ``(backend, mode)`` tuple where *backend* is one of
        ``"LAMBDA"``, ``"FARGATE"``, or ``"STEP_FUNCTIONS"`` and *mode*
        is ``"sync"`` or ``"async"``.
    """
    # scvi-tools always goes to Fargate (Req 1.11)
    if tool_name == "run_scvi_analysis":
        return ("FARGATE", "async")

    # Full pipeline always goes to Step Functions
    if tool_name == "submit_scverse_job" and workflow_type == "full_pipeline":
        return ("STEP_FUNCTIONS", "async")

    # Heavy workflow types always go to Fargate
    if tool_name == "submit_scverse_job" and workflow_type in HEAVY_WORKFLOW_TYPES:
        return ("FARGATE", "async")

    # Size-based routing for QC and remaining submit_scverse_job workflows
    if tool_name in ("run_scrna_qc", "submit_scverse_job"):
        file_size = get_file_size(STORAGE_BUCKET, input_s3_key)
        if file_size < SIZE_THRESHOLD_SYNC:
            return ("LAMBDA", "sync")
        if file_size < SIZE_THRESHOLD_FARGATE:
            return ("LAMBDA", "async")
        return ("FARGATE", "async")

    # Default fallback
    return ("FARGATE", "async")


# ---------------------------------------------------------------------------
# Dispatch functions
# ---------------------------------------------------------------------------
def dispatch_to_compute_lambda(event: dict) -> dict:
    """Invoke the Compute Lambda synchronously and return its response.

    Raises:
        ComputeLambdaThrottled: If the invocation is throttled or times out.
    """
    try:
        response = lambda_client.invoke(
            FunctionName=os.environ["COMPUTE_LAMBDA_ARN"],
            InvocationType="RequestResponse",
            Payload=json.dumps(event),
        )

        # Check for function errors (timeout, OOM, etc.)
        if response.get("FunctionError"):
            # Don't surface the raw Lambda error payload — it can contain
            # user-submitted data. Raise a generic signal instead.
            raise ComputeLambdaThrottled("Compute Lambda returned an error response")

        payload = json.loads(response["Payload"].read().decode("utf-8"))
        return payload

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("TooManyRequestsException", "ServiceException"):
            raise ComputeLambdaThrottled(
                f"Compute Lambda throttled: {error_code}"
            ) from exc
        raise


def dispatch_to_fargate(
    job_id: str,
    workflow_type: str,
    input_s3_key: str,
    params: dict,
    notification_email: str = None,
) -> None:
    """Write a job manifest to S3 and launch an ECS Fargate task."""
    manifest = {
        "job_id": job_id,
        "workflow_type": workflow_type,
        "input_s3_key": input_s3_key,
        "storage_bucket": STORAGE_BUCKET,
        "parameters": params,
        "output_prefix": f"scverse-jobs/{job_id}/outputs/",
    }
    if notification_email:
        manifest["notification_email"] = notification_email

    # Write manifest to S3
    manifest_key = build_manifest_key(job_id)
    s3_client.put_object(
        Bucket=STORAGE_BUCKET,
        Key=manifest_key,
        Body=json.dumps(manifest),
    )

    # Select resource overrides based on workflow type
    task_config = TASK_CONFIGS.get(workflow_type, {"cpu": "4096", "memory": "16384"})

    ecs_client.run_task(
        cluster=os.environ["ECS_CLUSTER_ARN"],
        taskDefinition=os.environ["ECS_TASK_DEFINITION_ARN"],
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": json.loads(os.environ["PRIVATE_SUBNET_IDS"]),
                "securityGroups": [os.environ["SECURITY_GROUP_ID"]],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={
            "cpu": task_config["cpu"],
            "memory": task_config["memory"],
            "containerOverrides": [
                {
                    "name": "scverse-worker",
                    "environment": [
                        {"name": "JOB_MANIFEST_S3_KEY", "value": manifest_key},
                        {"name": "STORAGE_BUCKET", "value": STORAGE_BUCKET},
                        {"name": "JOB_TABLE_NAME", "value": os.environ.get("JOB_TABLE_NAME", "scverse-job-table")},
                    ],
                }
            ],
        },
    )


def dispatch_to_step_functions(
    job_id: str,
    workflow_type: str,
    input_s3_key: str,
    params: dict,
    notification_email: str = None,
) -> str:
    """Start a Step Functions execution and return the execution ARN."""
    sfn_input = {
        "job_id": job_id,
        "workflow_type": workflow_type,
        "input_s3_key": input_s3_key,
        "parameters": params,
        "storage_bucket": STORAGE_BUCKET,
    }
    if notification_email:
        sfn_input["notification_email"] = notification_email

    response = sfn_client.start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=job_id,
        input=json.dumps(sfn_input),
    )
    return response["executionArn"]
