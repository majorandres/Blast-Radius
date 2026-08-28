"""Scenario lifecycle rules (v1.2 §9).

The transition table and the fault ramp are pure, so they are tested here
directly. The database-backed parts -- ground truth written before dispatch, one
run at a time -- are exercised by the live acceptance run.
"""

import pytest

from app.scenarios import IMPLEMENTED, SCENARIO_A, SCENARIOS, scale
from app.state_machine import (
    LEGAL,
    STATES,
    TERMINAL,
    IllegalTransition,
    check_transition,
)


# --- transition table ------------------------------------------------------
def test_the_happy_path_is_legal_end_to_end():
    path = ["IDLE", "ARMED", "INJECTING", "ACTIVE", "RECOVERING", "COMPLETE", "REVEALED"]
    for current, target in zip(path, path[1:], strict=False):
        check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("IDLE", "ACTIVE"),        # cannot inject without arming
        ("IDLE", "REVEALED"),      # cannot reveal what never ran
        ("ARMED", "REVEALED"),
        ("ACTIVE", "ARMED"),       # no going back
        ("COMPLETE", "ACTIVE"),
        ("REVEALED", "COMPLETE"),
    ],
)
def test_illegal_transitions_are_rejected(current, target):
    with pytest.raises(IllegalTransition):
        check_transition(current, target)


def test_a_run_can_be_abandoned_from_any_live_state():
    """Stopping by hand, or a dispatch failure, must always be able to end the
    run -- otherwise a fault stays applied with no run to explain it."""
    for state in ("ARMED", "INJECTING", "ACTIVE", "RECOVERING"):
        check_transition(state, "COMPLETE")


def test_revealed_is_terminal():
    assert LEGAL["REVEALED"] == frozenset()
    assert TERMINAL == frozenset({"COMPLETE", "REVEALED"})


def test_every_state_has_a_transition_rule():
    assert set(LEGAL) == set(STATES)


# --- ground truth ----------------------------------------------------------
def test_scenario_a_ground_truth_names_the_promo_domain():
    assert SCENARIO_A.injected_domain == "promo-provider"
    assert SCENARIO_A.fault_type == "dependency_degradation"


def test_only_scenario_a_is_wired():
    """v1.2 §26: Scenario A alone is required for MVP. B and C are defined so
    the dispatcher shape is fixed, and gated until their days."""
    assert IMPLEMENTED == frozenset({"A"})
    assert set(SCENARIOS) == {"A", "B", "C"}


def test_every_scenario_declares_a_domain_and_a_fault_type():
    for scenario in SCENARIOS.values():
        assert scenario.injected_domain
        assert scenario.fault_type
        assert scenario.promo_faults or scenario.ordering_faults


# --- the ramp --------------------------------------------------------------
def test_the_ramp_starts_at_zero_and_ends_at_full_strength():
    """A step change makes detection latency meaningless: the detector would be
    reacting to an event no real dependency produces."""
    assert scale(SCENARIO_A.promo_faults, 0.0) == {
        "added_latency_ms": 0, "timeout_prob": 0.0, "failure_prob": 0.0
    }
    assert scale(SCENARIO_A.promo_faults, 1.0) == SCENARIO_A.promo_faults


def test_the_ramp_is_monotonic():
    latencies = [scale(SCENARIO_A.promo_faults, i / 5)["added_latency_ms"] for i in range(6)]
    assert latencies == sorted(latencies)
    assert latencies[-1] == 3500


def test_scaling_recurses_into_nested_payloads():
    from app.scenarios import SCENARIO_B

    half = scale(SCENARIO_B.ordering_faults, 0.5)
    assert half["payment"]["failure_prob"] == pytest.approx(0.275)
    assert half["payment"]["added_latency_ms"] == 100


def test_selectors_are_not_scaled():
    """`payment_method` chooses *which* traffic is affected, not how hard. If
    the ramp scaled it, the fault would target nothing."""
    from app.scenarios import SCENARIO_B

    for factor in (0.0, 0.5, 1.0):
        assert scale(SCENARIO_B.ordering_faults, factor)["payment"]["payment_method"] == "wallet"
