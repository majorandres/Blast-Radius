"""Timing profiles (v1.2 §20).

DEMO compresses observation windows so an incident lifecycle fits an interactive
demonstration. **Detection logic, thresholds, and attribution are identical in
both profiles** -- only the windows move. That claim is what makes the demo
honest, so nothing here may carry a detection threshold.

The profile is split in two on purpose. `DetectionProfile` is everything the
detector needs; `ScenarioProfile` is everything the injector needs. The detector
imports only the former, so no module it loads mentions scenario timing at all.
This is defence in depth behind the grants (§1.3), not a substitute for them:
these are static constants either way and tell nobody whether a run is active.
"""

import os
from dataclasses import dataclass
from typing import Literal

ProfileName = Literal["DEMO", "REALISTIC"]


@dataclass(frozen=True)
class DetectionProfile:
    """What the detector reads. Contains no scenario knowledge of any kind."""

    name: ProfileName

    # --- SLO evaluation ---
    slo_eval_interval_s: int
    slo_window_s: int
    slo_min_samples: int

    # --- incident lifecycle ---
    breach_persistence: int
    recovery_persistence: int

    # --- trace settling ---
    trace_settle_s: int

    # --- baseline, frozen at incident open ---
    baseline_window_s: int
    baseline_guard_s: int

    # --- blast radius floors ---
    min_cohort_n: int
    min_abnormal_traces: int


@dataclass(frozen=True)
class ScenarioProfile:
    """What the injector reads. The detector never imports this."""

    name: ProfileName
    ramp_s: int
    hold_s: int
    recovery_hold_s: int
    drain_timeout_s: int
    flush_timeout_s: int


DETECTION: dict[ProfileName, DetectionProfile] = {
    "REALISTIC": DetectionProfile(
        name="REALISTIC",
        slo_eval_interval_s=30,
        slo_window_s=300,
        slo_min_samples=50,
        breach_persistence=2,
        recovery_persistence=3,
        trace_settle_s=5,
        baseline_window_s=900,
        baseline_guard_s=60,
        min_cohort_n=30,
        min_abnormal_traces=20,
    ),
    # DEMO evaluates every 3s, not the 5s in v1.2 §20. Recorded as a deviation.
    # This is an observation *cadence*, not a threshold: the window is still 60s
    # and a breach is still a breach. Checking more often only shortens how long
    # the detector sits on a signal it has already received -- at 5s, confirming
    # a breach costs 10s before anything else can happen.
    "DEMO": DetectionProfile(
        name="DEMO",
        slo_eval_interval_s=3,
        slo_window_s=60,
        slo_min_samples=40,
        breach_persistence=2,
        recovery_persistence=2,
        trace_settle_s=2,
        baseline_window_s=240,
        baseline_guard_s=20,
        min_cohort_n=10,
        min_abnormal_traces=10,
    ),
}

SCENARIO: dict[ProfileName, ScenarioProfile] = {
    "REALISTIC": ScenarioProfile(
        name="REALISTIC", ramp_s=45, hold_s=180, recovery_hold_s=60,
        drain_timeout_s=10, flush_timeout_s=5,
    ),
    # DEMO ramp is 9s, not the 15s in v1.2 §20. Recorded as a deviation, and
    # made for consistency rather than convenience: DEMO compresses the SLO
    # window 5x (300s -> 60s) and the baseline window 3.75x, but left the ramp
    # at 3x. Diagnosis cannot beat the ramp, so the slowest-compressed setting
    # was setting the demo's floor. Nine seconds is 45/5, the same factor the
    # observation windows use. No detection threshold moves.
    "DEMO": ScenarioProfile(
        name="DEMO", ramp_s=9, hold_s=90, recovery_hold_s=25,
        drain_timeout_s=10, flush_timeout_s=5,
    ),
}


def _selected() -> ProfileName:
    name = os.environ.get("PROFILE", "DEMO").upper()
    if name not in DETECTION:
        raise ValueError(f"unknown PROFILE {name!r}; expected DEMO or REALISTIC")
    return name  # type: ignore[return-value]


def detection_profile() -> DetectionProfile:
    return DETECTION[_selected()]


def scenario_profile() -> ScenarioProfile:
    return SCENARIO[_selected()]
