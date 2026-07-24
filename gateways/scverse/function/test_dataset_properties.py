"""
Property-based tests for dataset listing.

Validates:
- Property 13: Dataset listing with filtering and sorting —
  handle_list_datasets() returns only .h5ad files under
  ``datasets/{prefix_filter}``, sorted by last_modified descending, with
  the full metadata shape the agent relies on.
- Property 14: Dataset listing pagination —
  next_token is non-None iff more .h5ad matches exist than fit on a
  single page (>50), and the first page carries at most 50 entries.

Validates: Requirements 10.1, 10.6, 10.7
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Dummy AWS credentials / region BEFORE importing lambda_function, which
# constructs ``boto3.client("s3")`` at module load time (see workspace rule
# task-execution-safety §4: mock before import for modules that construct
# AWS clients at load time).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("STORAGE_BUCKET", "test-bucket")

# Ensure the function directory is on the import path when running from the
# repo root (matches the pattern used in test_routing_properties.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hypothesis import given, settings, strategies as st  # noqa: E402

import lambda_function  # noqa: E402
from lambda_function import handle_list_datasets  # noqa: E402
from job_utils import format_size_human  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# prefix_filter: alphanumerics / underscores / forward slashes, possibly empty.
# Kept narrow so the generated datasets prefix stays sensible and so filtering
# has a non-trivial chance of matching.
_prefix_filter_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_/",
    min_size=0,
    max_size=10,
)

# File name stem (without extension)
_file_stem_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_",
    min_size=1,
    max_size=20,
)

# Object size in bytes
_size_strategy = st.integers(min_value=0, max_value=5 * 1024 * 1024 * 1024)

# LastModified timestamps within a wide window so ordering is non-trivial
_base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
_timestamp_strategy = st.integers(min_value=0, max_value=365 * 24 * 3600).map(
    lambda s: _base_time + timedelta(seconds=s)
)


def _s3_object_strategy(prefix_filter: str):
    """Build a strategy that generates a single S3 object dict under
    ``datasets/{prefix_filter}`` with either a .h5ad or .csv extension.
    """
    extension = st.sampled_from([".h5ad", ".csv"])
    return st.builds(
        lambda stem, ext, size, ts: {
            "Key": f"datasets/{prefix_filter}{stem}{ext}",
            "Size": size,
            "LastModified": ts,
        },
        _file_stem_strategy,
        extension,
        _size_strategy,
        _timestamp_strategy,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_s3_response(objects, max_keys=50):
    """Build a list_objects_v2 response that honours the bucket's first-page
    behaviour: at most ``max_keys`` entries per page, IsTruncated + a
    continuation token only when more remain.
    """
    page = objects[:max_keys]
    is_truncated = len(objects) > max_keys
    response = {"Contents": page}
    if is_truncated:
        response["IsTruncated"] = True
        response["NextContinuationToken"] = "next-page-token"
    else:
        response["IsTruncated"] = False
    return response


# ---------------------------------------------------------------------------
# Property 13: Dataset listing with filtering and sorting
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    prefix_filter=_prefix_filter_strategy,
    data=st.data(),
)
def test_dataset_listing_filter_and_sort(prefix_filter, data):
    """handle_list_datasets() must:

    - Return ONLY .h5ad entries (no .csv, etc.).
    - Every ``s3_key`` starts with ``datasets/{prefix_filter}``.
    - Entries are sorted by ``last_modified`` descending.
    - Each entry has ``s3_key``, ``file_name``, ``size_bytes``,
      ``size_human``, ``last_modified``.

    Validates: Requirements 10.1, 10.6
    """
    # Generate a mixed batch of .h5ad and .csv objects under the prefix.
    objects = data.draw(
        st.lists(_s3_object_strategy(prefix_filter), min_size=0, max_size=30)
    )
    mock_response = _make_mock_s3_response(objects)

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = mock_response

    with patch.object(lambda_function, "s3_client", mock_s3):
        result = handle_list_datasets({"prefix_filter": prefix_filter})

    # Basic response shape
    assert result["status"] == "success"
    assert "datasets" in result
    assert isinstance(result["datasets"], list)
    assert result["count"] == len(result["datasets"])

    # S3 was called with the expected prefix
    mock_s3.list_objects_v2.assert_called_once()
    call_kwargs = mock_s3.list_objects_v2.call_args.kwargs
    assert call_kwargs["Prefix"] == f"datasets/{prefix_filter}"

    datasets = result["datasets"]

    # Count of expected .h5ad entries in the first page (server-side paging
    # already applied in the mock response).
    expected_h5ad = [
        obj for obj in mock_response["Contents"] if obj["Key"].endswith(".h5ad")
    ]
    assert len(datasets) == len(expected_h5ad), (
        f"Expected {len(expected_h5ad)} .h5ad entries, got {len(datasets)}"
    )

    required_keys = {"s3_key", "file_name", "size_bytes", "size_human", "last_modified"}
    last_modified_values = []
    for entry in datasets:
        # Only .h5ad files
        assert entry["s3_key"].endswith(".h5ad"), (
            f"Non-.h5ad entry leaked through filter: {entry['s3_key']}"
        )
        # Prefix invariant
        assert entry["s3_key"].startswith(f"datasets/{prefix_filter}"), (
            f"s3_key {entry['s3_key']!r} does not start with "
            f"'datasets/{prefix_filter}'"
        )
        # Full metadata shape
        assert required_keys.issubset(entry.keys()), (
            f"Missing keys: {required_keys - entry.keys()}"
        )
        # file_name derived correctly
        assert entry["file_name"] == entry["s3_key"].rsplit("/", 1)[-1]
        # size_human consistent with helper
        assert entry["size_human"] == format_size_human(entry["size_bytes"])
        # last_modified must be an ISO 8601 string we can parse back
        parsed = datetime.fromisoformat(entry["last_modified"])
        last_modified_values.append(parsed)

    # Sorted by last_modified descending
    assert last_modified_values == sorted(last_modified_values, reverse=True), (
        "datasets are not sorted by last_modified descending"
    )


# ---------------------------------------------------------------------------
# Property 14: Dataset listing pagination
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(n=st.integers(min_value=0, max_value=200))
def test_dataset_listing_pagination(n):
    """Pagination invariants:

    - S3 returns at most 50 entries per page.
    - ``next_token`` is non-None iff more .h5ad matches remain than fit on
      the first page. Since the gateway filters for .h5ad *after* calling
      list_objects_v2, the trigger is the raw S3 object count (>50), which
      is what drives IsTruncated in the real bucket.
    - The first page carries at most 50 datasets.

    Validates: Requirement 10.7
    """
    # Construct N S3 objects alternating .h5ad / .csv so we exercise both
    # filtering and pagination together. Timestamps are strictly decreasing
    # so the sort order is well-defined.
    objects = [
        {
            "Key": f"datasets/file_{i:04d}.{'h5ad' if i % 2 == 0 else 'csv'}",
            "Size": 1024 * (i + 1),
            "LastModified": _base_time + timedelta(seconds=n - i),
        }
        for i in range(n)
    ]

    mock_response = _make_mock_s3_response(objects, max_keys=50)

    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = mock_response

    with patch.object(lambda_function, "s3_client", mock_s3):
        result = handle_list_datasets({"prefix_filter": ""})

    # The first page has at most 50 datasets (before or after .h5ad filter).
    assert len(result["datasets"]) <= 50, (
        f"First page returned {len(result['datasets'])} entries; expected <=50"
    )

    # next_token reflects S3's IsTruncated, which fires when the underlying
    # matching object count (pre-.h5ad-filter) exceeds 50.
    if n > 50:
        assert result["next_token"] is not None, (
            f"Expected next_token when n={n} > 50 (S3 IsTruncated)"
        )
        assert result["next_token"] == "next-page-token"
    else:
        assert result["next_token"] is None, (
            f"Expected next_token=None when n={n} <= 50, got {result['next_token']!r}"
        )

    # Sanity: the MaxKeys parameter was 50
    call_kwargs = mock_s3.list_objects_v2.call_args.kwargs
    assert call_kwargs["MaxKeys"] == 50


def test_pagination_token_passthrough():
    """When the caller supplies a next_token, it is forwarded to S3 as
    ContinuationToken (Requirement 10.7).
    """
    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {
        "Contents": [],
        "IsTruncated": False,
    }

    with patch.object(lambda_function, "s3_client", mock_s3):
        handle_list_datasets({"prefix_filter": "", "next_token": "page-2"})

    call_kwargs = mock_s3.list_objects_v2.call_args.kwargs
    assert call_kwargs.get("ContinuationToken") == "page-2"
