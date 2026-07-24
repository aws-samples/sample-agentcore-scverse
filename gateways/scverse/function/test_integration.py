"""
End-to-end integration tests for the scverse Gateway Lambda handler.

These tests drive the ``lambda_handler`` through its full request flow —
routing decision, DynamoDB writes, compute dispatch, and result
retrieval — with mocked AWS services. They complement the focused unit
tests in ``test_routing_unit.py`` and the property tests in
``test_routing_properties.py``.

Covered paths:
  1. Synchronous QC (<500 MB → Compute Lambda sync)
  2. Async job submission (500 MB–2 GB → async Lambda via Fargate dispatch)
  3. Full pipeline submission (→ Step Functions)
  4. ``check_job_status`` → ``get_job_results`` flow for a COMPLETED job
  5. ``list_datasets`` filtering to ``.h5ad``
  6. Sync-to-async fallback on ``ComputeLambdaThrottled``

Validates: Requirements 1.2, 1.3, 1.4, 2.1, 2.3, 3.1, 3.2, 3.3, 3.5,
4.6, 10.1
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Ensure boto3 clients don't try to discover real credentials when modules
# under test construct clients at import time.
os.environ.setdefault("STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("JOB_TABLE_NAME", "test-job-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

# Match the style used by test_routing_unit.py / test_routing_properties.py:
# add the function directory to sys.path so ``import lambda_function`` works.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

JOB_ID_PATTERN = re.compile(r"^job-[a-f0-9]{8}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(tool_name: str) -> MagicMock:
    """Build a mock Lambda context carrying the AgentCore tool name."""
    ctx = MagicMock()
    ctx.client_context.custom = {"bedrockAgentCoreToolName": tool_name}
    return ctx


def _parse_body(response: dict) -> dict:
    """Parse the JSON body from a Lambda proxy response."""
    assert response["statusCode"] == 200, (
        f"Expected 200, got {response['statusCode']}: {response.get('body')}"
    )
    return json.loads(response["body"])


# ---------------------------------------------------------------------------
# 1. Synchronous QC path end-to-end
# ---------------------------------------------------------------------------

class TestSyncQcEndToEnd:
    """Small file routed to the Compute Lambda → inline QC result."""

    def test_sync_qc_returns_summary_plots_and_output_key(self):
        # 100 MB file, comfortably below the 500 MB sync threshold
        file_size = 100 * 1024 * 1024

        compute_result = {
            "status": "success",
            "qc_summary": {
                "before_filtering": {
                    "n_cells": 2700,
                    "n_genes": 32738,
                    "mean_counts_per_cell": 2367.0,
                    "median_counts_per_cell": 2197.0,
                    "mean_mt_pct": 2.1,
                },
                "after_filtering": {
                    "n_cells": 2638,
                    "n_genes": 13714,
                    "cells_removed": 62,
                    "cells_retained_pct": 97.7,
                },
            },
            "plots": {
                "qc_violin_before": {
                    "s3_key": "scverse-jobs/sync/outputs/qc_violin_before.png",
                    "presigned_url": "https://presigned/qc_violin_before.png",
                    "markdown_image": "![QC Before](https://presigned/qc_violin_before.png)",
                },
                "qc_thresholds": {
                    "s3_key": "scverse-jobs/sync/outputs/qc_thresholds.png",
                    "presigned_url": "https://presigned/qc_thresholds.png",
                    "markdown_image": "![Thresholds](https://presigned/qc_thresholds.png)",
                },
                "qc_violin_after": {
                    "s3_key": "scverse-jobs/sync/outputs/qc_violin_after.png",
                    "presigned_url": "https://presigned/qc_violin_after.png",
                    "markdown_image": "![QC After](https://presigned/qc_violin_after.png)",
                },
            },
            "output_s3_key": "scverse-jobs/sync/outputs/filtered.h5ad",
        }

        mock_lambda_client = MagicMock()
        mock_lambda_payload = MagicMock()
        mock_lambda_payload.read.return_value = json.dumps(compute_result).encode("utf-8")
        mock_lambda_client.invoke.return_value = {
            "Payload": mock_lambda_payload,
            "StatusCode": 200,
        }

        with (
            patch("compute_router.get_file_size", return_value=file_size),
            patch("compute_router.lambda_client", mock_lambda_client),
            patch.dict(os.environ, {"COMPUTE_LAMBDA_ARN": "arn:aws:lambda:us-west-2:0:function:compute"}),
            patch("lambda_function.create_job_record") as mock_create_job,
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"input_s3_key": "datasets/pbmc_small.h5ad", "species": "human"},
                _make_context("run_scrna_qc"),
            )

        body = _parse_body(response)

        # Core QC fields must round-trip to the researcher
        assert body["status"] == "success"
        assert "qc_summary" in body
        assert body["qc_summary"]["before_filtering"]["n_cells"] == 2700
        assert body["qc_summary"]["after_filtering"]["n_cells"] == 2638

        # Plots carry presigned URLs
        assert "plots" in body
        for plot_entry in body["plots"].values():
            assert plot_entry["presigned_url"].startswith("https://")

        # Output .h5ad key is returned for downstream steps
        assert body["output_s3_key"] == "scverse-jobs/sync/outputs/filtered.h5ad"

        # Sync path must NOT create a job record
        mock_create_job.assert_not_called()

        # Compute Lambda was invoked synchronously (RequestResponse)
        mock_lambda_client.invoke.assert_called_once()
        invoke_kwargs = mock_lambda_client.invoke.call_args.kwargs
        assert invoke_kwargs["InvocationType"] == "RequestResponse"


# ---------------------------------------------------------------------------
# 2. Async job submission path (500 MB – 2 GB → Lambda async, dispatched via Fargate path)
# ---------------------------------------------------------------------------

class TestAsyncQcSubmission:
    """Medium file must be async: create job record, dispatch, return job_id."""

    def test_async_qc_returns_well_formed_job_id(self):
        # 600 MB → between SIZE_THRESHOLD_SYNC (500 MB) and SIZE_THRESHOLD_FARGATE (2 GB)
        # Per ``_submit_async_qc`` this dispatches via ``dispatch_to_fargate``
        # regardless of backend label (LAMBDA-async or FARGATE).
        file_size = 600 * 1024 * 1024

        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with (
            patch("compute_router.get_file_size", return_value=file_size),
            patch("dynamodb_client.table", mock_table),
            patch("lambda_function.dispatch_to_fargate") as mock_dispatch_fargate,
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"input_s3_key": "datasets/pbmc_medium.h5ad"},
                _make_context("run_scrna_qc"),
            )

        body = _parse_body(response)

        assert body["status"] == "submitted"
        assert "job_id" in body
        assert JOB_ID_PATTERN.match(body["job_id"]), (
            f"job_id '{body['job_id']}' does not match job-<8 hex chars> format"
        )

        # Compute backend must indicate the async path chosen by the router
        assert body["compute_backend"] in ("LAMBDA", "FARGATE")

        # A DynamoDB job record was created before dispatch
        mock_table.put_item.assert_called_once()
        put_kwargs = mock_table.put_item.call_args.kwargs
        assert put_kwargs["Item"]["job_id"] == body["job_id"]
        assert put_kwargs["Item"]["status"] == "SUBMITTED"
        assert put_kwargs["Item"]["workflow_type"] == "qc"
        assert "ConditionExpression" in put_kwargs  # Req 2.8 idempotent writes

        # Dispatch was attempted exactly once
        mock_dispatch_fargate.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Full pipeline submission → Step Functions
# ---------------------------------------------------------------------------

class TestFullPipelineSubmission:
    """submit_scverse_job + full_pipeline → Step Functions execution."""

    def test_full_pipeline_starts_step_functions_execution(self):
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}
        mock_table.update_item.return_value = {}

        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-west-2:0:execution:pipeline:job-12345678",
            "startDate": datetime.now(tz=timezone.utc),
        }

        with (
            # File size check for input_s3_key validation (S3 HEAD)
            patch("lambda_function.get_file_size", return_value=1_200_000_000),
            patch("dynamodb_client.table", mock_table),
            patch("compute_router.sfn_client", mock_sfn),
            patch.dict(os.environ, {"STATE_MACHINE_ARN": "arn:aws:states:us-west-2:0:stateMachine:scverse-pipeline"}),
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {
                    "workflow": "full_pipeline",
                    "input_s3_key": "datasets/brain_spatial.h5ad",
                    "parameters": {"n_hvgs": 2000, "leiden_resolution": 1.0},
                },
                _make_context("submit_scverse_job"),
            )

        body = _parse_body(response)

        assert body["status"] == "submitted"
        assert JOB_ID_PATTERN.match(body["job_id"])
        assert body["compute_backend"] == "STEP_FUNCTIONS"

        # Job record created with the right backend
        mock_table.put_item.assert_called_once()
        put_item = mock_table.put_item.call_args.kwargs["Item"]
        assert put_item["compute_backend"] == "STEP_FUNCTIONS"
        assert put_item["workflow_type"] == "full_pipeline"
        assert put_item["status"] == "SUBMITTED"

        # Step Functions StartExecution was called with the job_id as execution name
        mock_sfn.start_execution.assert_called_once()
        sfn_kwargs = mock_sfn.start_execution.call_args.kwargs
        assert sfn_kwargs["name"] == body["job_id"]
        sfn_input = json.loads(sfn_kwargs["input"])
        assert sfn_input["job_id"] == body["job_id"]
        assert sfn_input["workflow_type"] == "full_pipeline"
        assert sfn_input["input_s3_key"] == "datasets/brain_spatial.h5ad"


# ---------------------------------------------------------------------------
# 4. check_job_status → get_job_results flow for a COMPLETED job
# ---------------------------------------------------------------------------

class TestStatusThenResultsFlow:
    """A completed job: status is RUNNING-then-COMPLETED, results return URLs."""

    def test_check_status_then_get_results_returns_presigned_urls(self):
        job_id = "job-abc12345"

        completed_record = {
            "job_id": job_id,
            "status": "COMPLETED",
            "workflow_type": "qc",
            "compute_backend": "LAMBDA",
            "submission_timestamp": "2025-06-01T10:00:00Z",
            "completion_timestamp": "2025-06-01T10:02:15Z",
            "output_s3_keys": [
                f"scverse-jobs/{job_id}/outputs/plots/qc_violin_before.png",
                f"scverse-jobs/{job_id}/outputs/plots/qc_thresholds.png",
                f"scverse-jobs/{job_id}/outputs/plots/qc_violin_after.png",
                f"scverse-jobs/{job_id}/outputs/data/filtered.h5ad",
                f"scverse-jobs/{job_id}/outputs/reports/pipeline_report.html",
            ],
        }

        def fake_presigned_url(bucket, key, expiry=3600):
            return f"https://presigned.example.com/{key}"

        # ---- check_job_status ----
        with patch("lambda_function.get_job", return_value=completed_record):
            from lambda_function import lambda_handler

            status_response = lambda_handler(
                {"job_id": job_id},
                _make_context("check_job_status"),
            )

        status_body = _parse_body(status_response)
        assert status_body["status"] == "COMPLETED"
        assert status_body["job_id"] == job_id
        assert status_body["workflow_type"] == "qc"
        assert status_body["compute_backend"] == "LAMBDA"
        # elapsed_seconds is a parsed timing field, may be present
        # depending on timestamp parsing; status must always be present.

        # ---- get_job_results ----
        with (
            patch("lambda_function.get_job", return_value=completed_record),
            patch("lambda_function.generate_presigned_url", side_effect=fake_presigned_url),
        ):
            from lambda_function import lambda_handler

            results_response = lambda_handler(
                {"job_id": job_id},
                _make_context("get_job_results"),
            )

        results_body = _parse_body(results_response)
        assert results_body["status"] == "COMPLETED"
        assert results_body["job_id"] == job_id
        assert results_body["artifact_count"] == len(completed_record["output_s3_keys"])
        assert len(results_body["artifacts"]) == len(completed_record["output_s3_keys"])

        # Every artifact has a presigned URL
        for artifact in results_body["artifacts"]:
            assert artifact["presigned_url"].startswith("https://presigned.example.com/")
            assert artifact["s3_key"] in completed_record["output_s3_keys"]

        # Image artifacts include markdown_image references (Req 3.5)
        png_artifacts = [
            a for a in results_body["artifacts"] if a["s3_key"].endswith(".png")
        ]
        assert len(png_artifacts) == 3
        for a in png_artifacts:
            assert "markdown_image" in a
            assert a["markdown_image"].startswith("![")
            assert "](https://presigned.example.com/" in a["markdown_image"]

        # Non-image artifacts (.h5ad, .html) must NOT carry markdown_image
        non_image_artifacts = [
            a for a in results_body["artifacts"]
            if not a["s3_key"].endswith((".png", ".jpg", ".jpeg", ".svg"))
        ]
        for a in non_image_artifacts:
            assert "markdown_image" not in a


# ---------------------------------------------------------------------------
# 5. list_datasets returns only .h5ad files
# ---------------------------------------------------------------------------

class TestListDatasetsFiltersH5ad:
    """list_datasets must only surface .h5ad files to the researcher."""

    def test_list_datasets_returns_only_h5ad(self):
        s3_response = {
            "Contents": [
                {
                    "Key": "datasets/pbmc_10k.h5ad",
                    "Size": 250_000_000,
                    "LastModified": datetime(2025, 7, 15, tzinfo=timezone.utc),
                },
                {
                    "Key": "datasets/readme.txt",
                    "Size": 1024,
                    "LastModified": datetime(2025, 7, 1, tzinfo=timezone.utc),
                },
                {
                    "Key": "datasets/metadata.csv",
                    "Size": 2048,
                    "LastModified": datetime(2025, 6, 28, tzinfo=timezone.utc),
                },
                {
                    "Key": "datasets/brain_spatial.h5ad",
                    "Size": 1_200_000_000,
                    "LastModified": datetime(2025, 7, 12, tzinfo=timezone.utc),
                },
                {
                    "Key": "datasets/notes.md",
                    "Size": 512,
                    "LastModified": datetime(2025, 7, 10, tzinfo=timezone.utc),
                },
            ],
            "IsTruncated": False,
        }

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _make_context("list_datasets"))

        body = _parse_body(response)

        assert body["status"] == "success"
        assert body["count"] == 2
        assert len(body["datasets"]) == 2

        returned_keys = {d["s3_key"] for d in body["datasets"]}
        assert returned_keys == {
            "datasets/pbmc_10k.h5ad",
            "datasets/brain_spatial.h5ad",
        }

        # All entries carry the fields a researcher sees in chat
        for d in body["datasets"]:
            assert d["file_name"].endswith(".h5ad")
            assert d["size_bytes"] > 0
            assert d["size_human"]  # non-empty human-readable size
            assert d["last_modified"]  # ISO timestamp string


# ---------------------------------------------------------------------------
# 6. Sync-to-async fallback on ComputeLambdaThrottled
# ---------------------------------------------------------------------------

class TestSyncToAsyncFallback:
    """A throttled sync Compute Lambda must fall back to the async path."""

    def test_throttled_compute_lambda_falls_back_to_async(self):
        # 100 MB → sync-eligible
        file_size = 100 * 1024 * 1024

        # Build a Lambda client that reports a throttling ClientError
        from botocore.exceptions import ClientError

        throttle_error = ClientError(
            error_response={
                "Error": {
                    "Code": "TooManyRequestsException",
                    "Message": "Rate exceeded",
                },
                "ResponseMetadata": {"HTTPStatusCode": 429},
            },
            operation_name="Invoke",
        )

        mock_lambda_client = MagicMock()
        mock_lambda_client.invoke.side_effect = throttle_error

        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with (
            patch("compute_router.get_file_size", return_value=file_size),
            patch("compute_router.lambda_client", mock_lambda_client),
            patch.dict(os.environ, {"COMPUTE_LAMBDA_ARN": "arn:aws:lambda:us-west-2:0:function:compute"}),
            patch("dynamodb_client.table", mock_table),
            patch("lambda_function.dispatch_to_fargate") as mock_dispatch_fargate,
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"input_s3_key": "datasets/pbmc_small.h5ad"},
                _make_context("run_scrna_qc"),
            )

        body = _parse_body(response)

        # Fallback must return a job_id — never propagate the throttle error
        assert body["status"] == "submitted"
        assert JOB_ID_PATTERN.match(body["job_id"]), (
            f"Fallback job_id '{body['job_id']}' does not match job-<8 hex chars>"
        )

        # A DynamoDB job record was written during fallback
        mock_table.put_item.assert_called_once()
        put_item = mock_table.put_item.call_args.kwargs["Item"]
        assert put_item["job_id"] == body["job_id"]
        assert put_item["status"] == "SUBMITTED"
        assert put_item["workflow_type"] == "qc"

        # Fargate dispatch was attempted
        mock_dispatch_fargate.assert_called_once()
