"""
Lambda handler for the scverse Gateway.

Routes requests to the appropriate scverse tool handler based on the
tool name specified in the AgentCore Gateway context.

Supported tools:
  - run_scrna_qc: Single-cell RNA QC (scanpy)
  - run_scvi_analysis: scvi-tools model selection + analysis
  - submit_scverse_job: Submit long-running analysis job
  - check_job_status: Query job status
  - get_job_results: Retrieve output presigned URLs
  - list_datasets: Discover .h5ad files in the storage bucket
"""

import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(__file__))

try:
    # Lambda runtime (flat directory)
    from run_scvi_analysis import run_scvi_analysis
    from compute_router import (
        determine_compute_backend,
        dispatch_to_compute_lambda,
        dispatch_to_fargate,
        dispatch_to_step_functions,
        ComputeLambdaThrottled,
    )
    from dynamodb_client import (
        create_job_record,
        get_job,
        update_job_status,
        append_output_keys,
    )
    from job_utils import (
        generate_job_id,
        get_file_size,
        validate_workflow_type,
        validate_email,
        generate_presigned_url,
        format_size_human,
        STORAGE_BUCKET,
        MAX_INLINE_IMAGES,
    )
except ImportError:
    # Package-style imports (pytest / local dev)
    from gateways.scverse.function.run_scvi_analysis import run_scvi_analysis
    from gateways.scverse.function.compute_router import (
        determine_compute_backend,
        dispatch_to_compute_lambda,
        dispatch_to_fargate,
        dispatch_to_step_functions,
        ComputeLambdaThrottled,
    )
    from gateways.scverse.function.dynamodb_client import (
        create_job_record,
        get_job,
        update_job_status,
        append_output_keys,
    )
    from gateways.scverse.function.job_utils import (
        generate_job_id,
        get_file_size,
        validate_workflow_type,
        validate_email,
        generate_presigned_url,
        format_size_human,
        STORAGE_BUCKET,
        MAX_INLINE_IMAGES,
    )

s3_client = __import__("boto3").client("s3")


def lambda_handler(event, context):
    try:
        tool_name = context.client_context.custom['bedrockAgentCoreToolName']
        # Log only non-sensitive metadata. The event can contain user-provided
        # values (notification_email, S3 keys, workflow params), so it is not logged.
        print(f"Invoked tool: {tool_name} (request_id={getattr(context, 'aws_request_id', 'n/a')})")

        delimiter = "___"
        if delimiter in tool_name:
            tool_name = tool_name[tool_name.index(delimiter) + len(delimiter):]

        if tool_name == "run_scrna_qc":
            result = _route_scrna_qc(event)
        elif tool_name == "run_scvi_analysis":
            result = _route_scvi_analysis(event)
        elif tool_name == "submit_scverse_job":
            result = handle_submit_job(event)
        elif tool_name == "check_job_status":
            result = handle_check_status(event)
        elif tool_name == "get_job_results":
            result = handle_get_results(event)
        elif tool_name == "list_datasets":
            result = handle_list_datasets(event)
        else:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": f"Unknown tool: {tool_name}",
                    "available_tools": [
                        "run_scrna_qc", "run_scvi_analysis",
                        "submit_scverse_job", "check_job_status",
                        "get_job_results", "list_datasets",
                    ],
                }),
            }

        return {
            "statusCode": 200,
            "body": json.dumps(result) if isinstance(result, dict) else str(result),
        }

    except Exception as e:
        print(f"Error in lambda_handler: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": f"Internal error: {str(e)}",
                "tool_name": tool_name if "tool_name" in dir() else "unknown",
            }),
        }


# ---------------------------------------------------------------------------
# Routing helpers for existing tools
# ---------------------------------------------------------------------------

def _route_scrna_qc(event: dict) -> dict:
    """Route run_scrna_qc to the correct compute backend based on file size."""
    input_s3_key = event.get("input_s3_key", "datasets/pbmc3k_raw.h5ad")

    try:
        backend, mode = determine_compute_backend("run_scrna_qc", input_s3_key)
    except FileNotFoundError:
        return {
            "status": "error",
            "error": f"No dataset found at '{input_s3_key}' in the storage bucket.",
            "hint": (
                "Upload an .h5ad file to the 'datasets/' prefix, or ask "
                "'What datasets are available?' to list what is there."
            ),
        }

    if mode == "sync":
        try:
            result = dispatch_to_compute_lambda({
                "workflow_type": "qc",
                "input_s3_key": input_s3_key,
                "parameters": _extract_qc_params(event),
                "storage_bucket": STORAGE_BUCKET,
            })
            return result
        except ComputeLambdaThrottled:
            # Fall back to async path
            return _submit_async_qc(input_s3_key, event, "LAMBDA")

    # Async path
    return _submit_async_qc(input_s3_key, event, backend)


def _extract_qc_params(event: dict) -> dict:
    """Extract QC-specific parameters from the event."""
    params = {}
    for key in ("species", "mad_counts", "mad_genes", "mad_mt",
                "mt_threshold", "min_cells"):
        if key in event:
            params[key] = event[key]
    return params


def _submit_async_qc(input_s3_key: str, event: dict, backend: str) -> dict:
    """Create a job record and dispatch QC to the async backend."""
    job_id = generate_job_id()
    params = _extract_qc_params(event)
    create_job_record(job_id, "qc", input_s3_key, params, backend)

    if backend == "FARGATE":
        dispatch_to_fargate(job_id, "qc", input_s3_key, params)
    else:
        dispatch_to_fargate(job_id, "qc", input_s3_key, params)

    return {
        "status": "submitted",
        "job_id": job_id,
        "compute_backend": backend,
        "message": (
            f"QC job submitted as {job_id}. The file is too large for "
            "synchronous processing. Use check_job_status to monitor progress "
            "and get_job_results to retrieve outputs when complete."
        ),
    }


def _route_scvi_analysis(event: dict) -> dict:
    """Route run_scvi_analysis — always async via Fargate."""
    input_s3_key = event.get("input_s3_key")

    # If no input_s3_key, fall back to the pre-computed stub
    if not input_s3_key:
        return run_scvi_analysis(event)

    job_id = generate_job_id()
    params = {k: v for k, v in event.items() if k != "input_s3_key"}
    notification_email = event.get("notification_email")

    create_job_record(
        job_id, "integration", input_s3_key, params, "FARGATE",
        notification_email=notification_email,
    )
    dispatch_to_fargate(
        job_id, "integration", input_s3_key, params,
        notification_email=notification_email,
    )

    return {
        "status": "submitted",
        "job_id": job_id,
        "compute_backend": "FARGATE",
        "message": (
            f"scvi-tools analysis submitted as {job_id}. "
            "Use check_job_status to monitor progress and "
            "get_job_results to retrieve outputs when complete."
        ),
    }


# ---------------------------------------------------------------------------
# Job management handlers
# ---------------------------------------------------------------------------

def handle_submit_job(event: dict) -> dict:
    """Submit a long-running scverse analysis job."""
    workflow_type = event.get("workflow")
    input_s3_key = event.get("input_s3_key")
    notification_email = event.get("notification_email")
    parameters = event.get("parameters", {})

    # Validate required fields
    if not workflow_type:
        return {"status": "error", "error": "Missing required field 'workflow'."}
    if not input_s3_key:
        return {"status": "error", "error": "Missing required field 'input_s3_key'."}

    try:
        validate_workflow_type(workflow_type)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    # Validate the file exists in S3
    try:
        get_file_size(STORAGE_BUCKET, input_s3_key)
    except FileNotFoundError:
        return {"status": "error", "error": f"File not found at key '{input_s3_key}'."}

    # Validate email if provided
    if notification_email and not validate_email(notification_email):
        return {"status": "error", "error": "Invalid notification_email format."}

    job_id = generate_job_id()
    backend, _mode = determine_compute_backend(
        "submit_scverse_job", input_s3_key, workflow_type,
    )

    create_job_record(
        job_id, workflow_type, input_s3_key, parameters, backend,
        notification_email=notification_email,
    )

    # Dispatch to the selected backend
    if backend == "STEP_FUNCTIONS":
        execution_arn = dispatch_to_step_functions(
            job_id, workflow_type, input_s3_key, parameters,
            notification_email=notification_email,
        )
        update_job_status(
            job_id, "SUBMITTED",
            step_functions_execution_arn=execution_arn,
        )
    else:
        dispatch_to_fargate(
            job_id, workflow_type, input_s3_key, parameters,
            notification_email=notification_email,
        )

    return {
        "status": "submitted",
        "job_id": job_id,
        "compute_backend": backend,
        "message": (
            f"Job {job_id} submitted for {workflow_type} analysis. "
            "Use check_job_status to monitor progress and "
            "get_job_results to retrieve outputs when complete."
        ),
    }


def handle_check_status(event: dict) -> dict:
    """Check status of a submitted job."""
    job_id = event.get("job_id", "")
    if not job_id:
        return {"status": "error", "error": "Missing required field 'job_id'."}

    job = get_job(job_id)
    if job is None:
        return {
            "status": "error",
            "error": f"Job '{job_id}' not found. Verify the Job ID format (job-xxxxxxxx).",
        }

    result = {
        "status": job["status"],
        "job_id": job_id,
        "workflow_type": job.get("workflow_type"),
        "compute_backend": job.get("compute_backend"),
    }

    # Calculate elapsed time
    submission_ts = job.get("submission_timestamp")
    if submission_ts:
        try:
            submitted_at = datetime.fromisoformat(submission_ts.replace("Z", "+00:00"))
            now = datetime.utcnow().replace(tzinfo=submitted_at.tzinfo)
            result["elapsed_seconds"] = round((now - submitted_at).total_seconds(), 1)
        except (ValueError, TypeError):
            pass

    if job.get("current_step_name"):
        result["current_step_name"] = job["current_step_name"]

    if job["status"] == "FAILED":
        if job.get("error_message"):
            result["error_message"] = job["error_message"]
        if job.get("error_step"):
            result["error_step"] = job["error_step"]

    return result


def handle_get_results(event: dict) -> dict:
    """Retrieve results (presigned URLs) for a job."""
    job_id = event.get("job_id", "")
    if not job_id:
        return {"status": "error", "error": "Missing required field 'job_id'."}

    job = get_job(job_id)
    if job is None:
        return {
            "status": "error",
            "error": f"Job '{job_id}' not found. Verify the Job ID format (job-xxxxxxxx).",
        }

    status = job["status"]

    if status == "SUBMITTED":
        return {
            "status": "SUBMITTED",
            "job_id": job_id,
            "message": "Job is queued and waiting for compute resources.",
        }

    if status == "RUNNING":
        result = {
            "status": "RUNNING",
            "job_id": job_id,
            "message": "Job is currently running.",
        }
        if job.get("current_step_name"):
            result["current_step_name"] = job["current_step_name"]
        submission_ts = job.get("submission_timestamp")
        if submission_ts:
            try:
                submitted_at = datetime.fromisoformat(submission_ts.replace("Z", "+00:00"))
                now = datetime.utcnow().replace(tzinfo=submitted_at.tzinfo)
                result["elapsed_seconds"] = round((now - submitted_at).total_seconds(), 1)
            except (ValueError, TypeError):
                pass
        return result

    if status == "COMPLETED":
        return _build_completed_response(job)

    if status == "FAILED":
        return _build_failed_response(job)

    return {"status": status, "job_id": job_id}


def _build_completed_response(job: dict) -> dict:
    """Build the response for a completed job with presigned URLs."""
    job_id = job["job_id"]
    output_keys = job.get("output_s3_keys", [])

    artifacts = []
    plot_count = 0

    for key in output_keys:
        try:
            url = generate_presigned_url(STORAGE_BUCKET, key)
        except Exception:
            continue

        entry = {"s3_key": key, "presigned_url": url}

        # Format plots as markdown images (up to MAX_INLINE_IMAGES)
        if key.endswith((".png", ".jpg", ".jpeg", ".svg")):
            if plot_count < MAX_INLINE_IMAGES:
                file_name = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                description = file_name.replace("_", " ").title()
                entry["markdown_image"] = f"![{description}]({url})"
                plot_count += 1

        artifacts.append(entry)

    return {
        "status": "COMPLETED",
        "job_id": job_id,
        "workflow_type": job.get("workflow_type"),
        "artifacts": artifacts,
        "artifact_count": len(artifacts),
    }


def _build_failed_response(job: dict) -> dict:
    """Build the response for a failed job with error info and partial outputs."""
    job_id = job["job_id"]
    result = {
        "status": "FAILED",
        "job_id": job_id,
        "workflow_type": job.get("workflow_type"),
    }

    if job.get("error_message"):
        result["error_message"] = job["error_message"]
    if job.get("error_step"):
        result["error_step"] = job["error_step"]

    # Include presigned URLs for any partial outputs
    output_keys = job.get("output_s3_keys", [])
    if output_keys:
        partial_artifacts = []
        for key in output_keys:
            try:
                url = generate_presigned_url(STORAGE_BUCKET, key)
                partial_artifacts.append({"s3_key": key, "presigned_url": url})
            except Exception:
                continue
        if partial_artifacts:
            result["partial_artifacts"] = partial_artifacts

    return result


# ---------------------------------------------------------------------------
# Dataset listing
# ---------------------------------------------------------------------------

def handle_list_datasets(event: dict) -> dict:
    """List .h5ad datasets available in the storage bucket."""
    prefix_filter = event.get("prefix_filter", "")
    next_token = event.get("next_token")
    search_prefix = f"datasets/{prefix_filter}"

    list_kwargs = {
        "Bucket": STORAGE_BUCKET,
        "Prefix": search_prefix,
        "MaxKeys": 50,
    }
    if next_token:
        list_kwargs["ContinuationToken"] = next_token

    try:
        response = s3_client.list_objects_v2(**list_kwargs)
    except Exception as exc:
        return {"status": "error", "error": f"Failed to list datasets: {str(exc)}"}

    datasets = []
    for obj in response.get("Contents", []):
        key = obj["Key"]
        if not key.endswith(".h5ad"):
            continue
        file_name = key.rsplit("/", 1)[-1]
        datasets.append({
            "s3_key": key,
            "file_name": file_name,
            "size_bytes": obj["Size"],
            "size_human": format_size_human(obj["Size"]),
            "last_modified": obj["LastModified"].isoformat(),
        })

    # Sort by last_modified descending
    datasets.sort(key=lambda d: d["last_modified"], reverse=True)

    result = {
        "status": "success",
        "datasets": datasets,
        "count": len(datasets),
    }

    if response.get("IsTruncated"):
        result["next_token"] = response.get("NextContinuationToken")
    else:
        result["next_token"] = None

    return result
