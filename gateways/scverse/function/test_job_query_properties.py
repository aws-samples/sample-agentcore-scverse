"""
Property-based tests for the handle_get_results job query response.

Validates:
- Property 4: Job query response varies correctly by status — SUBMITTED
  returns a queued message with no URLs; RUNNING returns progress info
  with no URLs; COMPLETED returns presigned URLs (with .png formatted as
  markdown images); FAILED returns error info plus partial output URLs.
- Property 5: Response payload size limit — at most MAX_INLINE_IMAGES
  markdown image references and total serialized size under
  MAX_RESPONSE_BYTES, regardless of how many output artifacts the job has.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 1.10
"""

import json
import os
import sys
from unittest.mock import patch

# Set fake AWS credentials/region + required env vars BEFORE importing any
# module that constructs boto3 clients at module load (job_utils, lambda_function).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("JOB_TABLE_NAME", "test-job-table")

# Ensure the function directory is on the import path when running from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hypothesis import given, settings, strategies as st

from job_utils import MAX_INLINE_IMAGES, MAX_RESPONSE_BYTES, VALID_WORKFLOW_TYPES
import lambda_function


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_STATUSES = ("SUBMITTED", "RUNNING", "COMPLETED", "FAILED")

# Realistic output key extensions the handler treats specially (.png, .jpg,
# .jpeg, .svg become markdown images). Other extensions are plain data artifacts.
_extensions = st.sampled_from(
    [".png", ".jpg", ".jpeg", ".svg", ".h5ad", ".json", ".html", ".csv"]
)

_output_key = st.builds(
    lambda name, ext: f"scverse-jobs/job-abcd1234/outputs/{name}{ext}",
    st.from_regex(r"[a-z_][a-z0-9_]{0,20}", fullmatch=True),
    _extensions,
)

_output_keys_list = st.lists(_output_key, min_size=0, max_size=20)

_workflow_strategy = st.sampled_from(sorted(VALID_WORKFLOW_TYPES))

_error_message = st.text(min_size=0, max_size=200).filter(lambda s: s.isprintable())
_error_step = st.sampled_from(
    ["qc", "integration", "clustering", "differential_expression", "spatial"]
)


def _make_job(
    status: str,
    output_keys: list,
    error_message: str = "",
    workflow_type: str = "qc",
    error_step: str = "qc",
) -> dict:
    """Build a realistic DynamoDB job record used by the mocked get_job()."""
    job = {
        "job_id": "job-abcd1234",
        "status": status,
        "workflow_type": workflow_type,
        "compute_backend": "FARGATE",
        "submission_timestamp": "2025-01-01T00:00:00Z",
        "output_s3_keys": output_keys,
    }
    if status == "RUNNING":
        job["current_step_name"] = "integration"
    if status == "FAILED":
        if error_message:
            job["error_message"] = error_message
        job["error_step"] = error_step
    return job


# ---------------------------------------------------------------------------
# Property 4: Job query response varies correctly by status
# ---------------------------------------------------------------------------


@settings(max_examples=50)
@given(
    status=st.sampled_from(_STATUSES),
    output_keys=_output_keys_list,
    error_message=_error_message,
    workflow_type=_workflow_strategy,
    error_step=_error_step,
)
def test_get_job_results_response_by_status(
    status, output_keys, error_message, workflow_type, error_step
):
    """handle_get_results must return the correct shape for every status.

    Validates Requirements: 3.1, 3.2, 3.3, 3.4, 3.5
    """
    job = _make_job(status, output_keys, error_message, workflow_type, error_step)

    # Fake presigned URL generator — deterministic and cheap.
    def _fake_url(bucket, key, *args, **kwargs):
        return f"https://presigned.example.com/{bucket}/{key}"

    with (
        patch.object(lambda_function, "get_job", return_value=job),
        patch.object(lambda_function, "generate_presigned_url", side_effect=_fake_url),
    ):
        response = lambda_function.handle_get_results({"job_id": "job-abcd1234"})

    # Every response must carry the status and the job_id
    assert response["status"] == status, f"Status mismatch: {response}"
    assert response["job_id"] == "job-abcd1234"

    if status == "SUBMITTED":
        # SUBMITTED → queued message, no presigned URLs anywhere in the payload
        assert "message" in response, f"SUBMITTED must include a message: {response}"
        assert "queued" in response["message"].lower(), (
            f"SUBMITTED message must mention 'queued': {response['message']!r}"
        )
        serialized = json.dumps(response)
        assert "presigned_url" not in serialized
        assert "artifacts" not in response
        assert "partial_artifacts" not in response
        return

    if status == "RUNNING":
        # RUNNING → status + current_step_name + elapsed_seconds, no URLs
        assert response.get("current_step_name") == "integration"
        assert "elapsed_seconds" in response, (
            f"RUNNING must include elapsed_seconds: {response}"
        )
        assert isinstance(response["elapsed_seconds"], (int, float))
        serialized = json.dumps(response)
        assert "presigned_url" not in serialized
        assert "artifacts" not in response
        return

    if status == "COMPLETED":
        # COMPLETED → artifacts list with presigned URLs for every output key
        assert "artifacts" in response, f"COMPLETED must include artifacts: {response}"
        assert response["artifact_count"] == len(output_keys)
        assert len(response["artifacts"]) == len(output_keys)

        image_extensions = (".png", ".jpg", ".jpeg", ".svg")
        image_keys = [k for k in output_keys if k.endswith(image_extensions)]

        for entry, key in zip(response["artifacts"], output_keys):
            assert entry["s3_key"] == key
            assert entry["presigned_url"].startswith("https://presigned.example.com/")
            assert "test-bucket" in entry["presigned_url"]

        # Every .png/.jpg/.jpeg/.svg that fits within MAX_INLINE_IMAGES must
        # have a markdown_image field formatted as `![description](url)`.
        markdown_entries = [
            e for e in response["artifacts"] if "markdown_image" in e
        ]
        assert len(markdown_entries) <= MAX_INLINE_IMAGES, (
            f"Exceeded inline image cap: {len(markdown_entries)} > {MAX_INLINE_IMAGES}"
        )
        # markdown_image only appears on image-extension entries
        for entry in markdown_entries:
            assert entry["s3_key"].endswith(image_extensions), (
                f"Non-image entry got markdown_image: {entry}"
            )
            md = entry["markdown_image"]
            assert md.startswith("!["), f"Bad markdown format: {md!r}"
            assert "](" in md and md.endswith(")"), (
                f"Bad markdown format: {md!r}"
            )
            assert entry["presigned_url"] in md, (
                f"URL missing from markdown: {md!r}"
            )
        # If there are at most MAX_INLINE_IMAGES images, they should ALL be
        # rendered as markdown. Beyond that, only the first MAX_INLINE_IMAGES.
        expected_markdown_count = min(len(image_keys), MAX_INLINE_IMAGES)
        assert len(markdown_entries) == expected_markdown_count
        return

    if status == "FAILED":
        # FAILED → error_step (always) + error_message (if provided) +
        # partial_artifacts for any output_s3_keys present.
        assert response["error_step"] == error_step
        if error_message:
            assert response["error_message"] == error_message
        if output_keys:
            assert "partial_artifacts" in response, (
                f"FAILED with outputs must include partial_artifacts: {response}"
            )
            assert len(response["partial_artifacts"]) == len(output_keys)
            for entry, key in zip(response["partial_artifacts"], output_keys):
                assert entry["s3_key"] == key
                assert entry["presigned_url"].startswith(
                    "https://presigned.example.com/"
                )
        else:
            assert "partial_artifacts" not in response
        return


# ---------------------------------------------------------------------------
# Property 5: Response payload size limit
# ---------------------------------------------------------------------------


# Strategy for COMPLETED jobs with 0–100 image outputs — this is the worst
# case for inline markdown images and response size.
_large_image_output_keys = st.lists(
    st.builds(
        lambda name: f"scverse-jobs/job-abcd1234/outputs/plots/{name}.png",
        st.from_regex(r"[a-z_][a-z0-9_]{0,40}", fullmatch=True),
    ),
    min_size=0,
    max_size=100,
)


@settings(max_examples=50, deadline=None)
@given(output_keys=_large_image_output_keys)
def test_completed_response_caps_inline_images_and_payload_size(output_keys):
    """COMPLETED response must cap markdown images and stay under the size limit.

    For N outputs (0–100), the response:
    - contains at most MAX_INLINE_IMAGES (20) markdown image references
    - serializes to less than MAX_RESPONSE_BYTES (6 MB)

    Validates Requirements: 1.10, 3.7
    """
    job = _make_job("COMPLETED", output_keys, workflow_type="qc")

    def _fake_url(bucket, key, *args, **kwargs):
        # Realistic-length presigned URL with query-string cruft
        return (
            f"https://{bucket}.s3.us-west-2.amazonaws.com/{key}"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=EXAMPLE/20250101/us-west-2/s3/aws4_request"
            "&X-Amz-Date=20250101T000000Z"
            "&X-Amz-Expires=3600"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=" + "a" * 64
        )

    with (
        patch.object(lambda_function, "get_job", return_value=job),
        patch.object(lambda_function, "generate_presigned_url", side_effect=_fake_url),
    ):
        response = lambda_function.handle_get_results({"job_id": "job-abcd1234"})

    assert response["status"] == "COMPLETED"

    # Inline markdown image cap
    markdown_count = sum(
        1 for entry in response["artifacts"] if "markdown_image" in entry
    )
    assert markdown_count <= MAX_INLINE_IMAGES, (
        f"Too many inline images: {markdown_count} > {MAX_INLINE_IMAGES} "
        f"(for {len(output_keys)} outputs)"
    )

    # Payload size cap — serialize and check byte length
    serialized = json.dumps(response)
    assert len(serialized) < MAX_RESPONSE_BYTES, (
        f"Response exceeds 6 MB: {len(serialized)} bytes "
        f"(for {len(output_keys)} outputs)"
    )


# ---------------------------------------------------------------------------
# Targeted boundary tests for the inline image cap
# ---------------------------------------------------------------------------


def test_completed_response_with_exactly_max_inline_images():
    """Exactly MAX_INLINE_IMAGES image outputs should all render inline."""
    output_keys = [
        f"scverse-jobs/job-abcd1234/outputs/plots/plot_{i}.png"
        for i in range(MAX_INLINE_IMAGES)
    ]
    job = _make_job("COMPLETED", output_keys)

    def _fake_url(bucket, key, *args, **kwargs):
        return f"https://example.com/{key}"

    with (
        patch.object(lambda_function, "get_job", return_value=job),
        patch.object(lambda_function, "generate_presigned_url", side_effect=_fake_url),
    ):
        response = lambda_function.handle_get_results({"job_id": "job-abcd1234"})

    markdown_count = sum(
        1 for entry in response["artifacts"] if "markdown_image" in entry
    )
    assert markdown_count == MAX_INLINE_IMAGES
    assert response["artifact_count"] == MAX_INLINE_IMAGES


def test_completed_response_over_max_inline_images():
    """More than MAX_INLINE_IMAGES image outputs — only the first N get markdown."""
    n = MAX_INLINE_IMAGES + 5
    output_keys = [
        f"scverse-jobs/job-abcd1234/outputs/plots/plot_{i}.png" for i in range(n)
    ]
    job = _make_job("COMPLETED", output_keys)

    def _fake_url(bucket, key, *args, **kwargs):
        return f"https://example.com/{key}"

    with (
        patch.object(lambda_function, "get_job", return_value=job),
        patch.object(lambda_function, "generate_presigned_url", side_effect=_fake_url),
    ):
        response = lambda_function.handle_get_results({"job_id": "job-abcd1234"})

    # All N artifacts are still listed…
    assert response["artifact_count"] == n
    # …but only the first MAX_INLINE_IMAGES have markdown_image
    markdown_entries = [e for e in response["artifacts"] if "markdown_image" in e]
    assert len(markdown_entries) == MAX_INLINE_IMAGES
    # And they should be the first N (insertion order)
    for entry in response["artifacts"][:MAX_INLINE_IMAGES]:
        assert "markdown_image" in entry
    for entry in response["artifacts"][MAX_INLINE_IMAGES:]:
        assert "markdown_image" not in entry


def test_completed_response_mixed_artifact_types():
    """Only image extensions receive markdown_image; data files do not."""
    output_keys = [
        "scverse-jobs/job-abcd1234/outputs/plots/umap.png",
        "scverse-jobs/job-abcd1234/outputs/data/filtered.h5ad",
        "scverse-jobs/job-abcd1234/outputs/metrics/qc.json",
        "scverse-jobs/job-abcd1234/outputs/reports/pipeline.html",
        "scverse-jobs/job-abcd1234/outputs/plots/violin.jpg",
    ]
    job = _make_job("COMPLETED", output_keys)

    def _fake_url(bucket, key, *args, **kwargs):
        return f"https://example.com/{key}"

    with (
        patch.object(lambda_function, "get_job", return_value=job),
        patch.object(lambda_function, "generate_presigned_url", side_effect=_fake_url),
    ):
        response = lambda_function.handle_get_results({"job_id": "job-abcd1234"})

    artifacts_by_key = {a["s3_key"]: a for a in response["artifacts"]}
    # Images get markdown_image
    assert "markdown_image" in artifacts_by_key[
        "scverse-jobs/job-abcd1234/outputs/plots/umap.png"
    ]
    assert "markdown_image" in artifacts_by_key[
        "scverse-jobs/job-abcd1234/outputs/plots/violin.jpg"
    ]
    # Non-image files do not
    assert "markdown_image" not in artifacts_by_key[
        "scverse-jobs/job-abcd1234/outputs/data/filtered.h5ad"
    ]
    assert "markdown_image" not in artifacts_by_key[
        "scverse-jobs/job-abcd1234/outputs/metrics/qc.json"
    ]
    assert "markdown_image" not in artifacts_by_key[
        "scverse-jobs/job-abcd1234/outputs/reports/pipeline.html"
    ]
