"""
DynamoDB Job Table CRUD operations for the scverse Gateway.

Provides functions to create, read, and update job records in the
DynamoDB Job Table used for asynchronous job tracking.
"""

import os
from datetime import datetime, timedelta

import boto3

# ---------------------------------------------------------------------------
# Module-level AWS resources and configuration
# ---------------------------------------------------------------------------
dynamodb = boto3.resource("dynamodb")
JOB_TABLE_NAME = os.environ.get("JOB_TABLE_NAME", "scverse-job-table")
table = dynamodb.Table(JOB_TABLE_NAME)


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------
def create_job_record(
    job_id: str,
    workflow_type: str,
    input_s3_key: str,
    parameters: dict,
    compute_backend: str,
    notification_email: str = None,
) -> dict:
    """Create a new job record in the Job Table.

    Uses a conditional write (``attribute_not_exists``) to guarantee
    idempotent creation — a duplicate ``job_id`` will raise a
    ``ConditionalCheckFailedException`` instead of silently overwriting.

    Returns the item dict that was written.
    """
    item = {
        "job_id": job_id,
        "workflow_type": workflow_type,
        "input_s3_key": input_s3_key,
        "parameters": parameters,
        "status": "SUBMITTED",
        "compute_backend": compute_backend,
        "submission_timestamp": datetime.utcnow().isoformat() + "Z",
        "output_s3_keys": [],
        "ttl": int((datetime.utcnow() + timedelta(days=30)).timestamp()),
    }
    if notification_email is not None:
        item["notification_email"] = notification_email

    table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(job_id)",
    )
    return item


def get_job(job_id: str) -> dict | None:
    """Retrieve a job record by its ID.

    Returns the item dict if found, or ``None`` if the job does not exist.
    """
    response = table.get_item(Key={"job_id": job_id})
    return response.get("Item")


def update_job_status(
    job_id: str,
    status: str,
    current_step_name: str = None,
    output_s3_keys: list = None,
    error_message: str = None,
    error_step: str = None,
    start_timestamp: str = None,
    completion_timestamp: str = None,
    heartbeat_timestamp: str = None,
    step_functions_execution_arn: str = None,
) -> None:
    """Update a job record with a new status and optional attributes.

    Builds the ``UpdateExpression`` dynamically so only non-None
    parameters are written.
    """
    expr_parts = ["#st = :status"]
    values = {":status": status}
    names = {"#st": "status"}

    _optional_fields = {
        "current_step_name": current_step_name,
        "output_s3_keys": output_s3_keys,
        "error_message": error_message,
        "error_step": error_step,
        "start_timestamp": start_timestamp,
        "completion_timestamp": completion_timestamp,
        "heartbeat_timestamp": heartbeat_timestamp,
        "step_functions_execution_arn": step_functions_execution_arn,
    }

    for field_name, field_value in _optional_fields.items():
        if field_value is not None:
            placeholder = f":{field_name}"
            alias = f"#{field_name}"
            expr_parts.append(f"{alias} = {placeholder}")
            values[placeholder] = field_value
            names[alias] = field_name

    update_expression = "SET " + ", ".join(expr_parts)

    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=update_expression,
        ExpressionAttributeValues=values,
        ExpressionAttributeNames=names,
    )


def append_output_keys(job_id: str, new_keys: list[str]) -> None:
    """Append new S3 keys to a job's ``output_s3_keys`` list.

    Uses ``list_append`` so existing keys are preserved.
    """
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #keys = list_append(#keys, :keys)",
        ExpressionAttributeValues={":keys": new_keys},
        ExpressionAttributeNames={"#keys": "output_s3_keys"},
    )
