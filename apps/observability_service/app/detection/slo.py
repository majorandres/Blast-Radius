"""SLO evaluation (v1.2 §11).

Two SLOs, and they are genuinely independent -- not a mirrored pair:

    checkout_success   confirmed / total            gte  0.98
    p95_latency        p95 of root_duration_ms      lte  1000ms

`error_rate` was removed in v1.1 as an exact mirror of `checkout_success`.
Keeping p95 separate is what lets the detector see a fail-slow incident at all:
under pool saturation, latency degrades badly while availability barely moves,
and a system watching only success rate would report everything healthy
throughout the phase that matters most.

Both metrics are read from `trace`, never from `"order"` -- the detector role
cannot read `"order"`, and under a datastore fault the row is missing precisely
for the worst-affected transactions.
"""

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

SLO_CHECKOUT_SUCCESS = 1
SLO_P95_LATENCY = 2

# One round trip for both metrics. The settle gate (v1.2 §7.3) excludes traces
# still receiving spans, so a trace mid-flight never counts as a failure.
_WINDOW_QUERY = sa.text(
    """
    SELECT
      count(*) AS n,
      count(*) FILTER (WHERE checkout_status = 'CONFIRMED')::numeric
        / NULLIF(count(*), 0)                                  AS success_ratio,
      percentile_cont(0.95) WITHIN GROUP (ORDER BY root_duration_ms) AS p95_ms
    FROM trace
    WHERE root_span_id IS NOT NULL
      AND root_end_ts >= now() - make_interval(secs => :window_s)
      AND now() - last_span_ts > make_interval(secs => :settle_s)
    """
)


@dataclass(frozen=True)
class SloReading:
    slo_id: int
    name: str
    observed: float | None
    breached: bool


@dataclass(frozen=True)
class Evaluation:
    """One pass of the evaluator.

    `evaluated` is false when the window held fewer than `slo_min_samples`
    traces. That is neither a breach nor a clean reading: it advances no
    counter and cannot open or close an incident. Treating thin data as
    healthy would hide an incident that arrives during a traffic lull.
    """

    ts: datetime
    sample_count: int
    evaluated: bool
    readings: tuple[SloReading, ...]

    @property
    def breached(self) -> bool:
        return self.evaluated and any(r.breached for r in self.readings)

    @property
    def breached_readings(self) -> tuple[SloReading, ...]:
        return tuple(r for r in self.readings if r.breached)


#: v1.2 S11. Held here, not in the database, so a boundary case can be asserted
#: without a window of traffic behind it.
CHECKOUT_SUCCESS_MIN = 0.98
P95_LATENCY_MAX_MS = 1000.0


def readings_for(success: float | None, p95_ms: float | None) -> tuple[SloReading, ...]:
    """Apply both comparators. Pure, so the boundaries are testable exactly.

    A `None` observation is not a breach. It means the window produced no value
    for that metric, which the sample floor has already ruled on.
    """
    return (
        SloReading(
            slo_id=SLO_CHECKOUT_SUCCESS,
            name="checkout_success",
            observed=success,
            breached=success is not None and success < CHECKOUT_SUCCESS_MIN,
        ),
        SloReading(
            slo_id=SLO_P95_LATENCY,
            name="p95_latency",
            observed=p95_ms,
            breached=p95_ms is not None and p95_ms > P95_LATENCY_MAX_MS,
        ),
    )


async def evaluate(
    conn: AsyncConnection, *, window_s: int, settle_s: int, min_samples: int, now: datetime
) -> Evaluation:
    row = (
        await conn.execute(_WINDOW_QUERY, {"window_s": window_s, "settle_s": settle_s})
    ).mappings().one()

    n = int(row["n"] or 0)
    if n < min_samples:
        return Evaluation(ts=now, sample_count=n, evaluated=False, readings=())

    success = float(row["success_ratio"]) if row["success_ratio"] is not None else None
    p95 = float(row["p95_ms"]) if row["p95_ms"] is not None else None

    return Evaluation(ts=now, sample_count=n, evaluated=True,
                      readings=readings_for(success, p95))


_BASELINE_QUERY = sa.text(
    """
    SELECT
      count(*) AS n,
      count(*) FILTER (WHERE checkout_status = 'FAILED')::numeric
        / NULLIF(count(*), 0)                                  AS failure_rate,
      percentile_cont(0.95) WITHIN GROUP (ORDER BY root_duration_ms) AS p95_ms
    FROM trace
    WHERE root_span_id IS NOT NULL
      AND root_end_ts >= :window_start
      AND root_end_ts <  :window_end
    """
)


async def baseline_snapshot(
    conn: AsyncConnection, *, first_breach_ts: datetime, window_s: int, guard_s: int
) -> dict[str, float | int | str | None]:
    """Freeze the pre-incident picture. Called once, on PENDING -> OPEN.

    The guard gap matters: the minutes immediately before the first breach are
    already contaminated by the fault ramping up, so including them would
    quietly raise the baseline and make the incident look smaller than it is.

    `abnormal_latency_threshold_ms` is frozen here rather than recomputed later,
    because attribution and concentration must both partition traces the same
    way for the whole life of the incident (v1.2 §12.1, §13.2).
    """
    from datetime import timedelta

    window_start = first_breach_ts - timedelta(seconds=window_s)
    window_end = first_breach_ts - timedelta(seconds=guard_s)

    row = (
        await conn.execute(
            _BASELINE_QUERY, {"window_start": window_start, "window_end": window_end}
        )
    ).mappings().one()

    p95 = float(row["p95_ms"]) if row["p95_ms"] is not None else None
    failure_rate = float(row["failure_rate"]) if row["failure_rate"] is not None else None

    return {
        "n": int(row["n"] or 0),
        "failure_rate": failure_rate,
        "p95_ms": p95,
        "abnormal_latency_threshold_ms": max((p95 or 0.0) * 3.0, 500.0),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }
