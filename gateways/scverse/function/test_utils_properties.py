"""
Property-based tests for shared utilities in job_utils.py.

Validates:
- Property 8: S3 path construction and job isolation
- Property 10: Email validation correctness
- Property 12: Job manifest JSON round-trip integrity

Requirements: 6.4, 8.1, 8.2, 7.7, 11.6
"""

import json
import os
import re
import sys

# Ensure the function directory is on the import path when running from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hypothesis import given, settings, assume, strategies as st

from job_utils import (
    build_output_prefix,
    build_intermediate_prefix,
    build_manifest_key,
    generate_job_id,
    validate_email,
    VALID_WORKFLOW_TYPES,
)

# ---------------------------------------------------------------------------
# Property 8: S3 path construction and job isolation
# ---------------------------------------------------------------------------

_OUTPUT_PREFIX_RE = re.compile(r"^scverse-jobs/job-[a-f0-9]{8}/outputs/$")
_INTERMEDIATE_PREFIX_RE = re.compile(r"^scverse-jobs/job-[a-f0-9]{8}/intermediate/$")
_MANIFEST_KEY_RE = re.compile(r"^scverse-jobs/job-[a-f0-9]{8}/manifest\.json$")


@settings(max_examples=100)
@given(st.data())
def test_distinct_job_ids_produce_distinct_output_prefixes(data):
    """Two independently generated job IDs must never share an output prefix."""
    id1 = generate_job_id()
    id2 = generate_job_id()
    assume(id1 != id2)

    prefix1 = build_output_prefix(id1)
    prefix2 = build_output_prefix(id2)

    assert prefix1 != prefix2, f"Collision: {id1} and {id2} share prefix {prefix1}"
    assert _OUTPUT_PREFIX_RE.match(prefix1), f"Bad format: {prefix1}"
    assert _OUTPUT_PREFIX_RE.match(prefix2), f"Bad format: {prefix2}"


@settings(max_examples=100)
@given(st.data())
def test_all_path_builders_consistent_for_same_job(data):
    """Output, intermediate, and manifest paths must all reference the same job ID."""
    job_id = generate_job_id()

    output = build_output_prefix(job_id)
    intermediate = build_intermediate_prefix(job_id)
    manifest = build_manifest_key(job_id)

    assert _OUTPUT_PREFIX_RE.match(output)
    assert _INTERMEDIATE_PREFIX_RE.match(intermediate)
    assert _MANIFEST_KEY_RE.match(manifest)

    # All three paths embed the same job_id segment
    assert job_id in output
    assert job_id in intermediate
    assert job_id in manifest


def test_job_id_format():
    """Generated job IDs must match the expected hex pattern."""
    job_id_re = re.compile(r"^job-[a-f0-9]{8}$")
    for _ in range(200):
        jid = generate_job_id()
        assert job_id_re.match(jid), f"Bad job ID format: {jid}"


# ---------------------------------------------------------------------------
# Property 10: Email validation
# ---------------------------------------------------------------------------

# Strategy for emails that conform to the validate_email regex pattern:
# local part: [a-zA-Z0-9._%+-]+  domain: [a-zA-Z0-9.-]+  TLD: [a-zA-Z]{2,}
_local_part = st.from_regex(r"[a-zA-Z0-9._%+\-]+", fullmatch=True).filter(
    lambda s: 1 <= len(s) <= 64
)
_domain = st.from_regex(r"[a-zA-Z0-9\-]+(\.[a-zA-Z0-9\-]+)*", fullmatch=True).filter(
    lambda s: 1 <= len(s) <= 50
)
_tld = st.from_regex(r"[a-zA-Z]{2,6}", fullmatch=True)
_valid_email = st.builds(lambda l, d, t: f"{l}@{d}.{t}", _local_part, _domain, _tld)


@settings(max_examples=100)
@given(_valid_email)
def test_validate_email_accepts_valid_emails(email):
    """validate_email must accept emails matching its regex pattern."""
    assert validate_email(email) is True, f"Rejected valid email: {email}"


@settings(max_examples=100)
@given(st.emails())
def test_validate_email_is_consistent_with_hypothesis_emails(email):
    """validate_email returns a bool for any RFC-compliant email (no crash)."""
    result = validate_email(email)
    assert isinstance(result, bool), f"Expected bool, got {type(result)}"


@settings(max_examples=100)
@given(
    st.text(
        alphabet=st.characters(
            exclude_categories=("Cs",),  # exclude surrogates
        ),
        min_size=0,
        max_size=50,
    ).filter(lambda s: "@" not in s)
)
def test_validate_email_rejects_strings_without_at(text):
    """Strings with no '@' character can never be valid emails."""
    assert validate_email(text) is False, f"Accepted non-email: {text!r}"


def test_validate_email_known_invalid_cases():
    """Specific known-invalid patterns must be rejected."""
    invalid = [
        "",
        "no-at-sign",
        "@no-local.com",
        "spaces in@email.com",
        "user@",
        "user@.com",
    ]
    for addr in invalid:
        assert validate_email(addr) is False, f"Accepted invalid: {addr!r}"


# ---------------------------------------------------------------------------
# Property 12: Job manifest round-trip (JSON serialization)
# ---------------------------------------------------------------------------

_workflow_strategy = st.sampled_from(sorted(VALID_WORKFLOW_TYPES))

_manifest_strategy = st.fixed_dictionaries(
    {
        "job_id": st.from_regex(r"job-[a-f0-9]{8}", fullmatch=True),
        "workflow_type": _workflow_strategy,
        "input_s3_key": st.text(
            alphabet=st.characters(
                categories=("L", "N", "P"),
                exclude_characters=("\x00",),
            ),
            min_size=1,
            max_size=100,
        ),
        "storage_bucket": st.text(
            alphabet=st.characters(categories=("Ll", "N"), include_characters=("-",)),
            min_size=3,
            max_size=63,
        ),
        "parameters": st.dictionaries(
            keys=st.text(min_size=1, max_size=20).filter(lambda s: s.isprintable()),
            values=st.one_of(
                st.text(max_size=50).filter(lambda s: s.isprintable()),
                st.integers(min_value=-1000, max_value=1000),
                st.floats(allow_nan=False, allow_infinity=False),
                st.booleans(),
            ),
            max_size=10,
        ),
        "output_prefix": st.from_regex(
            r"scverse-jobs/job-[a-f0-9]{8}/outputs/", fullmatch=True
        ),
    }
)


@settings(max_examples=100)
@given(_manifest_strategy)
def test_manifest_json_round_trip(manifest):
    """Serializing a manifest to JSON and back must preserve all fields."""
    serialized = json.dumps(manifest)
    deserialized = json.loads(serialized)

    assert deserialized == manifest, (
        f"Round-trip mismatch:\n  original:     {manifest}\n  deserialized: {deserialized}"
    )


@settings(max_examples=100)
@given(_manifest_strategy)
def test_manifest_contains_all_required_keys(manifest):
    """Every generated manifest must contain the full set of required keys."""
    required_keys = {
        "job_id",
        "workflow_type",
        "input_s3_key",
        "storage_bucket",
        "parameters",
        "output_prefix",
    }
    assert required_keys.issubset(manifest.keys()), (
        f"Missing keys: {required_keys - manifest.keys()}"
    )
