"""
Unit tests for gateway routing and job result retrieval.

Covers:
- Sync path: small file → Lambda invoke → inline results
- Async path: large file → DynamoDB + ECS → job_id
- handle_check_status: existing job, missing job
- handle_get_results: SUBMITTED, RUNNING, COMPLETED, FAILED statuses
- handle_list_datasets: filtered .h5ad files from S3

Requirements: 1.2, 1.3, 1.4, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4
"""

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(tool_name: str) -> MagicMock:
    """Build a mock Lambda context with the AgentCore tool name."""
    ctx = MagicMock()
    ctx.client_context.custom = {"bedrockAgentCoreToolName": tool_name}
    return ctx


# ---------------------------------------------------------------------------
# Sync path — small file routed to Compute Lambda
# ---------------------------------------------------------------------------

class TestSyncPath:
    """File < 500 MB → dispatch_to_compute_lambda → inline QC results."""

    def test_sync_returns_inline_qc_results(self):
        qc_result = {
            "status": "success",
            "qc_summary": {
                "before_filtering": {"n_cells": 2700, "n_genes": 32738},
                "after_filtering": {"n_cells": 2691, "n_genes": 9713},
            },
            "plots": {"qc_before": {"markdown_image": "![plot](https://url)"}},
        }

        with (
            patch("lambda_function.determine_compute_backend", return_value=("LAMBDA", "sync")),
            patch("lambda_function.dispatch_to_compute_lambda", return_value=qc_result),
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"input_s3_key": "datasets/small.h5ad", "species": "human"},
                _make_context("run_scrna_qc"),
            )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "success"
        assert "qc_summary" in body
        assert body["qc_summary"]["before_filtering"]["n_cells"] == 2700

    def test_sync_path_does_not_create_job_record(self):
        with (
            patch("lambda_function.determine_compute_backend", return_value=("LAMBDA", "sync")),
            patch("lambda_function.dispatch_to_compute_lambda", return_value={"status": "success"}),
            patch("lambda_function.create_job_record") as mock_create,
        ):
            from lambda_function import lambda_handler

            lambda_handler(
                {"input_s3_key": "datasets/small.h5ad"},
                _make_context("run_scrna_qc"),
            )

        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Async path — large file routed to Fargate
# ---------------------------------------------------------------------------

class TestAsyncPath:
    """File >= 500 MB → create job record + dispatch to Fargate → job_id."""

    def test_async_returns_job_id(self):
        with (
            patch("lambda_function.determine_compute_backend", return_value=("FARGATE", "async")),
            patch("lambda_function.create_job_record") as mock_create,
            patch("lambda_function.dispatch_to_fargate") as mock_fargate,
            patch("lambda_function.generate_job_id", return_value="job-async001"),
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"input_s3_key": "datasets/large.h5ad"},
                _make_context("run_scrna_qc"),
            )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "submitted"
        assert body["job_id"] == "job-async001"
        mock_create.assert_called_once()
        mock_fargate.assert_called_once()

    def test_async_lambda_mode_creates_job_and_dispatches(self):
        """LAMBDA async (500 MB–2 GB) still creates a job record."""
        with (
            patch("lambda_function.determine_compute_backend", return_value=("LAMBDA", "async")),
            patch("lambda_function.create_job_record") as mock_create,
            patch("lambda_function.dispatch_to_fargate") as mock_fargate,
            patch("lambda_function.generate_job_id", return_value="job-mid0001"),
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"input_s3_key": "datasets/medium.h5ad"},
                _make_context("run_scrna_qc"),
            )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["job_id"] == "job-mid0001"
        mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# handle_check_status
# ---------------------------------------------------------------------------

class TestCheckStatus:
    def test_existing_job_returns_status(self):
        job_record = {
            "job_id": "job-stat0001",
            "status": "RUNNING",
            "workflow_type": "qc",
            "compute_backend": "FARGATE",
            "submission_timestamp": "2025-06-01T10:00:00Z",
            "current_step_name": "filtering",
        }

        with patch("lambda_function.get_job", return_value=job_record):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-stat0001"},
                _make_context("check_job_status"),
            )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "RUNNING"
        assert body["workflow_type"] == "qc"
        assert body["compute_backend"] == "FARGATE"
        assert body["current_step_name"] == "filtering"
        assert "elapsed_seconds" in body

    def test_missing_job_returns_error(self):
        with patch("lambda_function.get_job", return_value=None):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-missing1"},
                _make_context("check_job_status"),
            )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "error"
        assert "not found" in body["error"].lower()

    def test_missing_job_id_field_returns_error(self):
        from lambda_function import lambda_handler

        response = lambda_handler({}, _make_context("check_job_status"))

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "error"
        assert "job_id" in body["error"].lower()

    def test_failed_job_includes_error_details(self):
        job_record = {
            "job_id": "job-fail0001",
            "status": "FAILED",
            "workflow_type": "integration",
            "compute_backend": "FARGATE",
            "submission_timestamp": "2025-06-01T10:00:00Z",
            "error_message": "OutOfMemoryError",
            "error_step": "scvi_train",
        }

        with patch("lambda_function.get_job", return_value=job_record):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-fail0001"},
                _make_context("check_job_status"),
            )

        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert body["error_message"] == "OutOfMemoryError"
        assert body["error_step"] == "scvi_train"


# ---------------------------------------------------------------------------
# handle_get_results
# ---------------------------------------------------------------------------

class TestGetResults:
    def test_submitted_job_returns_queued_message(self):
        job_record = {
            "job_id": "job-sub00001",
            "status": "SUBMITTED",
            "workflow_type": "qc",
        }

        with patch("lambda_function.get_job", return_value=job_record):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-sub00001"},
                _make_context("get_job_results"),
            )

        body = json.loads(response["body"])
        assert body["status"] == "SUBMITTED"
        assert "queued" in body["message"].lower() or "waiting" in body["message"].lower()

    def test_running_job_returns_progress(self):
        job_record = {
            "job_id": "job-run00001",
            "status": "RUNNING",
            "workflow_type": "qc",
            "submission_timestamp": "2025-06-01T10:00:00Z",
            "current_step_name": "normalization",
        }

        with patch("lambda_function.get_job", return_value=job_record):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-run00001"},
                _make_context("get_job_results"),
            )

        body = json.loads(response["body"])
        assert body["status"] == "RUNNING"
        assert body["current_step_name"] == "normalization"
        assert "elapsed_seconds" in body

    def test_completed_job_returns_presigned_urls(self):
        job_record = {
            "job_id": "job-done0001",
            "status": "COMPLETED",
            "workflow_type": "qc",
            "output_s3_keys": [
                "scverse-jobs/job-done0001/outputs/filtered.h5ad",
                "scverse-jobs/job-done0001/outputs/qc_violin.png",
            ],
        }

        with (
            patch("lambda_function.get_job", return_value=job_record),
            patch(
                "lambda_function.generate_presigned_url",
                side_effect=lambda b, k: f"https://presigned/{k}",
            ),
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-done0001"},
                _make_context("get_job_results"),
            )

        body = json.loads(response["body"])
        assert body["status"] == "COMPLETED"
        assert body["artifact_count"] == 2
        assert len(body["artifacts"]) == 2

        # The .png artifact should have a markdown_image
        png_artifact = next(a for a in body["artifacts"] if a["s3_key"].endswith(".png"))
        assert "markdown_image" in png_artifact
        assert "![" in png_artifact["markdown_image"]

        # The .h5ad artifact should have a presigned_url but no markdown_image
        h5ad_artifact = next(a for a in body["artifacts"] if a["s3_key"].endswith(".h5ad"))
        assert "presigned_url" in h5ad_artifact
        assert "markdown_image" not in h5ad_artifact

    def test_completed_job_limits_inline_images(self):
        """Only MAX_INLINE_IMAGES plots get markdown_image entries."""
        output_keys = [
            f"scverse-jobs/job-img0001/outputs/plot_{i}.png" for i in range(25)
        ]
        job_record = {
            "job_id": "job-img0001",
            "status": "COMPLETED",
            "workflow_type": "qc",
            "output_s3_keys": output_keys,
        }

        with (
            patch("lambda_function.get_job", return_value=job_record),
            patch(
                "lambda_function.generate_presigned_url",
                side_effect=lambda b, k: f"https://presigned/{k}",
            ),
            patch("lambda_function.MAX_INLINE_IMAGES", 20),
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-img0001"},
                _make_context("get_job_results"),
            )

        body = json.loads(response["body"])
        images_with_markdown = [a for a in body["artifacts"] if "markdown_image" in a]
        assert len(images_with_markdown) == 20

    def test_failed_job_returns_error_and_partial_outputs(self):
        job_record = {
            "job_id": "job-fail0001",
            "status": "FAILED",
            "workflow_type": "integration",
            "error_message": "OutOfMemoryError during scVI training",
            "error_step": "scvi_train",
            "output_s3_keys": [
                "scverse-jobs/job-fail0001/outputs/partial_qc.h5ad",
            ],
        }

        with (
            patch("lambda_function.get_job", return_value=job_record),
            patch(
                "lambda_function.generate_presigned_url",
                side_effect=lambda b, k: f"https://presigned/{k}",
            ),
        ):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-fail0001"},
                _make_context("get_job_results"),
            )

        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert body["error_message"] == "OutOfMemoryError during scVI training"
        assert body["error_step"] == "scvi_train"
        assert len(body["partial_artifacts"]) == 1

    def test_failed_job_without_outputs(self):
        job_record = {
            "job_id": "job-fail0002",
            "status": "FAILED",
            "workflow_type": "qc",
            "error_message": "Input file corrupt",
            "output_s3_keys": [],
        }

        with patch("lambda_function.get_job", return_value=job_record):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-fail0002"},
                _make_context("get_job_results"),
            )

        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert "partial_artifacts" not in body

    def test_missing_job_returns_error(self):
        with patch("lambda_function.get_job", return_value=None):
            from lambda_function import lambda_handler

            response = lambda_handler(
                {"job_id": "job-ghost001"},
                _make_context("get_job_results"),
            )

        body = json.loads(response["body"])
        assert body["status"] == "error"
        assert "not found" in body["error"].lower()


# ---------------------------------------------------------------------------
# handle_list_datasets
# ---------------------------------------------------------------------------

class TestListDatasets:
    def test_returns_only_h5ad_files(self):
        s3_response = {
            "Contents": [
                {"Key": "datasets/pbmc3k.h5ad", "Size": 100_000_000, "LastModified": datetime(2025, 6, 1, tzinfo=timezone.utc)},
                {"Key": "datasets/readme.txt", "Size": 1024, "LastModified": datetime(2025, 5, 1, tzinfo=timezone.utc)},
                {"Key": "datasets/heart_10x.h5ad", "Size": 500_000_000, "LastModified": datetime(2025, 6, 10, tzinfo=timezone.utc)},
                {"Key": "datasets/metadata.csv", "Size": 2048, "LastModified": datetime(2025, 4, 1, tzinfo=timezone.utc)},
            ],
            "IsTruncated": False,
        }

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _make_context("list_datasets"))

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "success"
        assert body["count"] == 2

        file_names = [d["file_name"] for d in body["datasets"]]
        assert "pbmc3k.h5ad" in file_names
        assert "heart_10x.h5ad" in file_names
        assert "readme.txt" not in file_names
        assert "metadata.csv" not in file_names

    def test_datasets_sorted_by_last_modified_descending(self):
        s3_response = {
            "Contents": [
                {"Key": "datasets/old.h5ad", "Size": 100, "LastModified": datetime(2025, 1, 1, tzinfo=timezone.utc)},
                {"Key": "datasets/new.h5ad", "Size": 200, "LastModified": datetime(2025, 6, 15, tzinfo=timezone.utc)},
                {"Key": "datasets/mid.h5ad", "Size": 300, "LastModified": datetime(2025, 3, 1, tzinfo=timezone.utc)},
            ],
            "IsTruncated": False,
        }

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _make_context("list_datasets"))

        body = json.loads(response["body"])
        names = [d["file_name"] for d in body["datasets"]]
        assert names == ["new.h5ad", "mid.h5ad", "old.h5ad"]

    def test_includes_human_readable_size(self):
        s3_response = {
            "Contents": [
                {"Key": "datasets/sample.h5ad", "Size": 500 * 1024 * 1024, "LastModified": datetime(2025, 6, 1, tzinfo=timezone.utc)},
            ],
            "IsTruncated": False,
        }

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _make_context("list_datasets"))

        body = json.loads(response["body"])
        assert body["datasets"][0]["size_human"] == "500.0 MB"

    def test_pagination_token_forwarded(self):
        s3_response = {
            "Contents": [
                {"Key": "datasets/a.h5ad", "Size": 100, "LastModified": datetime(2025, 6, 1, tzinfo=timezone.utc)},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "token-abc123",
        }

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _make_context("list_datasets"))

        body = json.loads(response["body"])
        assert body["next_token"] == "token-abc123"

    def test_no_pagination_when_not_truncated(self):
        s3_response = {
            "Contents": [
                {"Key": "datasets/a.h5ad", "Size": 100, "LastModified": datetime(2025, 6, 1, tzinfo=timezone.utc)},
            ],
            "IsTruncated": False,
        }

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _make_context("list_datasets"))

        body = json.loads(response["body"])
        assert body["next_token"] is None

    def test_empty_bucket_returns_zero_datasets(self):
        s3_response = {"Contents": [], "IsTruncated": False}

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _make_context("list_datasets"))

        body = json.loads(response["body"])
        assert body["count"] == 0
        assert body["datasets"] == []

    def test_prefix_filter_passed_to_s3(self):
        s3_response = {"Contents": [], "IsTruncated": False}

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            lambda_handler(
                {"prefix_filter": "pbmc"},
                _make_context("list_datasets"),
            )

        call_kwargs = mock_s3.list_objects_v2.call_args[1]
        assert call_kwargs["Prefix"] == "datasets/pbmc"
