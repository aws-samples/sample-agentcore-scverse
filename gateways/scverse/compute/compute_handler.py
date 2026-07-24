"""
Compute Lambda entry point for real scanpy execution.

Runs inside a container-based Lambda (up to 10 GB image, 10 GB memory,
15 min timeout).  Downloads .h5ad from S3, dispatches to the appropriate
scanpy workflow, uploads outputs, and returns presigned URLs.
"""

import json
import os
import time
import uuid
import tempfile

import boto3

from scanpy_qc import run_scanpy_qc
from scanpy_preprocessing import run_scanpy_preprocessing

# ---------------------------------------------------------------------------
# AWS clients and configuration
# ---------------------------------------------------------------------------
s3_client = boto3.client("s3")
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "")
PRESIGNED_URL_EXPIRY = 3600


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def download_h5ad(bucket: str, key: str) -> str:
    """Download an .h5ad file from S3 to /tmp and return the local path."""
    local_path = f"/tmp/{uuid.uuid4().hex}.h5ad"
    s3_client.download_file(bucket, key, local_path)
    return local_path


def upload_artifact(local_path: str, bucket: str, s3_key: str) -> str:
    """Upload a local file to S3 and return the s3_key."""
    s3_client.upload_file(local_path, bucket, s3_key)
    return s3_key


def generate_presigned_url(bucket: str, key: str) -> str:
    """Generate a presigned GET URL for an S3 object."""
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context) -> dict:
    """
    Entry point for the Compute Lambda.

    Expected *event* keys:
        job_id          – optional str (generated if absent)
        workflow_type   – str: "qc" or "preprocessing"
        input_s3_key    – str: S3 key of the input .h5ad file
        parameters      – dict: workflow-specific parameters
        storage_bucket  – str: S3 bucket name
    """
    start = time.time()

    job_id = event.get("job_id") or f"job-{uuid.uuid4().hex[:8]}"
    workflow_type = event.get("workflow_type", "qc")
    input_s3_key = event.get("input_s3_key", "")
    parameters = event.get("parameters", {})
    bucket = event.get("storage_bucket") or STORAGE_BUCKET

    output_prefix = f"scverse-jobs/{job_id}/outputs/"

    try:
        # Download input -------------------------------------------------
        local_h5ad = download_h5ad(bucket, input_s3_key)

        # Create a temp directory for outputs ----------------------------
        output_dir = tempfile.mkdtemp(prefix=f"{job_id}_")

        # Route to the correct workflow ----------------------------------
        if workflow_type == "qc":
            result = run_scanpy_qc(local_h5ad, parameters, output_dir)
        elif workflow_type == "preprocessing":
            result = run_scanpy_preprocessing(local_h5ad, parameters, output_dir)
        else:
            return {
                "status": "error",
                "error": f"Unsupported workflow_type '{workflow_type}' for Compute Lambda. "
                         "Supported: qc, preprocessing.",
            }

        # Upload outputs to S3 -------------------------------------------
        uploaded_keys = []
        plots_response = {}

        # Upload plots
        for plot_filename in result.get("plots", []):
            local_plot = os.path.join(output_dir, plot_filename)
            if os.path.exists(local_plot):
                s3_key = f"{output_prefix}plots/{plot_filename}"
                upload_artifact(local_plot, bucket, s3_key)
                uploaded_keys.append(s3_key)
                url = generate_presigned_url(bucket, s3_key)
                plot_name = os.path.splitext(plot_filename)[0]
                plots_response[plot_name] = {
                    "s3_key": s3_key,
                    "presigned_url": url,
                    "markdown_image": f"![{plot_name}]({url})",
                }

        # Upload filtered / preprocessed h5ad
        output_h5ad_name = result.get("filtered_h5ad") or result.get("preprocessed_h5ad")
        output_h5ad_key = ""
        if output_h5ad_name:
            local_h5ad_out = os.path.join(output_dir, output_h5ad_name)
            if os.path.exists(local_h5ad_out):
                output_h5ad_key = f"{output_prefix}data/{output_h5ad_name}"
                upload_artifact(local_h5ad_out, bucket, output_h5ad_key)
                uploaded_keys.append(output_h5ad_key)

        # Build response -------------------------------------------------
        elapsed = round(time.time() - start, 2)

        response = {
            "status": "success",
            "job_id": job_id,
            "workflow_type": workflow_type,
            "processing_time_seconds": elapsed,
            "plots": plots_response,
            "output_s3_key": output_h5ad_key,
            "output_s3_keys": uploaded_keys,
        }

        # Attach workflow-specific summary
        if "qc_summary" in result:
            response["qc_summary"] = result["qc_summary"]
        if "preprocessing_summary" in result:
            response["preprocessing_summary"] = result["preprocessing_summary"]

        return response

    except Exception as e:
        # Catch anndata ReadError, KeyError, or any other failure
        error_type = type(e).__name__
        is_read_error = "ReadError" in error_type or isinstance(e, KeyError)
        if is_read_error:
            return {
                "status": "error",
                "error": f"Failed to read file as .h5ad: {str(e)}",
            }
        return {
            "status": "error",
            "error": f"{error_type}: {str(e)}",
        }
