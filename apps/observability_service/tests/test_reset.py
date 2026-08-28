"""Reset: the fence invariant and the grant model (v1.2 §21.2, §22).

The headline test is the held-batch race. Draining narrows the window in which
a span can be exported just before a reset and arrive just after it; the fence
closes it. That distinction matters because a sleep-based test can only show
that the race did not happen *this time*, whereas fencing on `end_ts` makes it
impossible -- and therefore assertable.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import envelope, new_trace_id, root_attributes, trace_head
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

CONTROLLER_URL = os.environ.get("SCENARIO_CONTROLLER_URL", "http://scenario-controller:8003")
DETECTOR_URL = os.environ.get(
    "DATABASE_URL_DETECTOR",
    "postgresql+asyncpg://blastradius_detector:detector@postgres:5432/blastradius",
)


# --- the held-batch race (§21.2) ------------------------------------------
async def test_a_batch_captured_before_the_reset_is_fenced_on_arrival(client, db):
    """**Invariant: no span whose `end_ts` precedes `last_reset_ts` is ingested.**

    This is the deterministic form of the reset race. The batch is built first,
    exactly as an exporter would have built it, then the reset happens, then the
    batch is released. Nothing sleeps and nothing races: the spans are older
    than the fence, so they cannot land.
    """
    from app.ingest.fence import fence

    held_trace = new_trace_id()
    held_batch = [
        envelope(held_trace, attributes=root_attributes(
            "33333333-3333-3333-3333-333333333333", "web", True, "card")),
        envelope(held_trace, operation="pricing", span_kind="INTERNAL"),
        envelope(held_trace, operation="promo.apply", span_kind="CLIENT",
                 attribution_domain="promo-provider"),
    ]

    # The batch is now "in flight". Reset happens underneath it.
    fenced_before = fence.fenced_total
    response = await client.post("/internal/reset")
    assert response.status_code == 200, response.text

    # Released after the reset, as a real in-flight export would be.
    released = await client.post("/internal/spans", json={"spans": held_batch})
    assert released.status_code == 202
    assert released.json() == {"accepted": 0, "fenced": len(held_batch)}
    assert fence.fenced_total == fenced_before + len(held_batch)
    assert await trace_head(db, held_trace) is None, "a pre-reset span survived the reset"


async def test_a_span_produced_after_the_reset_is_accepted(client, db):
    """The fence must not be a blanket block: post-reset traffic has to land."""
    response = await client.post("/internal/reset")
    assert response.status_code == 200

    fresh = new_trace_id()
    result = await client.post("/internal/spans", json={
        "spans": [envelope(fresh, end_ts=datetime.now(UTC) + timedelta(seconds=1))]
    })
    assert result.json() == {"accepted": 1, "fenced": 0}
    assert await trace_head(db, fresh) is not None


async def test_reset_empties_exactly_the_tables_the_detector_owns(client, db):
    await client.post("/internal/spans", json={
        "spans": [envelope(new_trace_id()), envelope(new_trace_id())]
    })
    response = await client.post("/internal/reset")
    assert response.status_code == 200

    for table in ("span", "trace", "incident", "incident_symptom"):
        count = (await db.execute(sa.text(f"SELECT count(*) FROM {table}"))).scalar_one()
        assert count == 0, f"{table} not cleared"


async def test_reset_clears_the_in_memory_breach_counters(client):
    """The tracker counts consecutive breaches between passes. Left set across a
    reset, a single post-reset breach would open an incident immediately."""
    from datetime import datetime as dt

    from app.detection.engine import DetectionEngine
    from app.detection.slo import readings_for, Evaluation
    from blastradius_contracts.profiles import detection_profile

    engine = DetectionEngine(detection_profile())
    async with (await _detector_engine()).connect() as conn:
        await engine.tracker.observe(
            conn,
            Evaluation(ts=dt.now(UTC), sample_count=100, evaluated=True,
                       readings=readings_for(0.5, 200.0)),
            baseline_window_s=240, baseline_guard_s=20,
        )
        await conn.commit()
    assert engine.tracker.current is not None

    engine.reset_state()
    assert engine.tracker.current is None
    assert engine.last_evaluation is None


async def _detector_engine():
    return create_async_engine(DETECTOR_URL)


# --- the grant model still holds after a reset (§21.2) --------------------
@pytest.mark.parametrize("table", ["ground_truth", "scenario_run", "reveal", "order"])
async def test_the_detector_still_cannot_touch_what_it_does_not_own(client, table):
    """A reset must not require -- or quietly acquire -- a broader grant."""
    from asyncpg.exceptions import InsufficientPrivilegeError

    await client.post("/internal/reset")

    engine = create_async_engine(DETECTOR_URL)
    try:
        with pytest.raises(Exception) as excinfo:
            async with engine.connect() as conn:
                await conn.execute(sa.text(f'DELETE FROM "{table}"'))
        chain, exc = [], excinfo.value
        while exc is not None and exc not in chain:
            chain.append(exc)
            exc = getattr(exc, "orig", None) or exc.__cause__
        assert any(
            isinstance(c, InsufficientPrivilegeError)
            or getattr(c, "sqlstate", None) == "42501"
            or "InsufficientPrivilegeError" in str(c)
            for c in chain
        ), f"detector was allowed to delete from {table}"
    finally:
        await engine.dispose()


# --- the full orchestration, on real roles (§21.2, §22) -------------------
@pytest.mark.slow
async def test_the_full_reset_sequence_runs_without_violating_any_grant():
    """Every step runs as the service that owns the data, so the whole sequence
    completes with no role holding a privilege it should not have."""
    async with AsyncClient(timeout=60) as http:
        response = await http.post(f"{CONTROLLER_URL}/api/reset")
        assert response.status_code == 200, response.text
        report = response.json()

    assert report["drain"]["ordering"]["generator_stopped"] is True
    assert report["drain"]["promo"]["flush_succeeded"] is True
    assert set(report["observability"]["deleted"]) == {
        "incident_symptom", "incident", "span", "trace"
    }
    assert "order" in report["ordering"]["deleted"]
    assert set(report["scenario"]["deleted"]) == {"reveal", "ground_truth", "scenario_run"}
    assert report["warnings"] == [], report["warnings"]

    engine = create_async_engine(DETECTOR_URL)
    try:
        async with engine.connect() as conn:
            for table in ("span", "trace", "incident"):
                count = (await conn.execute(
                    sa.text(f"SELECT count(*) FROM {table}")
                )).scalar_one()
                assert count == 0, f"{table} not cleared by the orchestrated reset"
    finally:
        await engine.dispose()
