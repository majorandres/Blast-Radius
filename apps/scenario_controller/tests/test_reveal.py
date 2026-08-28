"""Reveal association and scoring (v1.2 §5.4, §21.4.10).

The window rule is what stops a run being scored against an unrelated incident.
Without it the score is meaningless in the *flattering* direction: hand back any
old incident that happens to name the right domain and you always win.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.reveal import IncidentOutsideRunWindow, is_correct, validate_window

RUN_START = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
RUN_END = RUN_START + timedelta(seconds=105)
RECOVERY_HOLD_S = 25
SLO_WINDOW_S = 60
#: run_end + recovery_hold + slo_window
UPPER = RUN_END + timedelta(seconds=RECOVERY_HOLD_S + SLO_WINDOW_S)


def check(first_breach_ts, *, ended=RUN_END):
    validate_window(
        incident_first_breach_ts=first_breach_ts,
        run_started_ts=RUN_START,
        run_ended_ts=ended,
        recovery_hold_s=RECOVERY_HOLD_S,
        slo_window_s=SLO_WINDOW_S,
    )


# --- inside the window -----------------------------------------------------
@pytest.mark.parametrize(
    "offset_s",
    [0, 1, 30, 105, 130, RECOVERY_HOLD_S + SLO_WINDOW_S + 105],
)
def test_an_incident_inside_the_window_is_accepted(offset_s):
    check(RUN_START + timedelta(seconds=offset_s))


def test_both_bounds_are_inclusive():
    check(RUN_START)
    check(UPPER)


# --- §21.4.10 lower bound --------------------------------------------------
def test_an_incident_predating_the_run_is_rejected():
    """It cannot have been caused by a fault that had not been armed yet."""
    with pytest.raises(IncidentOutsideRunWindow) as exc:
        check(RUN_START - timedelta(seconds=1))
    assert "before the run started" in exc.value.message


def test_an_incident_from_a_previous_run_is_rejected():
    with pytest.raises(IncidentOutsideRunWindow):
        check(RUN_START - timedelta(minutes=10))


# --- §21.4.10 upper bound --------------------------------------------------
def test_an_incident_after_the_detection_window_is_rejected():
    """Detection may lag the fault by one full SLO window plus the recovery
    hold. Anything later is a different incident."""
    with pytest.raises(IncidentOutsideRunWindow) as exc:
        check(UPPER + timedelta(seconds=1))
    assert "after the run's detection window closed" in exc.value.message


def test_a_still_running_run_measures_the_upper_bound_from_now():
    """`run_end = COALESCE(ended_ts, now())`, so a reveal during a live run
    still has a bounded window rather than an open-ended one."""
    now = datetime.now(UTC)
    validate_window(
        incident_first_breach_ts=now,
        run_started_ts=now - timedelta(seconds=30),
        run_ended_ts=None,
        recovery_hold_s=RECOVERY_HOLD_S,
        slo_window_s=SLO_WINDOW_S,
    )
    with pytest.raises(IncidentOutsideRunWindow):
        validate_window(
            incident_first_breach_ts=now + timedelta(seconds=RECOVERY_HOLD_S + SLO_WINDOW_S + 30),
            run_started_ts=now - timedelta(seconds=30),
            run_ended_ts=None,
            recovery_hold_s=RECOVERY_HOLD_S,
            slo_window_s=SLO_WINDOW_S,
        )


# --- scoring ---------------------------------------------------------------
def test_a_confident_correct_naming_scores():
    assert is_correct("ATTRIBUTED", "promo-provider", "promo-provider") is True


@pytest.mark.parametrize(
    ("verdict", "domain"),
    [
        ("ATTRIBUTED", "payment-gateway"),   # confident and wrong
        ("AMBIGUOUS", "promo-provider"),     # hedged, even on the right domain
        ("NO_DIAGNOSIS", None),
        ("NO_INCIDENT", None),
    ],
)
def test_everything_else_is_a_miss(verdict, domain):
    assert is_correct(verdict, domain, "promo-provider") is False


def test_a_hedge_on_the_right_domain_is_still_a_miss():
    """AMBIGUOUS means the detector could not separate two candidates. Scoring
    that as a win would reward a system that never commits."""
    assert is_correct("AMBIGUOUS", "promo-provider", "promo-provider") is False
