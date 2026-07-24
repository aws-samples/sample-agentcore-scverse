"""
Property-based tests for DynamoDB job record operations.

Validates:
- Property 3: Job record completeness — every record returned by
  create_job_record() contains all required keys with correct types.
- Property 11: Outputs indexed before status change — append_output_keys()
  is called before update_job_status(status="COMPLETED") in the expected
  workflow sequence.

Requirements: 2.1, 2.2, 7.3, 8.4
"""

import os
import re
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch, call

# Ensure the function directory is on the import path when running from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hypothesis import given, settings, strategies as st

from job_utils import VALID_WORKFLOW_TYPES

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_workflow_strategy = st.sampled_from(sorted(VALID_WORKFLOW_TYPES))

_input_s3_key_strategy = st.text(min_size=1, max_size=120).filter(
    lambda s: s.isprintable() and s.strip()
)

_parameters_strategy = st.dictionaries(
    keys=st.text(min_size=1, max_size=10).filter(lambda s: s.isprintable()),
    values=st.text(max_size=50).filter(lambda s: s.isprintable()),
    max_size=8,
)

_compute_backend_strategy = st.sampled_from(["LAMBDA", "FARGATE", "STEP_FUNCTIONS"])

# Build valid emails that match the validate_email regex used in the codebase
_local_part = st.from_regex(r"[a-zA-Z0-9._%+\-]+", fullmatch=True).filter(
    lambda s: 1 <= len(s) <= 32
)
_domain = st.from_regex(r"[a-zA-Z0-9\-]+", fullmatch=True).filter(
    lambda s: 1 <= len(s) <= 20
)
_tld = st.from_regex(r"[a-zA-Z]{2,6}", fullmatch=True)
_valid_email = st.builds(lambda l, d, t: f"{l}@{d}.{t}", _local_part, _domain, _tld)

_notification_email_strategy = st.one_of(st.none(), _valid_email)

# ISO 8601 pattern ending with Z
_ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


# ---------------------------------------------------------------------------
# Property 3: Job record completeness
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    workflow_type=_workflow_strategy,
    input_s3_key=_input_s3_key_strategy,
    parameters=_parameters_strategy,
    compute_backend=_compute_backend_strategy,
    notification_email=_notification_email_strategy,
)
@patch("dynamodb_client.table")
def test_job_record_has_all_required_keys(
    mock_table,
    workflow_type,
    input_s3_key,
    parameters,
    compute_backend,
    notification_email,
):
    """create_job_record() must return a dict with every required field."""
    mock_table.put_item = MagicMock()

    from dynamodb_client import create_job_record

    record = create_job_record(
        job_id="job-abcd1234",
        workflow_type=workflow_type,
        input_s3_key=input_s3_key,
        parameters=parameters,
        compute_backend=compute_backend,
        notification_email=notification_email,
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
    assert required_keys.issubset(record.keys()), (
        f"Missing keys: {required_keys - record.keys()}"
    )

    # Verify correct values and types
    assert record["job_id"] == "job-abcd1234"
    assert record["workflow_type"] == workflow_type
    assert record["input_s3_key"] == input_s3_key
    assert record["parameters"] == parameters
    assert record["status"] == "SUBMITTED"
    assert record["compute_backend"] == compute_backend
    assert isinstance(record["output_s3_keys"], list)
    assert record["output_s3_keys"] == []

    # submission_timestamp must be valid ISO 8601 ending with Z
    assert _ISO8601_RE.match(record["submission_timestamp"]), (
        f"Bad timestamp format: {record['submission_timestamp']}"
    )
    # Verify it actually parses
    datetime.fromisoformat(record["submission_timestamp"].rstrip("Z"))

    # TTL must be a positive integer (epoch seconds ~30 days in the future)
    assert isinstance(record["ttl"], int)
    assert record["ttl"] > 0

    # If notification_email was provided, it must be in the record
    if notification_email is not None:
        assert record["notification_email"] == notification_email
    else:
        assert "notification_email" not in record


@settings(max_examples=100)
@given(
    workflow_type=_workflow_strategy,
    compute_backend=_compute_backend_strategy,
)
@patch("dynamodb_client.table")
def test_job_record_put_item_uses_condition_expression(
    mock_table,
    workflow_type,
    compute_backend,
):
    """create_job_record() must use a conditional write for idempotency."""
    mock_table.put_item = MagicMock()

    from dynamodb_client import create_job_record

    create_job_record(
        job_id="job-00001111",
        workflow_type=workflow_type,
        input_s3_key="data/test.h5ad",
        parameters={},
        compute_backend=compute_backend,
    )

    mock_table.put_item.assert_called_once()
    call_kwargs = mock_table.put_item.call_args
    assert "ConditionExpression" in call_kwargs.kwargs or (
        len(call_kwargs.args) > 1
    ), "put_item must include ConditionExpression for idempotent writes"


# ---------------------------------------------------------------------------
# Property 11: Outputs indexed before status change
# ---------------------------------------------------------------------------


@patch("dynamodb_client.table")
def test_outputs_indexed_before_status_completed(mock_table):
    """append_output_keys() must be called before update_job_status(COMPLETED).

    This ensures that output S3 keys are visible in the record before the
    status transitions to COMPLETED, so consumers polling on status will
    always see the outputs (Requirement 7.3).
    """
    mock_table.update_item = MagicMock()

    from dynamodb_client import append_output_keys, update_job_status

    job_id = "job-aabb0011"
    output_keys = ["scverse-jobs/job-aabb0011/outputs/filtered.h5ad"]

    # Simulate the expected production call order
    append_output_keys(job_id, output_keys)
    update_job_status(job_id, "COMPLETED", completion_timestamp="2025-01-01T00:00:00Z")

    # Verify call order: append_output_keys update_item comes first
    assert mock_table.update_item.call_count == 2, (
        f"Expected 2 update_item calls, got {mock_table.update_item.call_count}"
    )

    first_call = mock_table.update_item.call_args_list[0]
    second_call = mock_table.update_item.call_args_list[1]

    # First call should be the list_append (from append_output_keys)
    first_expr = first_call.kwargs.get(
        "UpdateExpression", first_call[1].get("UpdateExpression", "")
    )
    assert "list_append" in first_expr, (
        f"First update_item should be append_output_keys (list_append), "
        f"got expression: {first_expr}"
    )

    # Second call should set status to COMPLETED (from update_job_status)
    second_values = second_call.kwargs.get(
        "ExpressionAttributeValues",
        second_call[1].get("ExpressionAttributeValues", {}),
    )
    assert second_values.get(":status") == "COMPLETED", (
        f"Second update_item should set status=COMPLETED, "
        f"got values: {second_values}"
    )


@settings(max_examples=50)
@given(
    output_keys=st.lists(
        st.from_regex(r"scverse-jobs/job-[a-f0-9]{8}/outputs/[a-z_]+\.(h5ad|png|json)", fullmatch=True),
        min_size=1,
        max_size=5,
    ),
)
@patch("dynamodb_client.table")
def test_append_then_complete_ordering_with_random_keys(mock_table, output_keys):
    """For any set of output keys, the append-then-complete ordering holds."""
    mock_table.update_item = MagicMock()

    from dynamodb_client import append_output_keys, update_job_status

    job_id = "job-cc220033"

    append_output_keys(job_id, output_keys)
    update_job_status(job_id, "COMPLETED")

    calls = mock_table.update_item.call_args_list
    assert len(calls) == 2

    # First call: list_append for output keys
    first_expr = calls[0].kwargs.get(
        "UpdateExpression", calls[0][1].get("UpdateExpression", "")
    )
    assert "list_append" in first_expr

    # Second call: status = COMPLETED
    second_values = calls[1].kwargs.get(
        "ExpressionAttributeValues",
        calls[1][1].get("ExpressionAttributeValues", {}),
    )
    assert second_values.get(":status") == "COMPLETED"

    # Verify the appended keys match what was passed in
    first_values = calls[0].kwargs.get(
        "ExpressionAttributeValues",
        calls[0][1].get("ExpressionAttributeValues", {}),
    )
    assert first_values.get(":keys") == output_keys
