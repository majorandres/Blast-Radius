"""Attribution algorithm (v1.2 §12, §21.1).

Pure functions on synthetic trees. No database, no timing, no live system —
every case here is a fixture built to isolate one decision, including the two
red herrings that exist specifically to defeat naive implementations.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.detection.attribution import (
    DOMINANCE,
    SpanNode,
    TraceTree,
    aggregate,
    attribute_error,
    attribute_latency,
    self_time_ms,
)

T0 = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)

ORDERING_APP = 1
PROMO_PROVIDER = 2
PAYMENT_GATEWAY = 3
ORDER_DATASTORE = 4

DOMAIN_NAMES = {
    ORDERING_APP: "ordering-app",
    PROMO_PROVIDER: "promo-provider",
    PAYMENT_GATEWAY: "payment-gateway",
    ORDER_DATASTORE: "order-datastore",
}


def span(
    span_id: str,
    parent: str | None,
    *,
    domain: int = ORDERING_APP,
    operation: str = "op",
    at_ms: int = 0,
    duration_ms: int = 10,
    status: str = "OK",
    blocking: bool = True,
    kind: str = "INTERNAL",
) -> SpanNode:
    start = T0 + timedelta(milliseconds=at_ms)
    return SpanNode(
        span_id=span_id,
        parent_span_id=parent,
        domain_id=domain,
        domain_name=DOMAIN_NAMES[domain],
        operation=operation,
        span_kind=kind,
        status=status,
        blocking=blocking,
        start=start,
        end=start + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms,
    )


def tree(spans: list[SpanNode], *, root_status: str = "OK") -> TraceTree:
    root = next(s for s in spans if s.parent_span_id is None)
    return TraceTree(
        trace_id="t" * 32,
        root_status=root_status,
        root_duration_ms=root.duration_ms,
        spans=spans,
    )


# --- §12.2 self time -------------------------------------------------------
def test_self_time_subtracts_a_single_child():
    parent = span("a", None, duration_ms=100)
    child = span("b", "a", at_ms=10, duration_ms=40)
    assert self_time_ms(parent, [child]) == pytest.approx(60.0)


def test_concurrent_children_are_unioned_not_summed():
    """Summing child durations here would give 100 - 80 = 20ms. The children
    overlap, so only 50ms of wall time is actually covered."""
    parent = span("a", None, duration_ms=100)
    kids = [
        span("b", "a", at_ms=10, duration_ms=40),   # 10..50
        span("c", "a", at_ms=20, duration_ms=40),   # 20..60
    ]
    assert self_time_ms(parent, kids) == pytest.approx(50.0)


def test_nested_and_disjoint_children_are_both_covered():
    parent = span("a", None, duration_ms=100)
    kids = [
        span("b", "a", at_ms=0, duration_ms=30),    # 0..30
        span("c", "a", at_ms=50, duration_ms=20),   # 50..70
    ]
    assert self_time_ms(parent, kids) == pytest.approx(50.0)


def test_a_child_overrunning_the_parent_is_clipped():
    parent = span("a", None, at_ms=0, duration_ms=50)
    child = span("b", "a", at_ms=40, duration_ms=100)   # runs to 140, clipped at 50
    assert self_time_ms(parent, [child]) == pytest.approx(40.0)


def test_self_time_never_goes_negative():
    parent = span("a", None, duration_ms=10)
    kids = [span("b", "a", at_ms=0, duration_ms=200)]
    assert self_time_ms(parent, kids) == 0.0


# --- §12.3 error path ------------------------------------------------------
def test_error_walk_descends_to_the_deepest_blocking_error():
    spans = [
        span("root", None, operation="checkout", duration_ms=2000, status="ERROR"),
        span("promo", "root", domain=PROMO_PROVIDER, operation="promo.apply",
             duration_ms=1900, status="ERROR", kind="CLIENT"),
    ]
    domain, path = attribute_error(tree(spans, root_status="ERROR"))
    assert (domain, path) == (PROMO_PROVIDER, "error")


def test_a_client_span_whose_peer_never_responded_still_attributes_to_the_peer():
    """CC-A (v1.2 §6.3). The promo call timed out, so no server span exists at
    all. Attribution must land on promo-provider anyway — this is the single
    most important property of the two-identity design."""
    spans = [
        span("root", None, operation="checkout", duration_ms=2100, status="ERROR"),
        span("promo", "root", domain=PROMO_PROVIDER, operation="promo.apply",
             duration_ms=2000, status="ERROR", kind="CLIENT"),
    ]
    assert [s.operation for s in spans if s.domain_id == PROMO_PROVIDER] == ["promo.apply"]
    domain, _ = attribute_error(tree(spans, root_status="ERROR"))
    assert domain == PROMO_PROVIDER


def test_herring_two_a_non_blocking_failure_is_never_the_culprit(caplog):
    """`analytics.publish` fails independently of the checkout outcome.

    A detector taking "the deepest ERROR span anywhere" blames ordering-app.
    Excluding non-blocking spans is what stops it.
    """
    spans = [
        span("root", None, operation="checkout", duration_ms=2000, status="ERROR"),
        span("promo", "root", domain=PROMO_PROVIDER, operation="promo.apply",
             duration_ms=1900, status="ERROR", kind="CLIENT"),
        span("analytics", "root", operation="analytics.publish", duration_ms=5,
             status="ERROR", blocking=False),
    ]
    domain, _ = attribute_error(tree(spans, root_status="ERROR"))
    assert domain == PROMO_PROVIDER, "the non-blocking failure must be ignored"


def test_a_handled_error_whose_parent_succeeded_is_not_the_culprit():
    """The walk follows a *connected* chain from the root. A failure the
    application caught and recovered from is not what broke the checkout."""
    spans = [
        span("root", None, operation="checkout", duration_ms=2000, status="ERROR"),
        span("payment", "root", domain=PAYMENT_GATEWAY, operation="payment.authorize",
             duration_ms=1800, status="ERROR", kind="CLIENT"),
        span("pricing", "root", operation="pricing", duration_ms=100, status="OK"),
        span("retry", "pricing", domain=ORDER_DATASTORE, operation="db.pool_acquire",
             duration_ms=90, status="ERROR", kind="CLIENT"),
    ]
    domain, _ = attribute_error(tree(spans, root_status="ERROR"))
    assert domain == PAYMENT_GATEWAY


def test_the_root_itself_can_be_the_culprit():
    spans = [span("root", None, operation="checkout", duration_ms=50, status="ERROR")]
    domain, _ = attribute_error(tree(spans, root_status="ERROR"))
    assert domain == ORDERING_APP


# --- §12.4 latency path ----------------------------------------------------
def test_latency_blames_the_span_owning_the_most_wall_time():
    spans = [
        span("root", None, operation="checkout", duration_ms=1000),
        span("db", "root", domain=ORDER_DATASTORE, operation="db.pool_acquire",
             at_ms=10, duration_ms=800, kind="CLIENT"),
    ]
    domain, path = attribute_latency(tree(spans))
    assert (domain, path) == (ORDER_DATASTORE, "latency")


def test_herring_one_a_large_relative_rise_on_a_tiny_span_is_not_the_cause():
    """`loyalty_tier_lookup` goes 8ms -> 45ms under load: the largest relative
    rise in the system, and about one percent of a multi-second trace.

    A multiplier-based detector picks it every time. Dominance over the root's
    wall time is what makes it a non-answer.
    """
    spans = [
        span("root", None, operation="checkout", duration_ms=3500),
        span("pricing", "root", operation="pricing", at_ms=10, duration_ms=60),
        span("loyalty", "pricing", operation="loyalty_tier_lookup", at_ms=15, duration_ms=45),
        span("promo", "root", domain=PROMO_PROVIDER, operation="promo.apply",
             at_ms=100, duration_ms=3300, kind="CLIENT"),
    ]
    domain, _ = attribute_latency(tree(spans))
    assert domain == PROMO_PROVIDER
    culprit_ops = [s.operation for s in spans if s.domain_id == domain]
    assert "loyalty_tier_lookup" not in culprit_ops


@pytest.mark.parametrize(
    ("candidate_ms", "expected"),
    [(299, None), (300, ORDER_DATASTORE), (301, ORDER_DATASTORE)],
)
def test_latency_dominance_boundary(candidate_ms, expected):
    """DOMINANCE is 0.30 of a 1000ms root, so 300ms is the exact edge.

    The rest of the root is covered by filler children small enough that none
    of them out-scores the candidate, and large enough together that the root's
    own self time does not win instead -- the root is a legal culprit, so a
    naive fixture measures the wrong span.
    """
    assert DOMINANCE == 0.30
    filler_total = 1000 - candidate_ms
    filler_each = filler_total // 3
    spans = [
        span("root", None, operation="checkout", duration_ms=1000),
        span("db", "root", domain=ORDER_DATASTORE, at_ms=0, duration_ms=candidate_ms,
             kind="CLIENT"),
    ]
    for i in range(3):
        spans.append(span(f"filler{i}", "root", at_ms=candidate_ms + i * filler_each,
                          duration_ms=filler_each))
    domain, _ = attribute_latency(tree(spans))
    assert domain == expected


def test_a_trace_slow_all_over_has_no_single_cause():
    """Four spans each owning a quarter of the trace. Naming one would be a
    guess, so the trace goes unattributed and shows up as such."""
    spans = [span("root", None, operation="checkout", duration_ms=1000)] + [
        span(f"s{i}", "root", at_ms=i * 240, duration_ms=200) for i in range(4)
    ]
    domain, _ = attribute_latency(tree(spans))
    assert domain is None


# --- §12.5 aggregation -----------------------------------------------------
def error_trace(domain: int) -> TraceTree:
    return tree(
        [
            span("root", None, operation="checkout", duration_ms=2000, status="ERROR"),
            span("dep", "root", domain=domain, duration_ms=1900, status="ERROR",
                 kind="CLIENT"),
        ],
        root_status="ERROR",
    )


@pytest.mark.parametrize(
    ("split", "expected"),
    [
        ((39, 25, 20, 16), "NO_DIAGNOSIS"),   # leader at 0.39
        ((40, 24, 20, 16), "ATTRIBUTED"),     # leader at 0.40, gap 0.16
    ],
)
def test_attribution_share_boundary(split, expected):
    """Below 0.40 the leader does not explain enough of the incident to be
    called the cause, however far ahead of second place it is.

    The remainder is spread across three domains so the leader still leads at
    0.39 -- a two-way split would hand the lead to the other side and test
    nothing.
    """
    domains = (PROMO_PROVIDER, PAYMENT_GATEWAY, ORDER_DATASTORE, ORDERING_APP)
    trees = [
        error_trace(domain)
        for domain, count in zip(domains, split, strict=True)
        for _ in range(count)
    ]
    result = aggregate(trees)
    assert result.candidate_count == 100
    assert result.domain_id == PROMO_PROVIDER, "the leader must be the one under test"
    assert result.share == pytest.approx(split[0] / 100)
    assert result.verdict == expected


def test_a_close_second_place_is_ambiguous_not_attributed():
    """45 vs 42 of 100: the leader clears 0.40 but the gap is under 0.15.
    Naming a winner here would be a coin flip presented as a diagnosis."""
    trees = [error_trace(PROMO_PROVIDER) for _ in range(45)]
    trees += [error_trace(PAYMENT_GATEWAY) for _ in range(42)]
    trees += [error_trace(ORDER_DATASTORE) for _ in range(13)]
    result = aggregate(trees)
    assert result.verdict == "AMBIGUOUS"
    assert result.domain_id == PROMO_PROVIDER
    assert result.runner_up_id == PAYMENT_GATEWAY


def test_a_clear_gap_is_attributed():
    trees = [error_trace(PROMO_PROVIDER) for _ in range(80)]
    trees += [error_trace(PAYMENT_GATEWAY) for _ in range(20)]
    result = aggregate(trees)
    assert result.verdict == "ATTRIBUTED"
    assert result.domain_id == PROMO_PROVIDER
    assert result.share == pytest.approx(0.80)


def test_zero_candidates_is_no_diagnosis():
    result = aggregate([])
    assert result.verdict == "NO_DIAGNOSIS"
    assert result.candidate_count == 0
    assert result.domain_id is None


def test_unattributable_traces_are_counted_not_hidden():
    """Traces with no dominant span still count toward the denominator. Dropping
    them would inflate the leader's share and manufacture confidence."""
    spread = tree([span("root", None, duration_ms=1000)] + [
        span(f"s{i}", "root", at_ms=i * 240, duration_ms=200) for i in range(4)
    ])
    result = aggregate([error_trace(PROMO_PROVIDER)] + [spread] * 3)
    assert result.unattributed == 3
    assert result.candidate_count == 4
    assert result.share == pytest.approx(0.25)
    assert result.verdict == "NO_DIAGNOSIS"


def test_an_orphan_span_attaches_to_the_root():
    """v1.2 §12.6. The parent was never ingested, so the span becomes a leaf of
    the nearest ancestor we can establish."""
    spans = [
        span("root", None, operation="checkout", duration_ms=2000, status="ERROR"),
        span("orphan", "missing-parent", domain=PROMO_PROVIDER, duration_ms=1900,
             status="ERROR", kind="SERVER"),
    ]
    domain, _ = attribute_error(tree(spans, root_status="ERROR"))
    assert domain == PROMO_PROVIDER
