"""
Property-based tests for compute routing and sync-to-async fallback.

Validates:
- Property 1: Compute routing decision — correct backend for every
  (tool_name, file_size, workflow_type) combination.
- Property 6: Sync-to-async fallback on error — ComputeLambdaThrottled
  triggers async path instead of propagating the error.

Requirements: 1.2, 1.3, 1.4, 1.11, 4.1, 4.2, 4.3, 4.4, 4.6
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

# Ensure the function directory is on the import path when running from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hypothesis import given, settings, strategies as st

from job_utils import (
    SIZE_THRESHOLD_SYNC,
    SIZE_THRESHOLD_FARGATE,
    VALID_WORKFLOW_TYPES,
)
from compute_router import (
    determine_compute_backend,
    HEAVY_WORKFLOW_TYPES,
    ComputeLambdaThrottled,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_tool_names = st.sampled_from(["run_scrna_qc", "run_scvi_analysis", "submit_scverse_job"])
_file_sizes = st.integers(min_value=0, max_value=10 * 1024 * 1024 * 1024)
_workflow_types = st.sampled_from(sorted(VALID_WORKFLOW_TYPES))


# ---------------------------------------------------------------------------
# Property 1: Compute routing decision
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(tool_name=_tool_names, file_size=_file_sizes, workflow_type=_workflow_types)
def test_compute_routing_decision(tool_name, file_size, workflow_type):
    """The routing function must select the correct backend per the design routing table.

    Routing rules:
    - run_scvi_analysis → always FARGATE async (regardless of file size)
    - submit_scverse_job + full_pipeline → STEP_FUNCTIONS async
    - submit_scverse_job + heavy workflow → FARGATE async
    - run_scrna_qc / submit_scverse_job (non-heavy, non-pipeline):
        file < 500 MB  → LAMBDA sync
        500 MB ≤ file < 2 GB → LAMBDA async
        file ≥ 2 GB → FARGATE async
    - anything else → FARGATE async
    """
    with patch("compute_router.get_file_size", return_value=file_size):
        backend, mode = determine_compute_backend(tool_name, "test.h5ad", workflow_type)

    # --- run_scvi_analysis: always Fargate async ---
    if tool_name == "run_scvi_analysis":
        assert backend == "FARGATE", (
            f"run_scvi_analysis should always route to FARGATE, got {backend}"
        )
        assert mode == "async", (
            f"run_scvi_analysis should always be async, got {mode}"
        )
        return

    # --- submit_scverse_job: full_pipeline → Step Functions ---
    if tool_name == "submit_scverse_job" and workflow_type == "full_pipeline":
        assert backend == "STEP_FUNCTIONS", (
            f"full_pipeline should route to STEP_FUNCTIONS, got {backend}"
        )
        assert mode == "async", (
            f"full_pipeline should be async, got {mode}"
        )
        return

    # --- submit_scverse_job: heavy workflow types → Fargate ---
    if tool_name == "submit_scverse_job" and workflow_type in HEAVY_WORKFLOW_TYPES:
        assert backend == "FARGATE", (
            f"Heavy workflow '{workflow_type}' should route to FARGATE, got {backend}"
        )
        assert mode == "async", (
            f"Heavy workflow '{workflow_type}' should be async, got {mode}"
        )
        return

    # --- Size-based routing (run_scrna_qc or submit_scverse_job with light workflow) ---
    if tool_name in ("run_scrna_qc", "submit_scverse_job"):
        if file_size < SIZE_THRESHOLD_SYNC:
            assert backend == "LAMBDA" and mode == "sync", (
                f"File {file_size} B < 500 MB should be LAMBDA/sync, "
                f"got {backend}/{mode}"
            )
        elif file_size < SIZE_THRESHOLD_FARGATE:
            assert backend == "LAMBDA" and mode == "async", (
                f"File {file_size} B (500 MB–2 GB) should be LAMBDA/async, "
                f"got {backend}/{mode}"
            )
        else:
            assert backend == "FARGATE" and mode == "async", (
                f"File {file_size} B ≥ 2 GB should be FARGATE/async, "
                f"got {backend}/{mode}"
            )
        return

    # --- Fallback: unknown tool → FARGATE async ---
    assert backend == "FARGATE" and mode == "async", (
        f"Unknown tool '{tool_name}' should default to FARGATE/async, "
        f"got {backend}/{mode}"
    )


# Targeted boundary tests for the size thresholds
def test_routing_at_sync_boundary():
    """File exactly at 500 MB boundary should route to LAMBDA async, not sync."""
    with patch("compute_router.get_file_size", return_value=SIZE_THRESHOLD_SYNC):
        backend, mode = determine_compute_backend("run_scrna_qc", "test.h5ad")
    assert backend == "LAMBDA" and mode == "async"


def test_routing_just_below_sync_boundary():
    """File 1 byte below 500 MB should route to LAMBDA sync."""
    with patch("compute_router.get_file_size", return_value=SIZE_THRESHOLD_SYNC - 1):
        backend, mode = determine_compute_backend("run_scrna_qc", "test.h5ad")
    assert backend == "LAMBDA" and mode == "sync"


def test_routing_at_fargate_boundary():
    """File exactly at 2 GB boundary should route to FARGATE async."""
    with patch("compute_router.get_file_size", return_value=SIZE_THRESHOLD_FARGATE):
        backend, mode = determine_compute_backend("run_scrna_qc", "test.h5ad")
    assert backend == "FARGATE" and mode == "async"


def test_routing_just_below_fargate_boundary():
    """File 1 byte below 2 GB should route to LAMBDA async."""
    with patch("compute_router.get_file_size", return_value=SIZE_THRESHOLD_FARGATE - 1):
        backend, mode = determine_compute_backend("run_scrna_qc", "test.h5ad")
    assert backend == "LAMBDA" and mode == "async"


# ---------------------------------------------------------------------------
# Property 6: Sync-to-async fallback on error
# ---------------------------------------------------------------------------


@settings(max_examples=50)
@given(
    file_size=st.integers(min_value=0, max_value=SIZE_THRESHOLD_SYNC - 1),
)
def test_sync_to_async_fallback_on_throttle(file_size):
    """When the Compute Lambda is throttled, the gateway must fall back to
    the async path: create a job record and return a job_id instead of
    propagating the error.
    """
    event = {
        "input_s3_key": "datasets/test.h5ad",
        "species": "human",
    }

    # Build a mock context with the expected AgentCore tool name
    mock_context = MagicMock()
    mock_context.client_context.custom = {"bedrockAgentCoreToolName": "run_scrna_qc"}

    with (
        patch("compute_router.get_file_size", return_value=file_size),
        patch("lambda_function.determine_compute_backend", return_value=("LAMBDA", "sync")),
        patch("lambda_function.dispatch_to_compute_lambda", side_effect=ComputeLambdaThrottled("throttled")),
        patch("lambda_function.create_job_record") as mock_create_job,
        patch("lambda_function.dispatch_to_fargate") as mock_dispatch_fargate,
        patch("lambda_function.generate_job_id", return_value="job-test1234"),
    ):
        from lambda_function import lambda_handler

        response = lambda_handler(event, mock_context)

    # The handler must NOT propagate the error — it should return 200
    assert response["statusCode"] == 200, (
        f"Expected 200 on fallback, got {response['statusCode']}: {response['body']}"
    )

    body = json.loads(response["body"])

    # Must contain a job_id (async submission)
    assert "job_id" in body, (
        f"Fallback response must contain job_id, got: {body}"
    )
    assert body["status"] == "submitted", (
        f"Fallback status should be 'submitted', got: {body.get('status')}"
    )

    # A job record must have been created
    mock_create_job.assert_called_once()

    # Fargate dispatch must have been attempted
    mock_dispatch_fargate.assert_called_once()


def test_sync_to_async_fallback_preserves_job_id_format():
    """The job_id returned on fallback must match the standard format."""
    event = {
        "input_s3_key": "datasets/small.h5ad",
    }

    mock_context = MagicMock()
    mock_context.client_context.custom = {"bedrockAgentCoreToolName": "run_scrna_qc"}

    with (
        patch("compute_router.get_file_size", return_value=100),
        patch("lambda_function.determine_compute_backend", return_value=("LAMBDA", "sync")),
        patch("lambda_function.dispatch_to_compute_lambda", side_effect=ComputeLambdaThrottled("timeout")),
        patch("lambda_function.create_job_record"),
        patch("lambda_function.dispatch_to_fargate"),
        patch("lambda_function.generate_job_id", return_value="job-abcd1234"),
    ):
        from lambda_function import lambda_handler

        response = lambda_handler(event, mock_context)

    body = json.loads(response["body"])
    assert body["job_id"] == "job-abcd1234"


def test_sync_success_does_not_create_job_record():
    """When the sync Compute Lambda succeeds, no job record should be created."""
    event = {
        "input_s3_key": "datasets/small.h5ad",
    }

    mock_context = MagicMock()
    mock_context.client_context.custom = {"bedrockAgentCoreToolName": "run_scrna_qc"}

    sync_result = {"status": "success", "qc_summary": {"n_cells": 1000}}

    with (
        patch("compute_router.get_file_size", return_value=100),
        patch("lambda_function.determine_compute_backend", return_value=("LAMBDA", "sync")),
        patch("lambda_function.dispatch_to_compute_lambda", return_value=sync_result),
        patch("lambda_function.create_job_record") as mock_create_job,
    ):
        from lambda_function import lambda_handler

        response = lambda_handler(event, mock_context)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])

    # Sync success should return the result directly, no job_id
    assert body.get("status") == "success"
    mock_create_job.assert_not_called()
