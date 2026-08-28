"""Tests 5-10: idempotence, ordering, dimensions, and the fence."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import envelope, new_span_id, new_trace_id, root_attributes, span_rows, trace_head


async def post(client, spans: list[dict]) -> dict:
    response = await client.post("/internal/spans", json={"spans": spans})
    assert response.status_code == 202, response.text
    return response.json()


# --- test 5 ---------------------------------------------------------------
async def test_duplicate_batch_is_a_no_op_and_does_not_advance_the_settle_clock(client, db):
    """A fully duplicate batch must change nothing.

    `last_span_ts` not advancing is the point: Day 2's settle gate is
    `now() - last_span_ts > trace_settle_seconds`, so a re-delivered batch that
    bumped the clock would postpone attribution for no reason.
    """
    trace_id = new_trace_id()
    batch = [envelope(trace_id), envelope(trace_id, operation="pricing", span_kind="INTERNAL")]

    assert (await post(client, batch))["accepted"] == 2
    first = await trace_head(db, trace_id)
    assert first["span_count"] == 2

    await post(client, batch)
    second = await trace_head(db, trace_id)

    assert second["span_count"] == 2
    assert second["last_span_ts"] == first["last_span_ts"]
    assert len(await span_rows(db, trace_id)) == 2


# --- test 6 ---------------------------------------------------------------
async def test_partial_duplicate_advances_by_new_spans_only(client, db):
    trace_id = new_trace_id()
    first_span = envelope(trace_id)
    await post(client, [first_span])
    before = await trace_head(db, trace_id)

    second_span = envelope(trace_id, operation="pricing", span_kind="INTERNAL")
    await post(client, [first_span, second_span])
    after = await trace_head(db, trace_id)

    assert after["span_count"] == 2, "the duplicate must not be counted twice"
    assert after["last_span_ts"] > before["last_span_ts"]


# --- test 7 ---------------------------------------------------------------
async def test_children_before_root_leaves_the_trace_invisible(client, db):
    """A trace with no root is invisible to every query (v1.2 §3.4).

    Children routinely arrive before the root, so the head must exist with null
    root fields rather than being withheld.
    """
    trace_id = new_trace_id()
    root_id = new_span_id()
    await post(client, [
        envelope(trace_id, parent_span_id=root_id, operation="pricing", span_kind="INTERNAL"),
        envelope(trace_id, parent_span_id=root_id, operation="confirmation", span_kind="INTERNAL"),
    ])

    head = await trace_head(db, trace_id)
    assert head is not None
    assert head["span_count"] == 2
    assert head["root_span_id"] is None
    assert head["root_end_ts"] is None
    assert head["channel"] is None


# --- test 8 ---------------------------------------------------------------
async def test_root_arriving_last_populates_the_head(client, db):
    trace_id = new_trace_id()
    root_id = new_span_id()
    await post(client, [
        envelope(trace_id, parent_span_id=root_id, operation="pricing", span_kind="INTERNAL"),
    ])
    assert (await trace_head(db, trace_id))["root_span_id"] is None

    await post(client, [
        envelope(trace_id, span_id=root_id, operation="checkout", duration_ms=321,
                 attributes=root_attributes("11111111-1111-1111-1111-111111111111",
                                            "web", True, "wallet")),
    ])

    head = await trace_head(db, trace_id)
    assert head["root_span_id"] == root_id
    assert head["root_operation"] == "checkout"
    assert head["root_duration_ms"] == 321
    assert head["span_count"] == 2


# --- test 9 ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("channel", "has_promo", "payment_method", "status", "expected_checkout_status"),
    [
        ("mobile", True, "card", "OK", "CONFIRMED"),
        ("aggregator", False, "other", "ERROR", "FAILED"),
    ],
)
async def test_transaction_dimensions_are_denormalized_onto_the_head(
    client, db, channel, has_promo, payment_method, status, expected_checkout_status
):
    """Blast radius reads `trace`, never `"order"` (v1.2 §3.4, RC4).

    The detector role cannot read `"order"` at all, and under Scenario C the row
    is missing precisely for the worst-affected transactions. These dimensions
    have to come off the root span.
    """
    trace_id = new_trace_id()
    order_id = "22222222-2222-2222-2222-222222222222"
    await post(client, [
        envelope(trace_id, status=status,
                 attributes=root_attributes(order_id, channel, has_promo, payment_method)),
    ])

    head = await trace_head(db, trace_id)
    assert str(head["order_id"]) == order_id
    assert head["channel"] == channel
    assert head["has_promo"] is has_promo
    assert head["payment_method"] == payment_method
    assert head["checkout_status"] == expected_checkout_status
    assert head["root_status"] == status


# --- test 10 --------------------------------------------------------------
async def test_span_older_than_last_reset_ts_is_fenced_and_counted(client, db):
    """**Invariant: no span whose `end_ts` precedes `last_reset_ts` is ingested.**

    This is what turns "no pre-reset span survives" from a timing assumption
    into something provable, and it is why Day 4's reset-race test can be
    deterministic rather than sleep-based.
    """
    from app.ingest.fence import fence

    original = fence.last_reset_ts
    fence._last_reset_ts = datetime.now(UTC)
    fenced_before = fence.fenced_total
    try:
        stale_trace = new_trace_id()
        fresh_trace = new_trace_id()
        result = await post(client, [
            envelope(stale_trace, end_ts=datetime.now(UTC) - timedelta(minutes=5)),
            envelope(stale_trace, end_ts=datetime.now(UTC) - timedelta(minutes=5),
                     operation="pricing", span_kind="INTERNAL"),
            envelope(fresh_trace, end_ts=datetime.now(UTC) + timedelta(seconds=1)),
        ])

        assert result == {"accepted": 1, "fenced": 2}
        assert fence.fenced_total == fenced_before + 2
        assert await trace_head(db, stale_trace) is None
        assert await trace_head(db, fresh_trace) is not None
    finally:
        fence._last_reset_ts = original


async def test_unknown_domain_is_rejected(client):
    response = await client.post("/internal/spans", json={
        "spans": [envelope(new_trace_id(), attribution_domain="not-a-domain")]
    })
    assert response.status_code == 400
    assert "not-a-domain" in response.text


async def test_malformed_batch_is_a_400_never_a_500(client):
    response = await client.post("/internal/spans", json={"spans": [{"trace_id": "x"}]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
