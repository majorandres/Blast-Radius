"""Slice 2a: SLO thresholds and the incident lifecycle.

The threshold tests are pure. The lifecycle tests write real incidents to real
PostgreSQL, but drive the tracker directly with synthesised evaluations rather
than waiting on the loop, so every transition is asserted at an exact boundary
instead of being inferred from timing.

The live detection loop is running against the same database while these
execute. Each test drives its own tracker instance and asserts only on the
incident it created, so nothing here depends on the system being quiescent.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.detection.incidents import IncidentTracker
from app.detection.slo import (
    CHECKOUT_SUCCESS_MIN,
    P95_LATENCY_MAX_MS,
    Evaluation,
    readings_for,
)

BASELINE_KWARGS = {"baseline_window_s": 240, "baseline_guard_s": 20}


def evaluation(ts: datetime, *, success: float | None = 1.0,
               p95: float | None = 200.0, n: int = 100) -> Evaluation:
    return Evaluation(ts=ts, sample_count=n, evaluated=True,
                      readings=readings_for(success, p95))


def thin(ts: datetime, n: int = 3) -> Evaluation:
    """A window below the sample floor: neither breach nor clean."""
    return Evaluation(ts=ts, sample_count=n, evaluated=False, readings=())


def tracker(breach: int = 2, recovery: int = 2) -> IncidentTracker:
    return IncidentTracker(breach_persistence=breach, recovery_persistence=recovery)


async def state_of(db, incident_id: uuid.UUID) -> str:
    return (await db.execute(
        sa.text("SELECT state FROM incident WHERE id = :i"), {"i": incident_id}
    )).scalar_one()


# --- SLO thresholds, at the boundary ---------------------------------------
@pytest.mark.parametrize(
    ("success", "expected"),
    [(1.0, False), (0.9801, False), (CHECKOUT_SUCCESS_MIN, False), (0.9799, True), (0.6, True)],
)
def test_checkout_success_breaches_strictly_below_threshold(success, expected):
    """`gte 0.98`, so exactly 0.98 holds. Off-by-one here would make the healthy
    soak flaky at precisely the rate the generator produces."""
    assert readings_for(success, 200.0)[0].breached is expected


@pytest.mark.parametrize(
    ("p95", "expected"),
    [(200.0, False), (999.9, False), (P95_LATENCY_MAX_MS, False), (1000.1, True), (2060.0, True)],
)
def test_p95_latency_breaches_strictly_above_threshold(p95, expected):
    assert readings_for(1.0, p95)[1].breached is expected


def test_the_two_slos_are_independent():
    """Scenario C's whole premise: latency degrades while availability holds.

    If these were a mirrored pair, a fail-slow incident would be invisible.
    """
    availability, latency = readings_for(1.0, 2500.0)
    assert availability.breached is False
    assert latency.breached is True


def test_a_missing_observation_is_not_a_breach():
    assert [r.breached for r in readings_for(None, None)] == [False, False]


# --- incident lifecycle ----------------------------------------------------
async def test_one_breach_pends_and_two_consecutive_breaches_open(client, db):
    """`breach_persistence` is why a single bad window is not an incident."""
    t = tracker()
    now = datetime.now(UTC)

    incident = await t.observe(db, evaluation(now, success=0.5), **BASELINE_KWARGS)
    assert incident.state == "PENDING"
    assert incident.opened_ts is None
    assert await state_of(db, incident.id) == "PENDING"

    incident = await t.observe(db, evaluation(now + timedelta(seconds=5), success=0.5),
                               **BASELINE_KWARGS)
    assert incident.state == "OPEN"
    assert incident.opened_ts is not None
    assert await state_of(db, incident.id) == "OPEN"


async def test_baseline_is_frozen_at_open_and_never_recomputed(client, db):
    """If the baseline followed the incident, the incident would become its own
    baseline and impact would trend to UNAFFECTED however bad things got."""
    t = tracker()
    now = datetime.now(UTC)
    await t.observe(db, evaluation(now, success=0.5), **BASELINE_KWARGS)
    incident = await t.observe(db, evaluation(now + timedelta(seconds=5), success=0.5),
                               **BASELINE_KWARGS)

    async def snapshot():
        return (await db.execute(
            sa.text("SELECT baseline_snapshot FROM incident WHERE id = :i"),
            {"i": incident.id},
        )).scalar_one()

    frozen = await snapshot()
    assert frozen["n"] >= 0
    assert frozen["abnormal_latency_threshold_ms"] >= 500.0, "floor of 500ms per §12.1"

    for offset in (10, 15, 20):
        await t.observe(db, evaluation(now + timedelta(seconds=offset), success=0.1),
                        **BASELINE_KWARGS)
    assert await snapshot() == frozen


async def test_a_pending_incident_that_recovers_is_discarded(client, db):
    """One bad window then recovery is noise, and must not surface."""
    t = tracker()
    now = datetime.now(UTC)
    incident = await t.observe(db, evaluation(now, success=0.5), **BASELINE_KWARGS)
    assert incident.state == "PENDING"

    assert await t.observe(db, evaluation(now + timedelta(seconds=5)), **BASELINE_KWARGS) is None
    assert t.current is None
    assert await state_of(db, incident.id) == "CLOSED"


async def test_open_recovers_through_recovering_to_closed(client, db):
    t = tracker(recovery=2)
    now = datetime.now(UTC)
    await t.observe(db, evaluation(now, success=0.5), **BASELINE_KWARGS)
    incident = await t.observe(db, evaluation(now + timedelta(seconds=5), success=0.5),
                               **BASELINE_KWARGS)
    assert incident.state == "OPEN"

    await t.observe(db, evaluation(now + timedelta(seconds=10)), **BASELINE_KWARGS)
    assert await state_of(db, incident.id) == "RECOVERING"

    assert await t.observe(db, evaluation(now + timedelta(seconds=15)), **BASELINE_KWARGS) is None
    assert await state_of(db, incident.id) == "CLOSED"
    assert t.current is None


async def test_a_breach_while_recovering_reopens_the_same_incident(client, db):
    """Not a new incident. An incident is a thing that happened, not a thing
    that was noticed repeatedly -- and reveal scores against exactly one."""
    t = tracker()
    now = datetime.now(UTC)
    await t.observe(db, evaluation(now, success=0.5), **BASELINE_KWARGS)
    incident = await t.observe(db, evaluation(now + timedelta(seconds=5), success=0.5),
                               **BASELINE_KWARGS)
    await t.observe(db, evaluation(now + timedelta(seconds=10)), **BASELINE_KWARGS)
    assert await state_of(db, incident.id) == "RECOVERING"

    reopened = await t.observe(db, evaluation(now + timedelta(seconds=15), success=0.5),
                               **BASELINE_KWARGS)
    assert reopened.id == incident.id
    assert await state_of(db, incident.id) == "OPEN"


async def test_further_breaches_append_symptoms_rather_than_new_incidents(client, db):
    t = tracker()
    now = datetime.now(UTC)
    await t.observe(db, evaluation(now, success=0.5, p95=3000.0), **BASELINE_KWARGS)
    incident = await t.observe(db, evaluation(now + timedelta(seconds=5), success=0.5, p95=3000.0),
                               **BASELINE_KWARGS)
    for offset in (10, 15, 20):
        again = await t.observe(
            db, evaluation(now + timedelta(seconds=offset), success=0.4, p95=3100.0),
            **BASELINE_KWARGS,
        )
        assert again.id == incident.id

    rows = (await db.execute(sa.text(
        "SELECT slo_id, count(*) FROM incident_symptom WHERE incident_id = :i GROUP BY slo_id"
    ), {"i": incident.id})).all()
    counts = dict(rows)
    assert counts == {1: 5, 2: 5}, "both SLOs breached on all five evaluations"


async def test_a_window_below_the_sample_floor_advances_nothing(client, db):
    """Thin data is not healthy data. Treating it as clean would close an
    incident during a traffic lull; treating it as breached would open one."""
    t = tracker()
    now = datetime.now(UTC)
    incident = await t.observe(db, evaluation(now, success=0.5), **BASELINE_KWARGS)

    unchanged = await t.observe(db, thin(now + timedelta(seconds=5)), **BASELINE_KWARGS)
    assert unchanged.id == incident.id
    assert unchanged.state == "PENDING", "a thin window must not open the incident"

    opened = await t.observe(db, evaluation(now + timedelta(seconds=10), success=0.5),
                             **BASELINE_KWARGS)
    assert opened.state == "OPEN"


async def test_no_incident_opens_on_a_healthy_stream(client, db):
    t = tracker()
    now = datetime.now(UTC)
    for offset in range(0, 60, 5):
        assert await t.observe(db, evaluation(now + timedelta(seconds=offset)),
                               **BASELINE_KWARGS) is None
    assert t.current is None
