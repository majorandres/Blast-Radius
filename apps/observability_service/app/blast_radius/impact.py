"""Impact: did this cohort degrade, and how (v1.2 §13.1, FINAL-01).

Three verdicts per cohort — availability, latency, and a derived overall.

v1.1 compared failure rates only. That contradicts a fail-slow incident
outright: under pool saturation latency degrades long before availability does,
so every cohort would read UNAFFECTED during exactly the phase the scenario
exists to demonstrate. Measuring latency separately is what makes "slow, but not
failing" a statement the system can make.

The overall verdict is *derived*, never independently measured. Two measurements
and a rule beat three measurements that can disagree with each other.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

Verdict = Literal["AFFECTED", "DEGRADED", "UNAFFECTED", "INSUFFICIENT_DATA"]

#: v1.2 §23: cohort dimensions come from a hardcoded allowlist, never from
#: request input. These are interpolated into SQL as column names, so the
#: allowlist is the injection defence.
DIMENSIONS: tuple[str, ...] = ("channel", "has_promo", "payment_method")

SEVERITY = {"UNAFFECTED": 0, "DEGRADED": 1, "AFFECTED": 2}


@dataclass(frozen=True)
class CohortStats:
    n: int
    failure_rate: float | None
    p95_ms: float | None
    abnormal_n: int = 0


@dataclass(frozen=True)
class CohortImpact:
    dimension: str
    value: str
    baseline_n: int
    incident_n: int
    baseline_failure_rate: float | None
    incident_failure_rate: float | None
    baseline_p95_ms: float | None
    incident_p95_ms: float | None
    availability_verdict: Verdict
    latency_verdict: Verdict
    overall_verdict: Verdict

    def as_dict(self) -> dict:
        return asdict(self)


def availability_verdict(
    base_rate: float | None, base_n: int, inc_rate: float | None, inc_n: int, min_n: int
) -> Verdict:
    if base_n < min_n or inc_n < min_n or base_rate is None or inc_rate is None:
        return "INSUFFICIENT_DATA"
    if inc_rate >= max(base_rate + 0.10, base_rate * 3.0):
        return "AFFECTED"
    if inc_rate <= base_rate + 0.02:
        return "UNAFFECTED"
    return "DEGRADED"


def latency_verdict(
    base_p95: float | None, base_n: int, inc_p95: float | None, inc_n: int, min_n: int
) -> Verdict:
    """Both a multiplicative and an absolute rise are required.

    The `max` is the point: without the absolute floor, a cohort with a small
    baseline gets flagged AFFECTED for a jump nobody would notice. At a 400ms
    baseline, AFFECTED needs >=900ms and UNAFFECTED means <=480ms.
    """
    if base_n < min_n or inc_n < min_n or base_p95 is None or inc_p95 is None:
        return "INSUFFICIENT_DATA"
    if inc_p95 >= max(base_p95 * 2.0, base_p95 + 500.0):
        return "AFFECTED"
    if inc_p95 <= max(base_p95 * 1.2, base_p95 + 50.0):
        return "UNAFFECTED"
    return "DEGRADED"


def overall_verdict(availability: Verdict, latency: Verdict) -> Verdict:
    """The worse of the two known verdicts. INSUFFICIENT_DATA never masks a
    finding: a cohort that is measurably slow is AFFECTED even if its
    availability could not be judged."""
    known = [v for v in (availability, latency) if v != "INSUFFICIENT_DATA"]
    if not known:
        return "INSUFFICIENT_DATA"
    return max(known, key=lambda v: SEVERITY[v])


def build_impact(
    dimension: str, value: str, baseline: CohortStats, incident: CohortStats, min_n: int
) -> CohortImpact:
    availability = availability_verdict(
        baseline.failure_rate, baseline.n, incident.failure_rate, incident.n, min_n
    )
    latency = latency_verdict(
        baseline.p95_ms, baseline.n, incident.p95_ms, incident.n, min_n
    )
    return CohortImpact(
        dimension=dimension,
        value=value,
        baseline_n=baseline.n,
        incident_n=incident.n,
        baseline_failure_rate=baseline.failure_rate,
        incident_failure_rate=incident.failure_rate,
        baseline_p95_ms=baseline.p95_ms,
        incident_p95_ms=incident.p95_ms,
        availability_verdict=availability,
        latency_verdict=latency,
        overall_verdict=overall_verdict(availability, latency),
    )


def _cohort_query(dimension: str) -> sa.TextClause:
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown cohort dimension: {dimension!r}")
    return sa.text(
        f"""
        SELECT {dimension}::text AS value,
               count(*) AS n,
               count(*) FILTER (WHERE checkout_status = 'FAILED')::numeric
                 / NULLIF(count(*), 0) AS failure_rate,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY root_duration_ms) AS p95_ms,
               count(*) FILTER (
                 WHERE root_status = 'ERROR' OR root_duration_ms > :threshold_ms
               ) AS abnormal_n
        FROM trace
        WHERE root_span_id IS NOT NULL
          AND root_end_ts >= :window_start
          AND root_end_ts <  :window_end
          AND {dimension} IS NOT NULL
        GROUP BY {dimension}
        """  # noqa: S608 - dimension is allowlisted above, never request input
    )


async def cohort_stats(
    conn: AsyncConnection,
    dimension: str,
    *,
    window_start: datetime,
    window_end: datetime,
    threshold_ms: float,
) -> dict[str, CohortStats]:
    rows = (
        await conn.execute(
            _cohort_query(dimension),
            {
                "window_start": window_start,
                "window_end": window_end,
                "threshold_ms": threshold_ms,
            },
        )
    ).mappings().all()
    return {
        r["value"]: CohortStats(
            n=int(r["n"]),
            failure_rate=float(r["failure_rate"]) if r["failure_rate"] is not None else None,
            p95_ms=float(r["p95_ms"]) if r["p95_ms"] is not None else None,
            abnormal_n=int(r["abnormal_n"] or 0),
        )
        for r in rows
    }
