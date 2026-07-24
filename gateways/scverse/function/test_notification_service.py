"""Unit tests for gateways.scverse.function.notification_service.

Covers the three public functions:
- ensure_email_subscribed
- send_completion_notification
- send_failure_notification

Requirements: 6.1, 6.2, 6.3, 6.5
"""

import os
import sys

# Set dummy AWS credentials BEFORE importing notification_service so that the
# module-level ``boto3.client("sns")`` call does not attempt to resolve real
# credentials (see workspace rule: task-execution-safety).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault(
    "SNS_TOPIC_ARN",
    "arn:aws:sns:us-east-1:000000000000:scverse-job-notifications",
)

# Ensure the function directory is on the import path when running from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import MagicMock

import pytest

import notification_service
from notification_service import (
    ensure_email_subscribed,
    send_completion_notification,
    send_failure_notification,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def mock_sns(monkeypatch):
    """Replace the module-level sns_client with a MagicMock for each test."""
    client = MagicMock()
    monkeypatch.setattr(notification_service, "sns_client", client)
    # Ensure SNS_TOPIC_ARN is set so the early-return guard does not skip the call
    monkeypatch.setattr(
        notification_service,
        "SNS_TOPIC_ARN",
        "arn:aws:sns:us-east-1:000000000000:scverse-job-notifications",
    )
    return client


# ===========================================================================
# ensure_email_subscribed
# ===========================================================================
class TestEnsureEmailSubscribed:
    def test_new_email_creates_subscription(self, mock_sns):
        """When the email is not in the existing subscriptions, subscribe is called."""
        mock_sns.list_subscriptions_by_topic.return_value = {
            "Subscriptions": [
                {"Protocol": "email", "Endpoint": "other@example.com"},
            ]
        }

        ensure_email_subscribed("new@example.com")

        mock_sns.subscribe.assert_called_once_with(
            TopicArn=notification_service.SNS_TOPIC_ARN,
            Protocol="email",
            Endpoint="new@example.com",
        )

    def test_no_existing_subscriptions_creates_subscription(self, mock_sns):
        """Empty subscription list still triggers subscribe for the new email."""
        mock_sns.list_subscriptions_by_topic.return_value = {"Subscriptions": []}

        ensure_email_subscribed("researcher@example.com")

        mock_sns.subscribe.assert_called_once()
        call_kwargs = mock_sns.subscribe.call_args.kwargs
        assert call_kwargs["Protocol"] == "email"
        assert call_kwargs["Endpoint"] == "researcher@example.com"

    def test_existing_email_skips_subscribe(self, mock_sns):
        """Already-subscribed emails are a no-op — subscribe is not called."""
        mock_sns.list_subscriptions_by_topic.return_value = {
            "Subscriptions": [
                {"Protocol": "email", "Endpoint": "already@example.com"},
                {"Protocol": "email", "Endpoint": "other@example.com"},
            ]
        }

        ensure_email_subscribed("already@example.com")

        mock_sns.subscribe.assert_not_called()

    def test_matches_endpoint_only_on_email_protocol(self, mock_sns):
        """A non-email subscription with a matching endpoint must not count as a hit."""
        mock_sns.list_subscriptions_by_topic.return_value = {
            "Subscriptions": [
                {"Protocol": "sms", "Endpoint": "user@example.com"},
            ]
        }

        ensure_email_subscribed("user@example.com")

        mock_sns.subscribe.assert_called_once()

    def test_pagination_traverses_all_pages(self, mock_sns):
        """The function must follow NextToken across pages before subscribing."""
        mock_sns.list_subscriptions_by_topic.side_effect = [
            {
                "Subscriptions": [
                    {"Protocol": "email", "Endpoint": "page1@example.com"},
                ],
                "NextToken": "token-1",
            },
            {
                "Subscriptions": [
                    {"Protocol": "email", "Endpoint": "target@example.com"},
                ],
            },
        ]

        ensure_email_subscribed("target@example.com")

        assert mock_sns.list_subscriptions_by_topic.call_count == 2
        mock_sns.subscribe.assert_not_called()

    def test_returns_early_when_topic_arn_not_configured(self, monkeypatch):
        """If SNS_TOPIC_ARN is empty, the function must not call SNS at all."""
        client = MagicMock()
        monkeypatch.setattr(notification_service, "sns_client", client)
        monkeypatch.setattr(notification_service, "SNS_TOPIC_ARN", "")

        ensure_email_subscribed("user@example.com")

        client.list_subscriptions_by_topic.assert_not_called()
        client.subscribe.assert_not_called()


# ===========================================================================
# send_completion_notification
# ===========================================================================
class TestSendCompletionNotification:
    def test_publishes_with_correct_subject(self, mock_sns):
        send_completion_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            duration_seconds=125.0,
        )

        mock_sns.publish.assert_called_once()
        kwargs = mock_sns.publish.call_args.kwargs
        assert kwargs["Subject"] == "scverse Job Complete: job-a1b2c3d4"
        assert kwargs["TopicArn"] == notification_service.SNS_TOPIC_ARN

    def test_message_contains_core_fields(self, mock_sns):
        """Message body must contain job_id, workflow_type, and formatted duration."""
        send_completion_notification(
            job_id="job-a1b2c3d4",
            workflow_type="integration",
            duration_seconds=1122.0,  # 18m 42s
        )

        message = mock_sns.publish.call_args.kwargs["Message"]
        assert "job-a1b2c3d4" in message
        assert "integration" in message
        assert "18m 42s" in message

    def test_message_contains_get_results_instruction(self, mock_sns):
        send_completion_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            duration_seconds=30.0,
        )

        message = mock_sns.publish.call_args.kwargs["Message"]
        assert "Get results for job-a1b2c3d4" in message

    def test_includes_metrics_when_provided(self, mock_sns):
        """Metrics dict with n_cells and n_clusters must appear in the message."""
        send_completion_notification(
            job_id="job-a1b2c3d4",
            workflow_type="clustering",
            duration_seconds=60.0,
            metrics={"n_cells": 2700, "n_clusters": 8},
        )

        message = mock_sns.publish.call_args.kwargs["Message"]
        assert "2700" in message
        assert "8" in message
        assert "n_cells" in message
        assert "n_clusters" in message

    def test_omits_metrics_section_when_none(self, mock_sns):
        """No ``Key metrics:`` section when no metrics are provided."""
        send_completion_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            duration_seconds=30.0,
            metrics=None,
        )

        message = mock_sns.publish.call_args.kwargs["Message"]
        assert "Key metrics" not in message

    def test_attaches_email_as_message_attribute(self, mock_sns):
        """When notification_email is supplied it must flow into MessageAttributes."""
        send_completion_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            duration_seconds=30.0,
            notification_email="researcher@example.com",
        )

        kwargs = mock_sns.publish.call_args.kwargs
        assert "MessageAttributes" in kwargs
        assert kwargs["MessageAttributes"]["email"]["StringValue"] == (
            "researcher@example.com"
        )
        assert kwargs["MessageAttributes"]["email"]["DataType"] == "String"

    def test_no_message_attributes_when_email_absent(self, mock_sns):
        send_completion_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            duration_seconds=30.0,
        )

        kwargs = mock_sns.publish.call_args.kwargs
        assert "MessageAttributes" not in kwargs

    def test_returns_early_when_topic_arn_not_configured(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(notification_service, "sns_client", client)
        monkeypatch.setattr(notification_service, "SNS_TOPIC_ARN", "")

        send_completion_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            duration_seconds=30.0,
        )

        client.publish.assert_not_called()


# ===========================================================================
# send_failure_notification
# ===========================================================================
class TestSendFailureNotification:
    def test_publishes_with_correct_subject(self, mock_sns):
        send_failure_notification(
            job_id="job-a1b2c3d4",
            workflow_type="integration",
            error_step="scvi_training",
            error_message="OutOfMemoryError",
        )

        mock_sns.publish.assert_called_once()
        kwargs = mock_sns.publish.call_args.kwargs
        assert kwargs["Subject"] == "scverse Job Failed: job-a1b2c3d4"
        assert kwargs["TopicArn"] == notification_service.SNS_TOPIC_ARN

    def test_message_contains_all_failure_fields(self, mock_sns):
        send_failure_notification(
            job_id="job-a1b2c3d4",
            workflow_type="integration",
            error_step="scvi_training",
            error_message="OutOfMemoryError: dataset too large for 16 GB task",
        )

        message = mock_sns.publish.call_args.kwargs["Message"]
        assert "job-a1b2c3d4" in message
        assert "integration" in message
        assert "scvi_training" in message
        assert "OutOfMemoryError" in message

    def test_message_contains_partial_results_instruction(self, mock_sns):
        send_failure_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            error_step="filtering",
            error_message="boom",
        )

        message = mock_sns.publish.call_args.kwargs["Message"]
        assert "job-a1b2c3d4" in message
        assert "partial" in message.lower()

    def test_handles_missing_optional_fields(self, mock_sns):
        """With error_step=None and error_message=None, publish must still succeed."""
        send_failure_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            error_step=None,
            error_message=None,
        )

        mock_sns.publish.assert_called_once()
        kwargs = mock_sns.publish.call_args.kwargs
        assert kwargs["Subject"] == "scverse Job Failed: job-a1b2c3d4"
        message = kwargs["Message"]
        assert "job-a1b2c3d4" in message
        assert "qc" in message

    def test_attaches_email_as_message_attribute(self, mock_sns):
        send_failure_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            error_step="filtering",
            error_message="boom",
            notification_email="researcher@example.com",
        )

        kwargs = mock_sns.publish.call_args.kwargs
        assert "MessageAttributes" in kwargs
        assert kwargs["MessageAttributes"]["email"]["StringValue"] == (
            "researcher@example.com"
        )

    def test_no_message_attributes_when_email_absent(self, mock_sns):
        send_failure_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            error_step="filtering",
            error_message="boom",
        )

        kwargs = mock_sns.publish.call_args.kwargs
        assert "MessageAttributes" not in kwargs

    def test_returns_early_when_topic_arn_not_configured(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(notification_service, "sns_client", client)
        monkeypatch.setattr(notification_service, "SNS_TOPIC_ARN", "")

        send_failure_notification(
            job_id="job-a1b2c3d4",
            workflow_type="qc",
            error_step="filtering",
            error_message="boom",
        )

        client.publish.assert_not_called()
