"""Tests for the Lambda function router."""

import json
import os
import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("JOB_TABLE_NAME", "test-job-table")

from gateways.scverse.function.lambda_function import lambda_handler


class MockClientContext:
    def __init__(self, tool_name):
        self.custom = {"bedrockAgentCoreToolName": tool_name}


class MockContext:
    def __init__(self, tool_name):
        self.client_context = MockClientContext(tool_name)


class TestToolRouting:
    @patch("gateways.scverse.function.lambda_function.determine_compute_backend", side_effect=FileNotFoundError)
    def test_routes_to_scrna_qc_missing_file(self, mock_route):
        """QC returns an honest error when the dataset is not in S3 (no precomputed fallback)."""
        result = lambda_handler({}, MockContext("run_scrna_qc"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["status"] == "error"
        assert "No dataset found" in body["error"]

    def test_routes_to_scvi_analysis(self):
        """scvi model selection validates and returns the selected model."""
        result = lambda_handler({}, MockContext("run_scvi_analysis"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["tool"] == "run_scvi_analysis"
        assert body["status"] == "success"

    def test_routes_to_submit_job_missing_workflow(self):
        """Returns error when workflow is missing."""
        result = lambda_handler({}, MockContext("submit_scverse_job"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["status"] == "error"
        assert "workflow" in body["error"].lower()

    @patch("gateways.scverse.function.lambda_function.get_job", return_value=None)
    def test_routes_to_check_status_missing_job(self, mock_get):
        result = lambda_handler({"job_id": "test-123"}, MockContext("check_job_status"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["status"] == "error"
        assert "not found" in body["error"].lower()

    @patch("gateways.scverse.function.lambda_function.get_job")
    def test_routes_to_check_status_existing_job(self, mock_get):
        mock_get.return_value = {
            "job_id": "job-abc12345",
            "status": "RUNNING",
            "workflow_type": "qc",
            "compute_backend": "LAMBDA",
            "submission_timestamp": "2025-01-01T00:00:00Z",
        }
        result = lambda_handler({"job_id": "job-abc12345"}, MockContext("check_job_status"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["status"] == "RUNNING"
        assert body["job_id"] == "job-abc12345"

    @patch("gateways.scverse.function.lambda_function.get_job", return_value=None)
    def test_routes_to_get_results_missing_job(self, mock_get):
        result = lambda_handler({"job_id": "test-456"}, MockContext("get_job_results"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["status"] == "error"
        assert "not found" in body["error"].lower()

    @patch("gateways.scverse.function.lambda_function.generate_presigned_url", return_value="https://example.com/out.png")
    @patch("gateways.scverse.function.lambda_function.get_job")
    def test_routes_to_get_results_completed(self, mock_get, mock_url):
        mock_get.return_value = {
            "job_id": "job-abc12345",
            "status": "COMPLETED",
            "workflow_type": "qc",
            "output_s3_keys": ["scverse-jobs/job-abc12345/outputs/plot.png"],
        }
        result = lambda_handler({"job_id": "job-abc12345"}, MockContext("get_job_results"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["status"] == "COMPLETED"
        assert len(body["artifacts"]) == 1
        assert "markdown_image" in body["artifacts"][0]

    def test_unknown_tool_returns_400(self):
        result = lambda_handler({}, MockContext("nonexistent_tool"))
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "Unknown tool" in body["error"]

    @patch("gateways.scverse.function.lambda_function.determine_compute_backend", side_effect=FileNotFoundError)
    def test_strips_target_prefix(self, mock_route):
        """AgentCore Gateway prefixes tool names with target name + ___; the router strips it."""
        result = lambda_handler({}, MockContext("ScverseLambdaTarget___run_scrna_qc"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        # Routed to the QC handler (returns the file-not-found error) after stripping the prefix
        assert body["status"] == "error"
        assert "No dataset found" in body["error"]


class TestSubmitJob:
    @patch("gateways.scverse.function.lambda_function.dispatch_to_fargate")
    @patch("gateways.scverse.function.lambda_function.create_job_record")
    @patch("gateways.scverse.function.lambda_function.determine_compute_backend", return_value=("FARGATE", "async"))
    @patch("gateways.scverse.function.lambda_function.get_file_size", return_value=1_000_000_000)
    @patch("gateways.scverse.function.lambda_function.validate_workflow_type")
    def test_submit_job_success(self, mock_validate, mock_size, mock_route,
                                mock_create, mock_dispatch):
        event = {
            "workflow": "integration",
            "input_s3_key": "datasets/test.h5ad",
            "parameters": {"model": "scvi"},
        }
        result = lambda_handler(event, MockContext("submit_scverse_job"))
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["status"] == "submitted"
        assert "job_id" in body
        assert body["compute_backend"] == "FARGATE"
        mock_create.assert_called_once()
        mock_dispatch.assert_called_once()


class TestGetResults:
    @patch("gateways.scverse.function.lambda_function.get_job")
    def test_submitted_job_returns_queued(self, mock_get):
        mock_get.return_value = {
            "job_id": "job-abc12345",
            "status": "SUBMITTED",
        }
        result = lambda_handler({"job_id": "job-abc12345"}, MockContext("get_job_results"))
        body = json.loads(result["body"])
        assert body["status"] == "SUBMITTED"
        assert "queued" in body["message"].lower()

    @patch("gateways.scverse.function.lambda_function.get_job")
    def test_failed_job_returns_error_info(self, mock_get):
        mock_get.return_value = {
            "job_id": "job-abc12345",
            "status": "FAILED",
            "workflow_type": "qc",
            "error_message": "Out of memory",
            "error_step": "qc",
            "output_s3_keys": [],
        }
        result = lambda_handler({"job_id": "job-abc12345"}, MockContext("get_job_results"))
        body = json.loads(result["body"])
        assert body["status"] == "FAILED"
        assert body["error_message"] == "Out of memory"


class TestListDatasets:
    @patch("gateways.scverse.function.lambda_function.s3_client")
    def test_list_datasets_filters_h5ad(self, mock_s3):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "datasets/test.h5ad", "Size": 1000, "LastModified": MagicMock(isoformat=lambda: "2025-01-01T00:00:00Z")},
                {"Key": "datasets/readme.txt", "Size": 100, "LastModified": MagicMock(isoformat=lambda: "2025-01-01T00:00:00Z")},
            ],
            "IsTruncated": False,
        }
        result = lambda_handler({}, MockContext("list_datasets"))
        body = json.loads(result["body"])
        assert body["status"] == "success"
        assert body["count"] == 1
        assert body["datasets"][0]["file_name"] == "test.h5ad"
