"""Attribution (v1.2 §12).

Given an incident, decide which failure *domain* explains it. Two paths, chosen
per trace by how that trace went wrong: an error walk for traces that failed, a
self-time dominance test for traces that were merely slow.

The whole algorithm operates on `attribution_domain`, never on
`emitting_service`. That distinction is the reason a promo call that timed out
with no server span still attributes to `promo-provider`, and the reason
`payment-gateway` is a legal answer despite no such process existing.

Two red herrings exist to break naive versions of this:

- `loyalty_tier_lookup` has the largest *relative* latency rise in the system
  under load and is never the culprit, because it is a rounding error against a
  multi-second trace. A multiplier-based detector picks it every time.
- `analytics.publish` fails independently of the checkout outcome and is
  non-blocking. A detector that takes "the deepest ERROR span anywhere" blames
  `ordering-app` on traces that succeeded.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger(__name__)

#: v1.2 §12.4. A span must own at least this share of the root's wall time
#: before it is called the cause of a slow trace.
DOMINANCE = 0.30

#: v1.2 §12.5.
MIN_ATTRIBUTION_SHARE = 0.40
MIN_RUNNER_UP_GAP = 0.15

#: Fewest abnormal traces that can carry a verdict. Not in v1.2; recorded as a
#: deviation. Without it the first pass after an incident opens reports "100% of
#: 3" -- a number that reads as certainty and is an artifact of the sample size.
#:
#: Deliberately lower than the profile's `min_abnormal_traces`, which gates
#: *concentration*. Attribution ranks one dimension (which domain) and needs
#: less evidence than partitioning traffic into cohorts to ask which of them
#: explains the abnormality.
MIN_CANDIDATES_FOR_VERDICT = 5


@dataclass(frozen=True)
class SpanNode:
    span_id: str
    parent_span_id: str | None
    domain_id: int
    domain_name: str
    operation: str
    span_kind: str
    status: str
    blocking: bool
    start: datetime
    end: datetime
    duration_ms: int


@dataclass
class TraceTree:
    trace_id: str
    root_status: str
    root_duration_ms: int
    spans: list[SpanNode]
    children: dict[str | None, list[SpanNode]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        present = {s.span_id for s in self.spans}
        true_root = next((s for s in self.spans if s.parent_span_id is None), None)
        root_id = true_root.span_id if true_root else None

        for span in self.spans:
            if span.parent_span_id is None:
                key = None
            elif span.parent_span_id in present:
                key = span.parent_span_id
            else:
                # An orphan: its parent was never ingested. It attaches to the
                # root, which is v1.2 §12.6's "nearest present ancestor" at the
                # only depth establishable without the missing span. It must not
                # become a second root -- that would hide it from the error walk
                # and make `root` ambiguous.
                key = root_id
            self.children.setdefault(key, []).append(span)

    @property
    def root(self) -> SpanNode | None:
        roots = self.children.get(None, [])
        return roots[0] if roots else None

    def kids(self, span: SpanNode) -> list[SpanNode]:
        return [c for c in self.children.get(span.span_id, []) if c is not span]


# --- §12.2 self time -------------------------------------------------------
def self_time_ms(span: SpanNode, children: list[SpanNode]) -> float:
    """Duration minus the union of child intervals, clipped to the parent.

    The union matters. Summing child durations double-counts concurrency and
    yields negative self time whenever children overlap, which then reads as
    "this span did nothing" for precisely the spans that fanned out work.
    """
    clipped = []
    for child in children:
        start = max(child.start, span.start)
        end = min(child.end, span.end)
        if end > start:
            clipped.append((start, end))

    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(clipped):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    covered_ms = sum((end - start).total_seconds() * 1000 for start, end in merged)
    return max(0.0, span.duration_ms - covered_ms)


# --- §12.3 error path ------------------------------------------------------
def culprit_error(tree: TraceTree) -> SpanNode | None:
    """Walk down the connected chain of blocking ERROR spans.

    Three properties, each load-bearing:

    - only *blocking* children, so `analytics.publish` can never be the culprit;
    - only a chain connected to the root, so a handled error whose parent
      succeeded is not blamed;
    - the domain of wherever the walk stops, so a client span whose peer never
      responded still attributes to the peer.
    """
    node = tree.root
    if node is None:
        return None
    while True:
        failing = [c for c in tree.kids(node) if c.status == "ERROR" and c.blocking]
        if not failing:
            return node
        node = max(failing, key=lambda c: c.duration_ms)


def attribute_error(tree: TraceTree) -> tuple[int | None, str]:
    culprit = culprit_error(tree)
    return (culprit.domain_id if culprit else None), "error"


# --- §12.4 latency path ----------------------------------------------------
def culprit_latency(tree: TraceTree) -> SpanNode | None:
    """Blame the span that owns the most wall time, if it owns enough of it.

    Below the dominance floor the trace has no single cause -- it was slow all
    over -- and saying so is more useful than naming whichever span happened to
    edge ahead.
    """
    root = tree.root
    if root is None or root.duration_ms <= 0:
        return None

    times = {s.span_id: self_time_ms(s, tree.kids(s)) for s in tree.spans if s.blocking}
    if not times:
        return None

    span_by_id = {s.span_id: s for s in tree.spans}
    span_id, best = max(times.items(), key=lambda kv: kv[1])
    if best / root.duration_ms < DOMINANCE:
        return None
    return span_by_id[span_id]


def attribute_latency(tree: TraceTree) -> tuple[int | None, str]:
    culprit = culprit_latency(tree)
    return (culprit.domain_id if culprit else None), "latency"


# --- §12.5 aggregation -----------------------------------------------------
@dataclass(frozen=True)
class AttributionResult:
    verdict: str
    domain_id: int | None
    share: float
    runner_up_id: int | None
    runner_up_share: float
    candidate_count: int
    unattributed: int
    counts: dict[int, int]
    paths: dict[str, int]
    culprit_operations: dict[str, int]
    culprit_kinds: dict[str, int]


def aggregate(trees: list[TraceTree], min_candidates: int = 0) -> AttributionResult:
    """Rank domains across the abnormal population.

    `min_candidates` guards against a verdict that is arithmetically confident
    and evidentially thin. Seconds after an incident opens the window holds a
    handful of traces, and three of them agreeing yields "100% of 3" -- a
    number that reads as certainty and is really an artifact of the sample
    size. Below the floor the answer is NO_DIAGNOSIS, which is what the system
    actually knows at that point.
    """
    counts: Counter[int] = Counter()
    paths: Counter[str] = Counter()
    operations: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    unattributed = 0

    for tree in trees:
        error_path = tree.root_status == "ERROR"
        culprit = culprit_error(tree) if error_path else culprit_latency(tree)
        paths["error" if error_path else "latency"] += 1
        if culprit is None:
            unattributed += 1
        else:
            counts[culprit.domain_id] += 1
            operations[culprit.operation] += 1
            kinds[culprit.span_kind] += 1

    total = len(trees)
    ranked = counts.most_common()
    share = ranked[0][1] / total if ranked and total else 0.0
    runner_up_share = ranked[1][1] / total if len(ranked) > 1 and total else 0.0

    if total < min_candidates:
        verdict = "NO_DIAGNOSIS"
    elif not ranked or share < MIN_ATTRIBUTION_SHARE:
        verdict = "NO_DIAGNOSIS"
    elif share - runner_up_share < MIN_RUNNER_UP_GAP:
        verdict = "AMBIGUOUS"
    else:
        verdict = "ATTRIBUTED"

    return AttributionResult(
        verdict=verdict,
        domain_id=ranked[0][0] if ranked else None,
        share=share,
        runner_up_id=ranked[1][0] if len(ranked) > 1 else None,
        runner_up_share=runner_up_share,
        candidate_count=total,
        unattributed=unattributed,
        counts=dict(counts),
        paths=dict(paths),
        culprit_operations=dict(operations),
        culprit_kinds=dict(kinds),
    )


# --- §12.1 candidate selection --------------------------------------------
_CANDIDATES = sa.text(
    """
    SELECT trace_id, root_status::text AS root_status, root_duration_ms
    FROM trace
    WHERE root_span_id IS NOT NULL
      AND root_end_ts >= :opened_ts
      AND now() - last_span_ts > make_interval(secs => :settle_s)
      AND (root_status = 'ERROR' OR root_duration_ms > :threshold_ms)
    """
)

_SPANS = sa.text(
    """
    SELECT s.trace_id, s.span_id, s.parent_span_id, s.attribution_domain_id,
           d.name AS domain_name, s.operation, s.span_kind::text AS span_kind,
           s.status::text AS status, s.blocking, s.start_ts, s.end_ts, s.duration_ms
    FROM span s JOIN domain d ON d.id = s.attribution_domain_id
    WHERE s.trace_id = ANY(:trace_ids)
    """
)


async def load_candidates(
    conn: AsyncConnection, *, opened_ts: datetime, settle_s: int, threshold_ms: float
) -> list[TraceTree]:
    # `opened_ts` here is the incident's start -- its first breach -- not the
    # moment it was confirmed open. See analysis.py.
    """The abnormal population: ERROR **or** slower than the frozen threshold.

    This exact set is shared with concentration (v1.2 §13.2). One definition of
    "abnormal" exists in the system, which is what lets a fail-slow incident be
    characterised at all -- under pool saturation there are almost no failures
    to count.
    """
    rows = (await conn.execute(_CANDIDATES, {
        "opened_ts": opened_ts, "settle_s": settle_s, "threshold_ms": threshold_ms,
    })).mappings().all()
    if not rows:
        return []

    heads = {r["trace_id"]: r for r in rows}
    span_rows = (await conn.execute(
        _SPANS, {"trace_ids": list(heads)}
    )).mappings().all()

    by_trace: dict[str, list[SpanNode]] = {}
    for r in span_rows:
        by_trace.setdefault(r["trace_id"], []).append(SpanNode(
            span_id=r["span_id"], parent_span_id=r["parent_span_id"],
            domain_id=r["attribution_domain_id"], domain_name=r["domain_name"],
            operation=r["operation"], span_kind=r["span_kind"], status=r["status"],
            blocking=r["blocking"], start=r["start_ts"], end=r["end_ts"],
            duration_ms=r["duration_ms"],
        ))

    return [
        TraceTree(
            trace_id=trace_id,
            root_status=head["root_status"],
            root_duration_ms=head["root_duration_ms"],
            spans=by_trace.get(trace_id, []),
        )
        for trace_id, head in heads.items()
        if by_trace.get(trace_id)
    ]
