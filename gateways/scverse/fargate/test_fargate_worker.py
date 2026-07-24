"""Unit tests for gateways.scverse.fargate.fargate_worker.

All scverse libraries, workflow imports, and AWS clients are mocked
since they are not installed in the test environment.

Validates: Requirements 7.1, 7.3, 7.4, 7.5, 7.6
"""

import json
import sys
import threading
import types
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Mock heavy third-party modules before importing the worker
# ---------------------------------------------------------------------------
_mock_jinja2 = MagicMock()
_mock_jinja2.Template.return_value.render.return_value = "<html>report</html>"

# Create a fake workflows package with all expected handlers
_mock_workflows = types.ModuleType("workflows")
_mock_workflows.run_qc_workflow = MagicMock(return_value={"steps": [], "metrics": {}})
_mock_workflows.run_integration_workflow = MagicMock(return_value={"steps": [], "metrics": {}})
_mock_workflows.run_clustering_workflow = MagicMock(return_value={"steps": [], "metrics": {}})
_mock_workflows.run_de_workflow = MagicMock(return_value={"steps": [], "metrics": {}})
_mock_workflows.run_spatial_workflow = MagicMock(return_value={"steps": [], "metrics": {}})
_mock_workflows.run_annotation_workflow = MagicMock(return_value={"steps": [], "metrics": {}})
_mock_workflows.run_velocity_workflow = MagicMock(return_value={"steps": [], "metrics": {}})
_mock_workflows.run_perturbation_workflow = MagicMock(return_value={"steps": [], "metrics": {}})

# Also create sub-modules so `from workflows.qc_workflow import ...` works
for _name in (
    "workflows.qc_workflow",
    "workflows.integration_workflow",
    "workflows.clustering_workflow",
    "workflows.de_workflow",
    "workflows.spatial_workflow",
    "workflows.annotation_workflow",
    "workflows.velocity_workflow",
    "workflows.perturbation_workflow",
):
    sys.modules[_name] = types.ModuleType(_name)

sys.modules["workflows"] = _mock_workflows
sys.modules["jinja2"] = _mock_jinja2

# Now import the module under test
import gateways.scverse.fargate.fargate_worker as fw  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_job_table():
    """Reset the lazy-init _job_table before each test."""
    fw._job_table = None
    yield
    fw._job_table = None


@pytest.fixture()
def mock_s3():
    """Patch the module-level s3_client."""
    with patch.object(fw, "s3_client") as m:
        yield m


@pytest.fixture()
def mock_dynamodb():
    """Patch the module-level _dynamodb resource and return a mock table."""
    mock_table = MagicMock()
    with patch.object(fw, "_dynamodb") as mock_resource:
        mock_resource.Table.return_value = mock_table
        yield mock_table


@pytest.fixture()
def env_vars(monkeypatch):
    """Set required environment variables for main()."""
    monkeypatch.setenv("JOB_MANIFEST_S3_KEY", "scverse-jobs/job-abc12345/manifest.json")
    monkeypatch.setenv("STORAGE_BUCKET", "test-bucket")
    monkeypatch.setenv("JOB_TABLE_NAME", "test-job-table")


@pytest.fixture()
def sample_manifest():
    """Return a valid job manifest dict."""
    return {
        "job_id": "job-abc12345",
        "workflow_type": "qc",
        "input_s3_key": "datasets/sample.h5ad",
        "parameters": {"min_cells": 20},
        "output_prefix": "scverse-jobs/job-abc12345/outputs/",
        "notification_email": "user@example.com",
    }


# ---------------------------------------------------------------------------
# TestUpdateJobStatus
# ---------------------------------------------------------------------------
class TestUpdateJobStatus:
    def test_status_only(self, mock_dynamodb, monkeypatch):
        monkeypatch.setenv("JOB_TABLE_NAME", "test-table")
        fw.update_job_status("job-abc12345", status="RUNNING")

        mock_dynamodb.update_item.assert_called_once()
        kwargs = mock_dynamodb.update_item.call_args[1]
        assert kwargs["Key"] == {"job_id": "job-abc12345"}
        assert kwargs["ExpressionAttributeValues"][":status"] == "RUNNING"

    def test_no_op_when_all_none(self, mock_dynamodb, monkeypatch):
        monkeypatch.setenv("JOB_TABLE_NAME", "test-table")
        fw.update_job_status("job-abc12345")
        mock_dynamodb.update_item.assert_not_called()

    def test_includes_optional_fields(self, mock_dynamodb, monkeypatch):
        monkeypatch.setenv("JOB_TABLE_NAME", "test-table")
        fw.update_job_status(
            "job-abc12345",
            status="FAILED",
            error_message="OutOfMemory",
            completion_timestamp="2025-01-01T00:00:00Z",
        )
        kwargs = mock_dynamodb.update_item.call_args[1]
        values = kwargs["ExpressionAttributeValues"]
        assert values[":status"] == "FAILED"
        assert values[":error_message"] == "OutOfMemory"
        assert values[":completion_timestamp"] == "2025-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# TestAppendOutputKeys
# ---------------------------------------------------------------------------
class TestAppendOutputKeys:
    def test_uses_list_append(self, mock_dynamodb, monkeypatch):
        monkeypatch.setenv("JOB_TABLE_NAME", "test-table")
        fw.append_output_keys("job-abc12345", ["s3://bucket/out.h5ad"])

        kwargs = mock_dynamodb.update_item.call_args[1]
        assert "list_append" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeValues"][":keys"] == ["s3://bucket/out.h5ad"]
        assert kwargs["ExpressionAttributeNames"]["#keys"] == "output_s3_keys"


# ---------------------------------------------------------------------------
# TestHeartbeatLoop
# ---------------------------------------------------------------------------
class TestHeartbeatLoop:
    def test_updates_dynamodb_at_interval(self, mock_dynamodb, monkeypatch):
        """Heartbeat loop should call update_job_status with heartbeat_timestamp."""
        monkeypatch.setenv("JOB_TABLE_NAME", "test-table")

        call_count = 0

        original_sleep = None

        def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise StopIteration("stop the loop")

        with patch("gateways.scverse.fargate.fargate_worker.time.sleep", side_effect=fake_sleep):
            with pytest.raises(StopIteration):
                fw.heartbeat_loop("job-abc12345", interval=30)

        # Should have called update_item at least twice (once before each sleep)
        assert mock_dynamodb.update_item.call_count >= 2

        # Verify heartbeat_timestamp is set in each call
        for c in mock_dynamodb.update_item.call_args_list:
            kwargs = c[1]
            assert ":heartbeat_timestamp" in kwargs["ExpressionAttributeValues"]
            ts = kwargs["ExpressionAttributeValues"][":heartbeat_timestamp"]
            assert ts.endswith("Z")

    def test_continues_on_exception(self, mock_dynamodb, monkeypatch):
        """Heartbeat loop should log and continue if update fails."""
        monkeypatch.setenv("JOB_TABLE_NAME", "test-table")

        call_count = 0
        mock_dynamodb.update_item.side_effect = [
            Exception("transient error"),  # first call fails
            None,                          # second call succeeds
        ]

        def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise StopIteration("stop")

        with patch("gateways.scverse.fargate.fargate_worker.time.sleep", side_effect=fake_sleep):
            with pytest.raises(StopIteration):
                fw.heartbeat_loop("job-abc12345", interval=30)

        # Both calls should have been attempted despite the first one failing
        assert mock_dynamodb.update_item.call_count == 2


# ---------------------------------------------------------------------------
# TestRunWorkflow
# ---------------------------------------------------------------------------
class TestRunWorkflow:
    def test_routes_to_correct_handler(self, tmp_path):
        input_path = str(tmp_path / "input.h5ad")
        (tmp_path / "input.h5ad").touch()

        _mock_workflows.run_qc_workflow.reset_mock()
        _mock_workflows.run_qc_workflow.return_value = {"steps": [], "metrics": {}}

        result = fw.run_workflow("job-abc12345", "qc", input_path, {"min_cells": 20})

        _mock_workflows.run_qc_workflow.assert_called_once()
        call_kwargs = _mock_workflows.run_qc_workflow.call_args[1]
        assert call_kwargs["input_path"] == input_path
        assert call_kwargs["job_id"] == "job-abc12345"

    def test_raises_for_unsupported_workflow(self, tmp_path):
        input_path = str(tmp_path / "input.h5ad")
        with pytest.raises(ValueError, match="Unsupported workflow_type 'invalid'"):
            fw.run_workflow("job-abc12345", "invalid", input_path, {})


# ---------------------------------------------------------------------------
# TestArtifactType
# ---------------------------------------------------------------------------
class TestArtifactType:
    @pytest.mark.parametrize(
        "key, expected",
        [
            ("outputs/plot.png", "plot"),
            ("outputs/data.h5ad", "data"),
            ("outputs/report.html", "report"),
            ("outputs/metrics.json", "metrics"),
            ("outputs/readme.txt", "other"),
        ],
    )
    def test_infers_type_from_extension(self, key, expected):
        assert fw._artifact_type(key) == expected



# ---------------------------------------------------------------------------
# TestMain — happy path and failure path
# ---------------------------------------------------------------------------
class TestMain:
    """Tests for the main() Fargate entry point."""

    def _setup_s3_manifest(self, mock_s3, manifest_dict):
        """Configure mock S3 to return the given manifest on get_object."""
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(manifest_dict).encode("utf-8")
        mock_s3.get_object.return_value = {"Body": body_mock}

    def test_happy_path_full_sequence(
        self, mock_s3, mock_dynamodb, env_vars, sample_manifest, tmp_path, monkeypatch
    ):
        """Verify: manifest download → RUNNING → workflow → outputs uploaded → COMPLETED.

        CRITICAL: append_output_keys is called BEFORE update_job_status(COMPLETED)
        per Requirement 7.3.
        """
        self._setup_s3_manifest(mock_s3, sample_manifest)

        # Make tempfile.mkdtemp return a controlled directory
        work_dir = str(tmp_path / "job-abc12345_work")
        import os
        os.makedirs(work_dir, exist_ok=True)
        monkeypatch.setattr("tempfile.mkdtemp", lambda prefix="": work_dir)

        # Create a fake output file so glob finds something to upload
        output_dir = os.path.join(work_dir, "outputs")
        os.makedirs(output_dir, exist_ok=True)
        fake_output = os.path.join(output_dir, "filtered.h5ad")
        with open(fake_output, "w") as f:
            f.write("fake data")

        # Mock the workflow to return a result and create an output file
        def fake_workflow(input_path, params, output_dir, job_id):
            return {"steps": [{"name": "qc", "duration_seconds": 5.0}], "metrics": {"n_cells": 100}}

        _mock_workflows.run_qc_workflow.reset_mock()
        _mock_workflows.run_qc_workflow.side_effect = fake_workflow

        # Mock _get_scverse_versions to avoid importing real packages
        monkeypatch.setattr(fw, "_get_scverse_versions", lambda: {"scanpy": "1.10.1"})

        # Mock threading to avoid starting real heartbeat thread
        mock_thread = MagicMock()
        monkeypatch.setattr("threading.Thread", lambda **kwargs: mock_thread)

        # Track DynamoDB call order to verify Req 7.3
        ddb_calls = []

        def track_update(**kwargs):
            expr = kwargs.get("UpdateExpression", "")
            if "list_append" in expr:
                ddb_calls.append("append_output_keys")
            elif ":status" in kwargs.get("ExpressionAttributeValues", {}):
                status_val = kwargs["ExpressionAttributeValues"].get(":status", "")
                ddb_calls.append(f"update_status_{status_val}")
            else:
                ddb_calls.append("update_other")

        mock_dynamodb.update_item.side_effect = track_update

        # Run main
        fw.main()

        # --- Verify manifest was downloaded from S3 ---
        mock_s3.get_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="scverse-jobs/job-abc12345/manifest.json",
        )

        # --- Verify heartbeat thread was started ---
        mock_thread.start.assert_called_once()

        # --- Verify input .h5ad was downloaded ---
        mock_s3.download_file.assert_called_once_with(
            "test-bucket", "datasets/sample.h5ad", os.path.join(work_dir, "input.h5ad")
        )

        # --- Verify workflow was called ---
        _mock_workflows.run_qc_workflow.assert_called_once()

        # --- Verify outputs were uploaded ---
        assert mock_s3.upload_file.call_count >= 1

        # --- Verify output manifest was written to S3 ---
        put_calls = [c for c in mock_s3.put_object.call_args_list]
        assert len(put_calls) == 1
        manifest_body = json.loads(put_calls[0][1]["Body"])
        assert manifest_body["job_id"] == "job-abc12345"
        assert "artifacts" in manifest_body
        assert "scverse_versions" in manifest_body

        # --- CRITICAL: Verify append_output_keys BEFORE COMPLETED (Req 7.3) ---
        append_idx = None
        completed_idx = None
        for i, c in enumerate(ddb_calls):
            if c == "append_output_keys" and append_idx is None:
                append_idx = i
            if c == "update_status_COMPLETED" and completed_idx is None:
                completed_idx = i

        assert append_idx is not None, "append_output_keys was never called"
        assert completed_idx is not None, "update_job_status(COMPLETED) was never called"
        assert append_idx < completed_idx, (
            f"Req 7.3 violation: append_output_keys (call {append_idx}) must come "
            f"before update_job_status(COMPLETED) (call {completed_idx}). "
            f"Actual call order: {ddb_calls}"
        )

        # --- Verify RUNNING was set before COMPLETED ---
        running_idx = None
        for i, c in enumerate(ddb_calls):
            if c == "update_status_RUNNING":
                running_idx = i
                break
        assert running_idx is not None, "Status was never set to RUNNING"
        assert running_idx < completed_idx, "RUNNING must come before COMPLETED"

    def test_failure_path_sets_failed_with_error(
        self, mock_s3, mock_dynamodb, env_vars, sample_manifest, tmp_path, monkeypatch
    ):
        """When workflow raises an exception, status should be FAILED with error_message."""
        self._setup_s3_manifest(mock_s3, sample_manifest)

        work_dir = str(tmp_path / "job-abc12345_work")
        import os
        os.makedirs(work_dir, exist_ok=True)
        monkeypatch.setattr("tempfile.mkdtemp", lambda prefix="": work_dir)

        # Mock threading
        mock_thread = MagicMock()
        monkeypatch.setattr("threading.Thread", lambda **kwargs: mock_thread)

        # Make the workflow raise an exception
        _mock_workflows.run_qc_workflow.reset_mock()
        _mock_workflows.run_qc_workflow.side_effect = RuntimeError("OutOfMemoryError: dataset too large")

        # main() should call sys.exit(1) on failure
        with pytest.raises(SystemExit) as exc_info:
            fw.main()
        assert exc_info.value.code == 1

        # Verify FAILED status was written with error_message
        update_calls = mock_dynamodb.update_item.call_args_list
        failed_call = None
        for c in update_calls:
            kwargs = c[1]
            vals = kwargs.get("ExpressionAttributeValues", {})
            if vals.get(":status") == "FAILED":
                failed_call = kwargs
                break

        assert failed_call is not None, "update_job_status(FAILED) was never called"
        assert "OutOfMemoryError" in failed_call["ExpressionAttributeValues"][":error_message"]
        assert ":completion_timestamp" in failed_call["ExpressionAttributeValues"]


# ---------------------------------------------------------------------------
# TestOutputManifestStructure
# ---------------------------------------------------------------------------
class TestOutputManifestStructure:
    """Verify the output manifest JSON structure matches the design spec."""

    def test_manifest_has_required_fields(self):
        """Output manifest must have job_id, artifacts list, and scverse_versions."""
        # Simulate what main() builds for the output manifest
        output_s3_keys = [
            "scverse-jobs/job-abc12345/outputs/filtered.h5ad",
            "scverse-jobs/job-abc12345/outputs/qc_violin.png",
            "scverse-jobs/job-abc12345/outputs/report.html",
            "scverse-jobs/job-abc12345/outputs/metrics.json",
        ]

        output_manifest = {
            "job_id": "job-abc12345",
            "artifacts": [
                {
                    "s3_key": key,
                    "type": fw._artifact_type(key),
                    "description": key.rsplit("/", 1)[-1],
                }
                for key in output_s3_keys
            ],
            "scverse_versions": {"scanpy": "1.10.1"},
        }

        # Verify top-level keys
        assert "job_id" in output_manifest
        assert "artifacts" in output_manifest
        assert "scverse_versions" in output_manifest

        # Verify artifacts structure
        assert isinstance(output_manifest["artifacts"], list)
        assert len(output_manifest["artifacts"]) == 4

        for artifact in output_manifest["artifacts"]:
            assert "s3_key" in artifact
            assert "type" in artifact
            assert "description" in artifact

        # Verify artifact types are correctly inferred
        types_found = {a["type"] for a in output_manifest["artifacts"]}
        assert types_found == {"data", "plot", "report", "metrics"}

    def test_manifest_serializes_to_valid_json(self):
        """Output manifest must be valid JSON."""
        manifest = {
            "job_id": "job-abc12345",
            "artifacts": [
                {"s3_key": "outputs/plot.png", "type": "plot", "description": "plot.png"}
            ],
            "scverse_versions": {"scanpy": "1.10.1", "anndata": "0.10.7"},
        }

        serialized = json.dumps(manifest, indent=2)
        deserialized = json.loads(serialized)
        assert deserialized == manifest
