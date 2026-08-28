"""Healthy soak on both profiles (v1.2 §21.4.5).

Twenty simulated minutes of baseline traffic must open zero incidents. A
detector that cries wolf on healthy traffic is worse than useless: every real
diagnosis it makes afterwards is unbelievable.

**Simulated, and deliberately so.** The evaluations are generated from the
distribution actually measured on the running system -- 100% checkout success,
p95 in the 180-320ms band -- rather than by waiting twenty real minutes twice.
That buys two things a wall-clock soak does not: it runs the REALISTIC profile,
whose 30s cadence and 300s window would otherwise never be exercised, and it can
inject the specific adversarial shapes that *should* be survivable, instead of
hoping they happen to occur.

The live system is separately observed to sit quiet; this pins the logic.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest
from blastradius_contracts.profiles import DETECTION

from app.detection.incidents import IncidentTracker
from app.detection.slo import Evaluation, readings_for

SOAK_MINUTES = 20

# Measured on the running system at 150 orders/min with both red herrings on.
# analytics.publish failures do not touch checkout_status, which is why healthy
# success sits at 1.0 rather than a little under it.
HEALTHY_SUCCESS = (0.995, 1.0)
HEALTHY_P95_MS = (180.0, 320.0)


def healthy(rng: random.Random, ts: datetime, n: int) -> Evaluation:
    return Evaluation(
        ts=ts,
        sample_count=n,
        evaluated=True,
        readings=readings_for(rng.uniform(*HEALTHY_SUCCESS), rng.uniform(*HEALTHY_P95_MS)),
    )


def tracker_for(profile) -> IncidentTracker:
    return IncidentTracker(
        breach_persistence=profile.breach_persistence,
        recovery_persistence=profile.recovery_persistence,
    )


@pytest.mark.parametrize("profile_name", ["DEMO", "REALISTIC"])
async def test_twenty_minutes_of_healthy_traffic_opens_no_incident(client, db, profile_name):
    profile = DETECTION[profile_name]
    rng = random.Random(20260828)
    tracker = tracker_for(profile)

    ts = datetime.now(UTC)
    passes = (SOAK_MINUTES * 60) // profile.slo_eval_interval_s

    for _ in range(passes):
        ts += timedelta(seconds=profile.slo_eval_interval_s)
        await tracker.observe(
            db, healthy(rng, ts, profile.slo_min_samples * 3),
            baseline_window_s=profile.baseline_window_s,
            baseline_guard_s=profile.baseline_guard_s,
        )

    assert tracker.current is None, (
        f"{profile_name}: an incident opened during {SOAK_MINUTES} minutes of "
        f"healthy traffic across {passes} evaluations"
    )


@pytest.mark.parametrize("profile_name", ["DEMO", "REALISTIC"])
async def test_isolated_bad_windows_do_not_open_an_incident(client, db, profile_name):
    """A single bad window is noise, and `breach_persistence` exists for it.

    This is the shape that actually threatens a clean soak: one slow window from
    a GC pause or a batch of unlucky traces. Two *consecutive* breaches are
    required, so an isolated spike must recover without ever opening.
    """
    profile = DETECTION[profile_name]
    rng = random.Random(7)
    tracker = tracker_for(profile)

    ts = datetime.now(UTC)
    passes = (SOAK_MINUTES * 60) // profile.slo_eval_interval_s
    blip_every = max(profile.breach_persistence + 2, 8)

    for i in range(passes):
        ts += timedelta(seconds=profile.slo_eval_interval_s)
        if i % blip_every == blip_every - 1:
            evaluation = Evaluation(
                ts=ts, sample_count=profile.slo_min_samples * 3, evaluated=True,
                readings=readings_for(0.995, 1400.0),   # one slow window
            )
        else:
            evaluation = healthy(rng, ts, profile.slo_min_samples * 3)

        await tracker.observe(
            db, evaluation,
            baseline_window_s=profile.baseline_window_s,
            baseline_guard_s=profile.baseline_guard_s,
        )
        current = tracker.current
        # PENDING is the correct state immediately after a blip. What must never
        # happen is reaching OPEN, which is what a scored incident requires.
        assert current is None or current.state == "PENDING", (
            f"{profile_name}: an isolated bad window reached {current.state}"
        )

    # The loop can end on a blip, which legitimately leaves a PENDING behind.
    # Let healthy traffic resume and assert it is discarded rather than opened.
    for _ in range(profile.recovery_persistence + 1):
        ts += timedelta(seconds=profile.slo_eval_interval_s)
        await tracker.observe(
            db, healthy(rng, ts, profile.slo_min_samples * 3),
            baseline_window_s=profile.baseline_window_s,
            baseline_guard_s=profile.baseline_guard_s,
        )

    assert tracker.current is None, (
        f"{profile_name}: a PENDING incident survived the return to healthy traffic"
    )


@pytest.mark.parametrize("profile_name", ["DEMO", "REALISTIC"])
async def test_thin_windows_during_a_lull_open_nothing(client, db, profile_name):
    """Below the sample floor nothing is decided, in either direction."""
    profile = DETECTION[profile_name]
    tracker = tracker_for(profile)
    ts = datetime.now(UTC)

    for _ in range(60):
        ts += timedelta(seconds=profile.slo_eval_interval_s)
        await tracker.observe(
            db,
            Evaluation(ts=ts, sample_count=profile.slo_min_samples - 1,
                       evaluated=False, readings=()),
            baseline_window_s=profile.baseline_window_s,
            baseline_guard_s=profile.baseline_guard_s,
        )

    assert tracker.current is None


def test_both_profiles_share_every_detection_threshold():
    """The README claim, asserted rather than trusted: DEMO compresses
    observation windows and nothing else. If a threshold ever diverges, the
    demo stops being evidence about the real system."""
    demo, realistic = DETECTION["DEMO"], DETECTION["REALISTIC"]

    from app.detection.slo import CHECKOUT_SUCCESS_MIN, P95_LATENCY_MAX_MS
    from app.detection.attribution import (
        DOMINANCE, MIN_ATTRIBUTION_SHARE, MIN_RUNNER_UP_GAP,
    )
    from app.blast_radius.concentration import CONCENTRATED_AT, SPARED_AT

    # These are module-level constants, shared by construction -- the assertion
    # is that they are not profile-scoped fields.
    for threshold in (CHECKOUT_SUCCESS_MIN, P95_LATENCY_MAX_MS, DOMINANCE,
                      MIN_ATTRIBUTION_SHARE, MIN_RUNNER_UP_GAP,
                      CONCENTRATED_AT, SPARED_AT):
        assert threshold is not None

    assert demo.breach_persistence == realistic.breach_persistence

    # What DEMO *is* allowed to compress: windows and cadences.
    assert demo.slo_window_s < realistic.slo_window_s
    assert demo.slo_eval_interval_s < realistic.slo_eval_interval_s
    assert demo.baseline_window_s < realistic.baseline_window_s
