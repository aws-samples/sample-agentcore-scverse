"""
Job management helpers and S3 utilities for the scverse Gateway.

Provides shared constants, job ID generation, S3 path construction,
input validation, presigned URL generation, and human-readable formatting
used across the gateway, compute, and Fargate worker components.
"""

import os
import re
import uuid

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Module-level AWS clients and configuration
# ---------------------------------------------------------------------------
s3_client = boto3.client("s3")
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "")

# ---------------------------------------------------------------------------
# Constants — size thresholds and limits
# ---------------------------------------------------------------------------
SIZE_THRESHOLD_SYNC = 500 * 1024 * 1024          # 500 MB
SIZE_THRESHOLD_FARGATE = 2 * 1024 * 1024 * 1024  # 2 GB
PRESIGNED_URL_EXPIRY = 3600                       # 1 hour
MAX_INLINE_IMAGES = 20
MAX_RESPONSE_BYTES = 6 * 1024 * 1024              # 6 MB

# ---------------------------------------------------------------------------
# Valid workflow types
# ---------------------------------------------------------------------------
VALID_WORKFLOW_TYPES = {
    "qc",
    "preprocessing",
    "integration",
    "clustering",
    "differential_expression",
    "spatial",
    "annotation",
    "perturbation",
    "velocity",
    "full_pipeline",
}

# ---------------------------------------------------------------------------
# Email validation pattern
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


# ---------------------------------------------------------------------------
# Job ID generation
# ---------------------------------------------------------------------------
def generate_job_id() -> str:
    """Return a unique job identifier in the format ``job-<8 hex chars>``."""
    return f"job-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def get_file_size(bucket: str, key: str) -> int:
    """Return the size in bytes of an S3 object via HEAD request.

    Raises:
        FileNotFoundError: If the object does not exist (404 / NoSuchKey).
    """
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
        return response["ContentLength"]
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey"):
            raise FileNotFoundError(
                f"File not found at s3://{bucket}/{key}"
            ) from exc
        raise


def generate_presigned_url(bucket: str, key: str, expiry: int = 3600) -> str:
    """Generate a presigned GET URL for an S3 object."""
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry,
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def validate_workflow_type(workflow_type: str) -> None:
    """Validate that *workflow_type* is one of the accepted values.

    Raises:
        ValueError: If the workflow type is not recognised.
    """
    if workflow_type not in VALID_WORKFLOW_TYPES:
        raise ValueError(
            f"Unknown workflow_type '{workflow_type}'. "
            f"Valid types: {sorted(VALID_WORKFLOW_TYPES)}"
        )


def validate_email(email: str) -> bool:
    """Return ``True`` if *email* matches a basic email format."""
    return bool(_EMAIL_RE.match(email))


# ---------------------------------------------------------------------------
# S3 path builders
# ---------------------------------------------------------------------------
def build_output_prefix(job_id: str) -> str:
    """Return the S3 key prefix for a job's output artifacts."""
    return f"scverse-jobs/{job_id}/outputs/"


def build_intermediate_prefix(job_id: str) -> str:
    """Return the S3 key prefix for a job's intermediate files."""
    return f"scverse-jobs/{job_id}/intermediate/"


def build_manifest_key(job_id: str) -> str:
    """Return the S3 key for a job's manifest file."""
    return f"scverse-jobs/{job_id}/manifest.json"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def format_size_human(size_bytes: int) -> str:
    """Return a human-readable file size string (e.g. ``"245 MB"``, ``"1.2 GB"``)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
