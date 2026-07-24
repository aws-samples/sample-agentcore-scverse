"""
Property-based tests for SNS notification messages.

Validates:
- Property 9: Notification message completeness — every message published
  by send_completion_notification() contains the job_id, workflow_type,
  and formatted duration; every message published by
  send_failure_notification() contains the job_id, error_step, and
  error_message.

Requirements: 6.2, 6.3
"""

import os
import sys

# Set dummy AWS credentials BEFORE importing notification_service so that the
# module-level `boto3.client("sns")` call does not attempt to resolve real
# credentials (see workspace rule: task-execution-safety).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault(
    "SNS_TOPIC_ARN",
    "arn:aws:sns:us-east-1:000000000000:scverse-job-notifications",
)

from unittest.mock import MagicMock, patch

# Ensure the function directory is on the import path when running from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hypothesis import given, settings, strategies as st

import notification_service
from notification_service import (
    send_completion_notification,
    send_failure_notification,
    _format_duration,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# job_id follows the canonical format: "job-<hex8>"
_job_id_strategy = st.from_regex(r"job-[a-f0-9]{8}", fullmatch=True)

_workflow_type_strategy = st.sampled_from([
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
])

# Duration: 0 seconds up to 4h (the Step Functions timeout)
_duration_strategy = st.floats(
    min_value=0.0,
    max_value=4 * 3600.0,
    allow_nan=False,
    allow_infinity=False,
)

# Printable, non-empty text for error fields
_printable_nonempty_text = st.text(min_size=1, max_size=120).filter(
    lambda s: s.isprintable() and s.strip()
)


# ---------------------------------------------------------------------------
# Property 9: Notification message completeness — completion
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    job_id=_job_id_strategy,
    workflow_type=_workflow_type_strategy,
    duration_seconds=_duration_strategy,
)
def test_completion_notification_contains_required_fields(
    job_id, workflow_type, duration_seconds
):
    """The completion SNS message must include job_id, workflow_type, and
    a formatted duration string.

    Validates: Requirements 6.2
    """
    mock_sns = MagicMock()
    with patch.object(notification_service, "sns_client", mock_sns):
        send_completion_notification(
            job_id=job_id,
            workflow_type=workflow_type,
            duration_seconds=duration_seconds,
        )

    mock_sns.publish.assert_called_once()
    call_kwargs = mock_sns.publish.call_args.kwargs
    message = call_kwargs["Message"]
    subject = call_kwargs["Subject"]

    # Subject must reference the job_id
    assert job_id in subject, (
        f"Subject must contain job_id.\n  subject={subject!r}\n  job_id={job_id!r}"
    )

    # Message body must contain job_id, workflow_type, and formatted duration
    assert job_id in message, (
        f"Message must contain job_id.\n  job_id={job_id!r}\n  message={message!r}"
    )
    assert workflow_type in message, (
        f"Message must contain workflow_type.\n  "
        f"workflow_type={workflow_type!r}\n  message={message!r}"
    )

    expected_duration = _format_duration(duration_seconds)
    assert expected_duration in message, (
        f"Message must contain formatted duration.\n  "
        f"expected={expected_duration!r}\n  message={message!r}"
    )


# ---------------------------------------------------------------------------
# Property 9: Notification message completeness — failure
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    job_id=_job_id_strategy,
    workflow_type=_workflow_type_strategy,
    error_step=_printable_nonempty_text,
    error_message=_printable_nonempty_text,
)
def test_failure_notification_contains_required_fields(
    job_id, workflow_type, error_step, error_message
):
    """The failure SNS message must include job_id, error_step, and
    error_message.

    Validates: Requirements 6.3
    """
    mock_sns = MagicMock()
    with patch.object(notification_service, "sns_client", mock_sns):
        send_failure_notification(
            job_id=job_id,
            workflow_type=workflow_type,
            error_step=error_step,
            error_message=error_message,
        )

    mock_sns.publish.assert_called_once()
    call_kwargs = mock_sns.publish.call_args.kwargs
    message = call_kwargs["Message"]
    subject = call_kwargs["Subject"]

    # Subject must reference the job_id
    assert job_id in subject, (
        f"Subject must contain job_id.\n  subject={subject!r}\n  job_id={job_id!r}"
    )

    # Message body must contain job_id, error_step, and error_message
    assert job_id in message, (
        f"Failure message must contain job_id.\n  "
        f"job_id={job_id!r}\n  message={message!r}"
    )
    assert error_step in message, (
        f"Failure message must contain error_step.\n  "
        f"error_step={error_step!r}\n  message={message!r}"
    )
    assert error_message in message, (
        f"Failure message must contain error_message.\n  "
        f"error_message={error_message!r}\n  message={message!r}"
    )
