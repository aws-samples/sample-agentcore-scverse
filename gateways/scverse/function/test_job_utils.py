"""Unit tests for gateways.scverse.function.job_utils."""

import re
from unittest.mock import MagicMock, patch

import pytest

from gateways.scverse.function.job_utils import (
    VALID_WORKFLOW_TYPES,
    format_size_human,
    generate_job_id,
    get_file_size,
    validate_email,
    validate_workflow_type,
)


# ---------------------------------------------------------------------------
# generate_job_id
# ---------------------------------------------------------------------------
class TestGenerateJobId:
    def test_format_matches_pattern(self):
        job_id = generate_job_id()
        assert re.fullmatch(r"^job-[a-f0-9]{8}$", job_id)

    def test_ids_are_unique(self):
        ids = {generate_job_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# get_file_size
# ---------------------------------------------------------------------------
class TestGetFileSize:
    @patch("gateways.scverse.function.job_utils.s3_client")
    def test_returns_content_length(self, mock_s3):
        size_245_mb = 245 * 1024 * 1024
        mock_s3.head_object.return_value = {"ContentLength": size_245_mb}

        result = get_file_size("my-bucket", "data/sample.h5ad")

        assert result == size_245_mb
        mock_s3.head_object.assert_called_once_with(
            Bucket="my-bucket", Key="data/sample.h5ad"
        )

    @patch("gateways.scverse.function.job_utils.s3_client")
    def test_raises_file_not_found_on_404(self, mock_s3):
        from botocore.exceptions import ClientError

        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        with pytest.raises(FileNotFoundError, match="File not found"):
            get_file_size("my-bucket", "missing/key.h5ad")

    @patch("gateways.scverse.function.job_utils.s3_client")
    def test_raises_file_not_found_on_no_such_key(self, mock_s3):
        from botocore.exceptions import ClientError

        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "No such key"}},
            "HeadObject",
        )

        with pytest.raises(FileNotFoundError):
            get_file_size("my-bucket", "missing/key.h5ad")

    @patch("gateways.scverse.function.job_utils.s3_client")
    def test_reraises_other_client_errors(self, mock_s3):
        from botocore.exceptions import ClientError

        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}},
            "HeadObject",
        )

        with pytest.raises(ClientError):
            get_file_size("my-bucket", "forbidden/key.h5ad")


# ---------------------------------------------------------------------------
# validate_workflow_type
# ---------------------------------------------------------------------------
class TestValidateWorkflowType:
    @pytest.mark.parametrize("wf_type", sorted(VALID_WORKFLOW_TYPES))
    def test_accepts_all_valid_types(self, wf_type):
        # Should not raise
        validate_workflow_type(wf_type)

    def test_rejects_invalid_type(self):
        with pytest.raises(ValueError, match="Unknown workflow_type 'invalid'"):
            validate_workflow_type("invalid")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            validate_workflow_type("")


# ---------------------------------------------------------------------------
# validate_email
# ---------------------------------------------------------------------------
class TestValidateEmail:
    def test_accepts_valid_email(self):
        assert validate_email("user@example.com") is True

    def test_rejects_empty_string(self):
        assert validate_email("") is False

    def test_rejects_no_at_sign(self):
        assert validate_email("no-at-sign") is False

    def test_rejects_no_local_part(self):
        assert validate_email("@no-local.com") is False

    def test_rejects_no_domain(self):
        assert validate_email("user@") is False


# ---------------------------------------------------------------------------
# format_size_human
# ---------------------------------------------------------------------------
class TestFormatSizeHuman:
    def test_zero_bytes(self):
        assert format_size_human(0) == "0 B"

    def test_kilobytes(self):
        assert format_size_human(1024) == "1.0 KB"

    def test_500_megabytes(self):
        result = format_size_human(500 * 1024 * 1024)
        assert result == "500.0 MB"

    def test_2_gigabytes(self):
        result = format_size_human(2 * 1024 * 1024 * 1024)
        assert result == "2.0 GB"

    def test_small_bytes(self):
        assert format_size_human(512) == "512 B"
