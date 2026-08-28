"""Tests 1-4: the live spine, against the running stack.

These drive a real checkout through ordering-app over HTTP and then assert on
what landed in Postgres. Nothing is stubbed -- the point is that a genuine OTel
SDK, a genuine HTTP hop, and a genuine batch export produce the tree the
contract describes.
"""

import asyncio
import os

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

ORDERING_URL = os.environ.get("ORDERING_APP_URL", "http://ordering-app:8001")
PROMO_URL = os.environ.get("PROMO_PROVIDER_URL_BASE", "http://promo-provider:8002")
DETECTOR_URL = os.environ.get(
    "DATABASE_URL_DETECTOR",
    "postgresql+asyncpg://blastradius_detector:detector@postgres:5432/blastradius",
)

# Two BatchSpanProcessors at schedule_delay_millis=2000, plus the cross-process
# hop, so the promo spans can trail the ordering-app batch by a full cycle.
SETTLE_TIMEOUT_S = 25

SPAN_QUERY = sa.text(
    "SELECT s.operation, s.span_kind, s.blocking, s.parent_span_id, s.span_id, "
    "       sv.name AS emitting_service, d.name AS attribution_domain "
    "FROM span s "
    "JOIN service sv ON sv.id = s.emitting_service_id "
    "JOIN domain  d  ON d.id  = s.attribution_domain_id "
    "WHERE s.trace_id = :t"
)


async def _await_spans(engine, trace_id: str, expected: int) -> list:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + SETTLE_TIMEOUT_S
    rows: list = []
    while loop.time() < deadline:
        async with engine.connect() as conn:
            rows = (await conn.execute(SPAN_QUERY, {"t": trace_id})).mappings().all()
        if len(rows) >= expected:
            return list(rows)
        await asyncio.sleep(1)
    return list(rows)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def checkout_trace():
    """One promo-bearing checkout, followed all the way into Postgres.

    The full §6.2 tree only exists when the promo call succeeds: under a timeout
    the client aborts and no `promo.handle` span is ever emitted, which is
    correct behaviour and the wrong fixture for asserting tree shape. So this
    clears any fault first and retries until it gets a clean checkout.
    """
    async with AsyncClient(timeout=30) as client:
        await client.put(f"{PROMO_URL}/_faults", json={})
        trace_id = None
        for _ in range(6):
            response = await client.post(
                f"{ORDERING_URL}/_debug/checkout",
                params={"has_promo": "true", "channel": "web", "payment_method": "wallet"},
            )
            response.raise_for_status()
            body = response.json()
            if body["status"] == "CONFIRMED":
                trace_id = body["trace_id"]
                break
            await asyncio.sleep(2)
        assert trace_id, "could not obtain a healthy promo-bearing checkout"

    engine = create_async_engine(DETECTOR_URL)
    try:
        rows = await _await_spans(engine, trace_id, expected=11)
        async with engine.connect() as conn:
            head = (
                await conn.execute(
                    sa.text("SELECT * FROM trace WHERE trace_id = :t"), {"t": trace_id}
                )
            ).mappings().first()
        yield trace_id, rows, head
    finally:
        await engine.dispose()


# --- test 1 ---------------------------------------------------------------
@pytest.mark.asyncio(loop_scope="module")
async def test_checkout_reaches_the_detector(checkout_trace):
    _, rows, head = checkout_trace
    assert len(rows) == 11, f"expected the full tree, got {[r['operation'] for r in rows]}"
    assert head is not None
    assert head["span_count"] == 11


# --- test 2 ---------------------------------------------------------------
@pytest.mark.asyncio(loop_scope="module")
async def test_trace_context_propagates_across_the_promo_hop(checkout_trace):
    """One trace id spanning two processes -- gate zero, asserted.

    If this fails, promo spans arrive as orphan roots and every Day 2
    attribution result is wrong in a way that looks like an algorithm bug.
    """
    _, rows, _ = checkout_trace
    services = {r["emitting_service"] for r in rows}
    assert services == {"ordering-app", "promo-provider"}
    assert any(
        r["operation"] == "promo.handle" and r["emitting_service"] == "promo-provider"
        for r in rows
    )


# --- test 3 ---------------------------------------------------------------
@pytest.mark.asyncio(loop_scope="module")
async def test_hierarchy_matches_the_contract(checkout_trace):
    _, rows, _ = checkout_trace
    by_id = {r["span_id"]: r for r in rows}
    parent_of = {
        r["operation"]: (
            by_id[r["parent_span_id"]]["operation"] if r["parent_span_id"] in by_id else None
        )
        for r in rows
    }

    assert parent_of["checkout"] is None, "checkout must be the root"
    assert parent_of["loyalty_tier_lookup"] == "pricing"
    # Across the process boundary, through five filtered auto spans.
    assert parent_of["promo.handle"] == "promo.apply"
    for operation in (
        "validate_order",
        "pricing",
        "db.pool_acquire",
        "promo.apply",
        "payment.authorize",
        "db.persist_order",
        "analytics.publish",
        "confirmation",
    ):
        assert parent_of[operation] == "checkout", operation

    analytics = next(r for r in rows if r["operation"] == "analytics.publish")
    assert analytics["blocking"] is False, "analytics.publish must not be blocking"


# --- test 4 ---------------------------------------------------------------
@pytest.mark.asyncio(loop_scope="module")
async def test_emitting_service_is_distinct_from_attribution_domain(checkout_trace):
    """Two identities per span (v1.2 §6.1).

    `emitting_service` is truthful OTel resource identity; `attribution_domain`
    is the logical failure domain. A CLIENT span's domain is always its *peer*
    (CC-A), which is why payment-gateway and order-datastore are legal
    attribution targets even though no such process exists.
    """
    _, rows, _ = checkout_trace
    by_operation = {r["operation"]: r for r in rows}

    payment = by_operation["payment.authorize"]
    assert payment["emitting_service"] == "ordering-app"
    assert payment["attribution_domain"] == "payment-gateway"
    assert payment["span_kind"] == "CLIENT"

    for operation in ("db.pool_acquire", "db.persist_order"):
        assert by_operation[operation]["emitting_service"] == "ordering-app"
        assert by_operation[operation]["attribution_domain"] == "order-datastore"

    promo_apply = by_operation["promo.apply"]
    assert promo_apply["emitting_service"] == "ordering-app"
    assert promo_apply["attribution_domain"] == "promo-provider"
