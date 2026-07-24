"""
Property-based tests for the Step Functions pipeline orchestrator.

Property 7: Pipeline step ordering — Validates Requirements 5.1, 5.2.

Parses the ASL definition at ``pipeline_definition.json`` (the single source
of truth for the orchestrator) and simulates the state machine execution
path for every generated subset of optional steps. Asserts:

  * The core pipeline steps always appear in order as a contiguous prefix:
    ``qc → integration → clustering → differential_expression``.
  * Each requested optional step from ``{spatial, annotation, velocity}``
    appears exactly once, strictly after the core prefix, in the ASL's
    fixed optional-step order.
  * No unrequested optional step ever appears in the path.

Requirements:
  * 5.1 — Core order: QC → integration → clustering → DE.
  * 5.2 — Optional steps (spatial, annotation, velocity) appended after
    the core pipeline when requested.
"""

import json
import os
import sys
from typing import Any, Dict, Iterable, List, Set

from hypothesis import given, settings, strategies as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PIPELINE_DEFINITION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pipeline_definition.json"
)

# Map ASL state name → canonical step name the test asserts on.
# These are the only "real" compute steps; Update* / Check* / ParseInput etc.
# are orchestration states and do not count toward the executed-step list.
COMPUTE_STATE_TO_STEP = {
    "RunQC": "qc",
    "RunIntegration": "integration",
    "RunClustering": "clustering",
    "RunDE": "differential_expression",
    "RunSpatial": "spatial",
    "RunAnnotation": "annotation",
    "RunVelocity": "velocity",
}

CORE_STEPS = ["qc", "integration", "clustering", "differential_expression"]
OPTIONAL_STEPS = ["spatial", "annotation", "velocity"]

# Terminal state types that end the walk.
TERMINAL_TYPES = {"Succeed", "Fail"}


# ---------------------------------------------------------------------------
# ASL loading + simulation helpers
# ---------------------------------------------------------------------------


def _load_asl() -> Dict[str, Any]:
    with open(PIPELINE_DEFINITION_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _evaluate_choice(state: Dict[str, Any], optional_steps: str) -> str:
    """Evaluate a Choice state against the optional_steps string.

    Supports the two Choice patterns used in the pipeline:
      * ``IsPresent: true`` on ``$.parameters.optional_steps``
      * ``StringMatches: "*<token>*"`` on ``$.parameters.optional_steps``

    ``optional_steps`` is the string value of ``$.parameters.optional_steps``;
    pass ``None`` to indicate the field is absent.
    """
    for choice in state.get("Choices", []):
        if "IsPresent" in choice:
            present = optional_steps is not None
            if present == choice["IsPresent"]:
                return choice["Next"]
        elif "StringMatches" in choice:
            if optional_steps is None:
                continue
            pattern = choice["StringMatches"]
            # Only ``*token*`` patterns appear in this pipeline.
            assert pattern.startswith("*") and pattern.endswith("*"), (
                f"Unsupported StringMatches pattern: {pattern!r}"
            )
            token = pattern.strip("*")
            if token in optional_steps:
                return choice["Next"]
        else:
            raise AssertionError(f"Unsupported Choice operator in {choice!r}")
    return state["Default"]


def _simulate_path(
    asl: Dict[str, Any], requested_optional: Iterable[str]
) -> List[str]:
    """Simulate the happy-path execution of the state machine.

    Returns the ordered list of canonical step names (``qc``, ``integration``,
    …) that were executed. Orchestration-only states (``Update*``, ``Check*``,
    ``ParseInput``, ``GenerateReport``, ``SendNotification``) are skipped.
    Failure-handling states (``HandleFailure``, ``SendFailureNotification``,
    ``PipelineFailed``) are never reached on the happy path.
    """
    requested_set: Set[str] = set(requested_optional)
    # Encode requested subset as the ASL expects: a string that ``StringMatches``
    # against ``*spatial*`` / ``*annotation*`` / ``*velocity*``. ``None`` means
    # the field is absent (CheckOptionalSteps branches on IsPresent).
    if requested_set:
        optional_steps_value = ",".join(sorted(requested_set))
    else:
        optional_steps_value = None

    states = asl["States"]
    current = asl["StartAt"]
    executed: List[str] = []
    visited: Set[str] = set()

    while True:
        if current in visited:
            raise AssertionError(f"Cycle detected in state machine at {current!r}")
        visited.add(current)

        state = states[current]
        state_type = state["Type"]

        if state_type in TERMINAL_TYPES:
            break

        if state_type == "Choice":
            current = _evaluate_choice(state, optional_steps_value)
            continue

        # Pass / Task states: record the step if it's a compute state.
        if current in COMPUTE_STATE_TO_STEP:
            executed.append(COMPUTE_STATE_TO_STEP[current])

        next_state = state.get("Next")
        if next_state is None:
            # End: true only when a non-terminal state has no Next.
            if state.get("End") is True:
                break
            raise AssertionError(
                f"State {current!r} has no Next and is not terminal"
            )
        current = next_state

    return executed


# ---------------------------------------------------------------------------
# Sanity check: the ASL we're testing against is well-formed for this test.
# ---------------------------------------------------------------------------


def test_asl_definition_loads_and_has_expected_states():
    """The pipeline ASL must contain every state the simulator depends on."""
    asl = _load_asl()
    assert asl["StartAt"] == "ParseInput"
    for state_name in COMPUTE_STATE_TO_STEP:
        assert state_name in asl["States"], f"Missing state: {state_name}"
    for choice_name in ("CheckOptionalSteps", "CheckSpatial", "CheckAnnotation", "CheckVelocity"):
        assert asl["States"][choice_name]["Type"] == "Choice"


# ---------------------------------------------------------------------------
# Property 7: Pipeline step ordering
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    requested=st.sets(
        st.sampled_from(OPTIONAL_STEPS),
        max_size=len(OPTIONAL_STEPS),
    )
)
def test_pipeline_step_ordering(requested: Set[str]):
    """For any subset of optional steps, the executed path must satisfy:

      1. Core steps appear exactly once, in order, as a contiguous prefix.
      2. Every requested optional step appears exactly once, after the core
         prefix, in the ASL's fixed order (spatial → annotation → velocity).
      3. No unrequested optional step appears anywhere in the path.
    """
    asl = _load_asl()
    executed = _simulate_path(asl, requested)

    # (1) Core prefix — exact order, no duplicates, contiguous at the start.
    assert executed[: len(CORE_STEPS)] == CORE_STEPS, (
        f"Core prefix mismatch. Expected {CORE_STEPS}, got {executed[: len(CORE_STEPS)]} "
        f"(full path: {executed})"
    )

    tail = executed[len(CORE_STEPS):]

    # (2) Tail contains exactly the requested optional steps, in ASL order.
    expected_tail = [s for s in OPTIONAL_STEPS if s in requested]
    assert tail == expected_tail, (
        f"Optional-step tail mismatch. Requested={sorted(requested)}, "
        f"expected tail={expected_tail}, got tail={tail} (full path: {executed})"
    )

    # (3) No unrequested optional step appears.
    unrequested = set(OPTIONAL_STEPS) - requested
    for step in unrequested:
        assert step not in executed, (
            f"Unrequested optional step {step!r} was executed "
            f"(requested={sorted(requested)}, full path={executed})"
        )

    # (4) Core steps are never duplicated.
    for core_step in CORE_STEPS:
        assert executed.count(core_step) == 1, (
            f"Core step {core_step!r} appeared {executed.count(core_step)} times "
            f"in {executed}"
        )


# ---------------------------------------------------------------------------
# Targeted examples — pin down each boundary of the property above.
# ---------------------------------------------------------------------------


def test_no_optional_steps_runs_only_core():
    asl = _load_asl()
    assert _simulate_path(asl, set()) == CORE_STEPS


def test_all_optional_steps_runs_core_then_all_optionals_in_order():
    asl = _load_asl()
    assert _simulate_path(asl, set(OPTIONAL_STEPS)) == CORE_STEPS + OPTIONAL_STEPS


def test_only_annotation_optional():
    asl = _load_asl()
    assert _simulate_path(asl, {"annotation"}) == CORE_STEPS + ["annotation"]


def test_spatial_and_velocity_only_preserves_asl_order():
    asl = _load_asl()
    # Requesting {velocity, spatial} must still produce spatial before velocity,
    # since the ASL checks spatial first, then annotation, then velocity.
    assert _simulate_path(asl, {"velocity", "spatial"}) == CORE_STEPS + ["spatial", "velocity"]
