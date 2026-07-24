"""
Unit tests for ``handle_list_datasets`` — task 12.4.

Focused coverage aligned with the task spec:
- .h5ad files only (non-.h5ad entries filtered out)
- ``prefix_filter`` is prepended with ``datasets/`` when passed to S3
- Empty bucket returns a well-formed empty response
- Pagination token is returned in the response when S3 reports IsTruncated
- ``next_token`` from the event is forwarded to S3 as ``ContinuationToken``

Requirements: 10.1, 10.6, 10.7
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _context(tool_name: str = "list_datasets") -> SimpleNamespace:
    """Build a minimal Lambda context carrying the AgentCore tool name."""
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": tool_name}
        )
    )


class TestListDatasetsFilters:
    """Covers filtering, prefix handling, empty results, and pagination."""

    def test_returns_only_h5ad_files(self):
        """3 .h5ad + 1 .csv → response contains only the 3 .h5ad entries."""
        s3_response = {
            "Contents": [
                {
                    "Key": "datasets/pbmc3k.h5ad",
                    "Size": 100_000_000,
                    "LastModified": datetime(2025, 6, 1, tzinfo=timezone.utc),
                },
                {
                    "Key": "datasets/heart_10x.h5ad",
                    "Size": 250_000_000,
                    "LastModified": datetime(2025, 6, 10, tzinfo=timezone.utc),
                },
                {
                    "Key": "datasets/spatial/visium.h5ad",
                    "Size": 500_000_000,
                    "LastModified": datetime(2025, 6, 15, tzinfo=timezone.utc),
                },
                {
                    "Key": "datasets/metadata.csv",
                    "Size": 2048,
                    "LastModified": datetime(2025, 5, 1, tzinfo=timezone.utc),
                },
            ],
            "IsTruncated": False,
        }

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _context())

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "success"
        assert body["count"] == 3

        file_names = {d["file_name"] for d in body["datasets"]}
        assert file_names == {"pbmc3k.h5ad", "heart_10x.h5ad", "visium.h5ad"}
        assert "metadata.csv" not in file_names

    def test_prefix_filter_narrows_search(self):
        """prefix_filter='spatial/' → S3 called with Prefix='datasets/spatial/'."""
        s3_response = {"Contents": [], "IsTruncated": False}

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            lambda_handler({"prefix_filter": "spatial/"}, _context())

        call_kwargs = mock_s3.list_objects_v2.call_args.kwargs
        assert call_kwargs["Prefix"] == "datasets/spatial/"

    def test_empty_bucket(self):
        """Empty Contents → success with empty list, count 0, next_token None."""
        s3_response = {"Contents": [], "IsTruncated": False}

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _context())

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body == {
            "status": "success",
            "datasets": [],
            "count": 0,
            "next_token": None,
        }

    def test_pagination_token_in_response(self):
        """IsTruncated=True + NextContinuationToken → echoed into next_token."""
        s3_response = {
            "Contents": [
                {
                    "Key": "datasets/a.h5ad",
                    "Size": 1024,
                    "LastModified": datetime(2025, 6, 1, tzinfo=timezone.utc),
                },
            ],
            "IsTruncated": True,
            "NextContinuationToken": "token-page-2",
        }

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            response = lambda_handler({}, _context())

        body = json.loads(response["body"])
        assert body["status"] == "success"
        assert body["next_token"] == "token-page-2"

    def test_pagination_token_forwarded_to_s3(self):
        """next_token in event → forwarded to S3 as ContinuationToken."""
        s3_response = {"Contents": [], "IsTruncated": False}

        with patch("lambda_function.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = s3_response

            from lambda_function import lambda_handler

            lambda_handler({"next_token": "token-page-2"}, _context())

        call_kwargs = mock_s3.list_objects_v2.call_args.kwargs
        assert call_kwargs["ContinuationToken"] == "token-page-2"
