"""Unit tests for gateways.scverse.function.dynamodb_client."""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — patch boto3.resource before the module under test imports it
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _mock_dynamodb(monkeypatch):
    """Replace the module-level DynamoDB table with a MagicMock.

    Because ``dynamodb_client.py`` creates the ``table`` object at import
    time, we patch the already-imported module attribute directly.
    """
    mock_table = MagicMock()
    monkeypatch.setattr(
        "gateways.scverse.function.dynamodb_client.table", mock_table
    )
    return mock_table


@pytest.fixture()
def mock_table(_mock_dynamodb):
    """Convenience alias so tests can reference the mock table."""
    return _mock_dynamodb


# ---------------------------------------------------------------------------
# create_job_record
# ---------------------------------------------------------------------------
class TestCreateJobRecord:
    def test_calls_put_item_with_condition_expression(self, mock_table):
        from gateways.scverse.function.dynamodb_client import create_job_record

        result = create_job_record(
            job_id="job-abc12345",
            workflow_type="qc",
            input_s3_key="datasets/sample.h5ad",
            parameters={"min_cells": 20},
            compute_backend="LAMBDA",
        )

        mock_table.put_item.assert_called_once()
        call_kwargs = mock_table.put_item.call_args[1]
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(job_id)"

    def test_returned_item_has_required_keys(self, mock_table):
        from gateways.scverse.function.dynamodb_client import create_job_record

        result = create_job_record(
            job_id="job-abc12345",
            workflow_type="qc",
            input_s3_key="datasets/sample.h5ad",
            parameters={},
            compute_backend="LAMBDA",
        )

        required_keys = {
            "job_id",
            "workflow_type",
            "input_s3_key",
            "parameters",
            "status",
            "compute_backend",
            "submission_timestamp",
            "output_s3_keys",
            "ttl",
        }
        assert required_keys.issubset(result.keys())

    def test_status_is_submitted(self, mock_table):
        from gateways.scverse.function.dynamodb_client import create_job_record

        result = create_job_record(
            job_id="job-abc12345",
            workflow_type="qc",
            input_s3_key="datasets/sample.h5ad",
            parameters={},
            compute_backend="LAMBDA",
        )

        assert result["status"] == "SUBMITTED"

    def test_notification_email_included_when_provided(self, mock_table):
        from gateways.scverse.function.dynamodb_client import create_job_record

        result = create_job_record(
            job_id="job-abc12345",
            workflow_type="qc",
            input_s3_key="datasets/sample.h5ad",
            parameters={},
            compute_backend="LAMBDA",
            notification_email="user@example.com",
        )

        assert result["notification_email"] == "user@example.com"
        item_written = mock_table.put_item.call_args[1]["Item"]
        assert item_written["notification_email"] == "user@example.com"

    def test_notification_email_absent_when_none(self, mock_table):
        from gateways.scverse.function.dynamodb_client import create_job_record

        result = create_job_record(
            job_id="job-abc12345",
            workflow_type="qc",
            input_s3_key="datasets/sample.h5ad",
            parameters={},
            compute_backend="LAMBDA",
        )

        assert "notification_email" not in result

    def test_ttl_is_positive_integer(self, mock_table):
        from gateways.scverse.function.dynamodb_client import create_job_record

        result = create_job_record(
            job_id="job-abc12345",
            workflow_type="qc",
            input_s3_key="datasets/sample.h5ad",
            parameters={},
            compute_backend="LAMBDA",
        )

        assert isinstance(result["ttl"], int)
        assert result["ttl"] > 0

    def test_output_s3_keys_starts_empty(self, mock_table):
        from gateways.scverse.function.dynamodb_client import create_job_record

        result = create_job_record(
            job_id="job-abc12345",
            workflow_type="qc",
            input_s3_key="datasets/sample.h5ad",
            parameters={},
            compute_backend="LAMBDA",
        )

        assert result["output_s3_keys"] == []


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------
class TestGetJob:
    def test_returns_none_for_missing_job(self, mock_table):
        from gateways.scverse.function.dynamodb_client import get_job

        mock_table.get_item.return_value = {}

        assert get_job("job-nonexist") is None
        mock_table.get_item.assert_called_once_with(Key={"job_id": "job-nonexist"})

    def test_returns_item_for_existing_job(self, mock_table):
        from gateways.scverse.function.dynamodb_client import get_job

        expected_item = {
            "job_id": "job-abc12345",
            "status": "RUNNING",
            "workflow_type": "qc",
        }
        mock_table.get_item.return_value = {"Item": expected_item}

        result = get_job("job-abc12345")

        assert result == expected_item
        mock_table.get_item.assert_called_once_with(Key={"job_id": "job-abc12345"})


# ---------------------------------------------------------------------------
# update_job_status
# ---------------------------------------------------------------------------
class TestUpdateJobStatus:
    def test_status_only_update(self, mock_table):
        from gateways.scverse.function.dynamodb_client import update_job_status

        update_job_status("job-abc12345", status="RUNNING")

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]

        assert call_kwargs["Key"] == {"job_id": "job-abc12345"}
        assert ":status" in call_kwargs["ExpressionAttributeValues"]
        assert call_kwargs["ExpressionAttributeValues"][":status"] == "RUNNING"
        # Only status — no optional fields in the expression
        assert call_kwargs["UpdateExpression"] == "SET #st = :status"

    def test_full_update_includes_all_optional_fields(self, mock_table):
        from gateways.scverse.function.dynamodb_client import update_job_status

        update_job_status(
            "job-abc12345",
            status="COMPLETED",
            current_step_name="clustering",
            output_s3_keys=["s3://bucket/out1.h5ad"],
            error_message=None,  # explicitly None — should be excluded
            completion_timestamp="2025-01-01T00:00:00Z",
        )

        call_kwargs = mock_table.update_item.call_args[1]
        expr = call_kwargs["UpdateExpression"]
        values = call_kwargs["ExpressionAttributeValues"]
        names = call_kwargs["ExpressionAttributeNames"]

        # status is always present
        assert ":status" in values
        assert values[":status"] == "COMPLETED"

        # optional fields that were provided
        assert ":current_step_name" in values
        assert values[":current_step_name"] == "clustering"
        assert "#current_step_name" in names

        assert ":output_s3_keys" in values
        assert "#output_s3_keys" in names

        assert ":completion_timestamp" in values
        assert "#completion_timestamp" in names

        # error_message was None — should NOT appear
        assert ":error_message" not in values

    def test_update_with_error_fields(self, mock_table):
        from gateways.scverse.function.dynamodb_client import update_job_status

        update_job_status(
            "job-abc12345",
            status="FAILED",
            error_message="OutOfMemoryError",
            error_step="integration",
        )

        call_kwargs = mock_table.update_item.call_args[1]
        values = call_kwargs["ExpressionAttributeValues"]

        assert values[":status"] == "FAILED"
        assert values[":error_message"] == "OutOfMemoryError"
        assert values[":error_step"] == "integration"

    def test_update_with_heartbeat(self, mock_table):
        from gateways.scverse.function.dynamodb_client import update_job_status

        update_job_status(
            "job-abc12345",
            status="RUNNING",
            heartbeat_timestamp="2025-06-01T12:00:00Z",
        )

        call_kwargs = mock_table.update_item.call_args[1]
        values = call_kwargs["ExpressionAttributeValues"]

        assert values[":heartbeat_timestamp"] == "2025-06-01T12:00:00Z"

    def test_update_with_step_functions_arn(self, mock_table):
        from gateways.scverse.function.dynamodb_client import update_job_status

        arn = "arn:aws:states:us-east-1:123456789012:execution:pipeline:job-abc12345"
        update_job_status(
            "job-abc12345",
            status="RUNNING",
            step_functions_execution_arn=arn,
        )

        call_kwargs = mock_table.update_item.call_args[1]
        values = call_kwargs["ExpressionAttributeValues"]

        assert values[":step_functions_execution_arn"] == arn


# ---------------------------------------------------------------------------
# append_output_keys
# ---------------------------------------------------------------------------
class TestAppendOutputKeys:
    def test_uses_list_append_expression(self, mock_table):
        from gateways.scverse.function.dynamodb_client import append_output_keys

        append_output_keys("job-abc12345", ["s3://bucket/out1.h5ad", "s3://bucket/plot.png"])

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]

        assert call_kwargs["Key"] == {"job_id": "job-abc12345"}
        assert "list_append" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeValues"][":keys"] == [
            "s3://bucket/out1.h5ad",
            "s3://bucket/plot.png",
        ]
        assert call_kwargs["ExpressionAttributeNames"]["#keys"] == "output_s3_keys"

    def test_single_key_append(self, mock_table):
        from gateways.scverse.function.dynamodb_client import append_output_keys

        append_output_keys("job-abc12345", ["s3://bucket/result.h5ad"])

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":keys"] == [
            "s3://bucket/result.h5ad"
        ]
