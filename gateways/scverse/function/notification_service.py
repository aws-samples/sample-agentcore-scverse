"""
SNS notification service for scverse job completion and failure events.

Provides helpers to subscribe researcher emails to the notification topic
and to publish completion / failure messages consumed by the Gateway Lambda,
the Fargate worker, and the Step Functions orchestrator.
"""

import json
import os

import boto3

# ---------------------------------------------------------------------------
# Module-level AWS clients and configuration
# ---------------------------------------------------------------------------
sns_client = boto3.client("sns")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------
def ensure_email_subscribed(email: str) -> None:
    """Ensure *email* is subscribed to the notification SNS topic.

    Paginates through existing subscriptions and matches on endpoint.
    If no match is found, creates a new email subscription — the researcher
    must confirm via the link sent to their inbox on first use.

    Idempotent: calling again with an already-subscribed email is a no-op.
    """
    if not SNS_TOPIC_ARN:
        return

    next_token = None
    while True:
        kwargs = {"TopicArn": SNS_TOPIC_ARN}
        if next_token:
            kwargs["NextToken"] = next_token
        response = sns_client.list_subscriptions_by_topic(**kwargs)

        for subscription in response.get("Subscriptions", []):
            if (
                subscription.get("Protocol") == "email"
                and subscription.get("Endpoint") == email
            ):
                return

        next_token = response.get("NextToken")
        if not next_token:
            break

    sns_client.subscribe(
        TopicArn=SNS_TOPIC_ARN,
        Protocol="email",
        Endpoint=email,
    )


# ---------------------------------------------------------------------------
# Notification publishing
# ---------------------------------------------------------------------------
def send_completion_notification(
    job_id: str,
    workflow_type: str,
    duration_seconds: float,
    metrics: dict = None,
    notification_email: str = None,
) -> None:
    """Publish a completion message to the SNS topic.

    The message body includes the job_id, workflow_type, duration, and
    a summary of key metrics (n_cells, n_clusters when available), along
    with an instruction to ask the agent for full results.

    When *notification_email* is provided, it is attached as an SNS
    ``MessageAttributes`` entry so subscription filter policies can route
    the message to the researcher's subscription only.
    """
    if not SNS_TOPIC_ARN:
        return

    subject = f"scverse Job Complete: {job_id}"
    duration_str = _format_duration(duration_seconds)

    lines = [
        f"Your {workflow_type} analysis has completed successfully.",
        "",
        f"Job ID: {job_id}",
        f"Workflow: {workflow_type}",
        f"Duration: {duration_str}",
    ]

    if metrics:
        summary_lines = _format_metrics_summary(metrics)
        if summary_lines:
            lines.append("")
            lines.append("Key metrics:")
            lines.extend(summary_lines)

    lines.extend([
        "",
        f"Ask the agent: 'Get results for {job_id}'",
    ])
    message = "\n".join(lines)

    publish_kwargs = {
        "TopicArn": SNS_TOPIC_ARN,
        "Subject": subject,
        "Message": message,
    }
    if notification_email:
        publish_kwargs["MessageAttributes"] = {
            "email": {
                "DataType": "String",
                "StringValue": notification_email,
            }
        }

    sns_client.publish(**publish_kwargs)


def send_failure_notification(
    job_id: str,
    workflow_type: str,
    error_step: str = None,
    error_message: str = None,
    notification_email: str = None,
) -> None:
    """Publish a failure message to the SNS topic.

    The message body includes the job_id, the failed step (if this was a
    pipeline), the error message, and an instruction to ask the agent for
    any partial results.

    When *notification_email* is provided, it is attached as an SNS
    ``MessageAttributes`` entry for subscription filtering.
    """
    if not SNS_TOPIC_ARN:
        return

    subject = f"scverse Job Failed: {job_id}"

    lines = [
        f"Your {workflow_type} analysis has failed.",
        "",
        f"Job ID: {job_id}",
        f"Workflow: {workflow_type}",
    ]
    if error_step:
        lines.append(f"Failed step: {error_step}")
    if error_message:
        lines.append(f"Error: {error_message}")

    lines.extend([
        "",
        f"Ask the agent: 'Get results for {job_id}' to retrieve any partial outputs.",
    ])
    message = "\n".join(lines)

    publish_kwargs = {
        "TopicArn": SNS_TOPIC_ARN,
        "Subject": subject,
        "Message": message,
    }
    if notification_email:
        publish_kwargs["MessageAttributes"] = {
            "email": {
                "DataType": "String",
                "StringValue": notification_email,
            }
        }

    sns_client.publish(**publish_kwargs)


# ---------------------------------------------------------------------------
# Internal formatting helpers
# ---------------------------------------------------------------------------
def _format_duration(duration_seconds: float) -> str:
    """Return a human-readable duration (e.g. ``"18m 42s"``, ``"1h 05m"``)."""
    total_seconds = int(max(0, duration_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _format_metrics_summary(metrics: dict) -> list:
    """Return a list of human-readable metric lines for well-known keys."""
    summary = []
    for key in ("n_cells", "n_genes", "n_clusters"):
        if key in metrics and metrics[key] is not None:
            summary.append(f"  - {key}: {metrics[key]}")
    return summary
