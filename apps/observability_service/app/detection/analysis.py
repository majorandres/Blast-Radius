"""Analyse an open incident: attribution, impact, concentration.

Runs on every detection pass while an incident is OPEN or RECOVERING, and
overwrites its own previous answer. The incident window grows as the incident
runs, so the diagnosis is expected to sharpen — but the *baseline* it is
measured against was frozen at open and never moves.

Nothing here knows a scenario exists.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from blastradius_contracts.profiles import DetectionProfile
from sqlalchemy.ext.asyncio import AsyncConnection

from app.blast_radius.concentration import (
    CohortConcentration,
    build_concentration,
    primary_dimension,
)
from app.blast_radius.impact import DIMENSIONS, CohortImpact, build_impact, cohort_stats
from app.detection.attribution import AttributionResult, aggregate, load_candidates
from app.detection.incidents import Incident

log = logging.getLogger(__name__)

_LOAD_INCIDENT = sa.text(
    "SELECT opened_ts, first_breach_ts, baseline_snapshot FROM incident WHERE id = :id"
)

_PERSIST = sa.text(
    """
    UPDATE incident SET
      verdict               = CAST(:verdict AS attribution_verdict),
      attributed_domain_id  = :domain_id,
      attribution_share     = :share,
      candidate_trace_count = :candidate_count,
      attribution_detail    = CAST(:detail AS jsonb),
      impact                = CAST(:impact AS jsonb),
      concentration         = CAST(:concentration AS jsonb),
      primary_dimension     = :primary_dimension,
      primary_cohort        = :primary_cohort
    WHERE id = :id
    """
)


@dataclass(frozen=True)
class Analysis:
    attribution: AttributionResult
    impact: list[CohortImpact]
    concentration: list[CohortConcentration]
    primary_dimension: str | None
    primary_cohort: str | None
    total_traces: int
    total_abnormal: int
    threshold_ms: float


async def analyse(
    conn: AsyncConnection, incident: Incident, profile: DetectionProfile
) -> Analysis | None:
    row = (await conn.execute(_LOAD_INCIDENT, {"id": incident.id})).mappings().first()
    if row is None or row["opened_ts"] is None:
        return None

    baseline = row["baseline_snapshot"] or {}
    threshold_ms = float(baseline.get("abnormal_latency_threshold_ms") or 500.0)
    opened_ts: datetime = row["opened_ts"]
    now = datetime.now(UTC)

    trees = await load_candidates(
        conn, opened_ts=opened_ts, settle_s=profile.trace_settle_s, threshold_ms=threshold_ms
    )
    attribution = aggregate(trees)

    baseline_start = datetime.fromisoformat(baseline["window_start"])
    baseline_end = datetime.fromisoformat(baseline["window_end"])

    impact: list[CohortImpact] = []
    concentration: list[CohortConcentration] = []
    total_traces = 0
    total_abnormal = 0

    for index, dimension in enumerate(DIMENSIONS):
        base = await cohort_stats(
            conn, dimension, window_start=baseline_start, window_end=baseline_end,
            threshold_ms=threshold_ms,
        )
        live = await cohort_stats(
            conn, dimension, window_start=opened_ts, window_end=now, threshold_ms=threshold_ms
        )
        # Every dimension partitions the same traces, so the totals are read
        # once rather than summed per dimension.
        if index == 0:
            total_traces = sum(c.n for c in live.values())
            total_abnormal = sum(c.abnormal_n for c in live.values())

        from app.blast_radius.impact import CohortStats

        empty = CohortStats(n=0, failure_rate=None, p95_ms=None)
        for value in sorted(set(base) | set(live)):
            impact.append(build_impact(
                dimension, value, base.get(value, empty), live.get(value, empty),
                profile.min_cohort_n,
            ))
            concentration.append(build_concentration(
                dimension, value, live.get(value, empty),
                total_traces=total_traces, total_abnormal=total_abnormal,
                min_cohort_n=profile.min_cohort_n,
                min_abnormal_traces=profile.min_abnormal_traces,
            ))

    dimension_name, cohort_value = primary_dimension(concentration)

    return Analysis(
        attribution=attribution,
        impact=impact,
        concentration=concentration,
        primary_dimension=dimension_name,
        primary_cohort=cohort_value,
        total_traces=total_traces,
        total_abnormal=total_abnormal,
        threshold_ms=threshold_ms,
    )


async def analyse_and_persist(
    conn: AsyncConnection, incident: Incident, profile: DetectionProfile
) -> Analysis | None:
    analysis = await analyse(conn, incident, profile)
    if analysis is None:
        return None

    a = analysis.attribution
    await conn.execute(_PERSIST, {
        "id": incident.id,
        "verdict": a.verdict,
        "domain_id": a.domain_id if a.verdict == "ATTRIBUTED" else None,
        "share": round(a.share, 4),
        "candidate_count": a.candidate_count,
        "detail": json.dumps({
            "counts": {str(k): v for k, v in a.counts.items()},
            "paths": a.paths,
            "unattributed": a.unattributed,
            "runner_up_domain_id": a.runner_up_id,
            "runner_up_share": round(a.runner_up_share, 4),
            "abnormal_latency_threshold_ms": analysis.threshold_ms,
            "total_traces": analysis.total_traces,
            "total_abnormal": analysis.total_abnormal,
        }),
        "impact": json.dumps([i.as_dict() for i in analysis.impact]),
        "concentration": json.dumps([c.as_dict() for c in analysis.concentration]),
        "primary_dimension": analysis.primary_dimension,
        "primary_cohort": analysis.primary_cohort,
    })

    log.info(
        "incident %s: %s share=%.2f candidates=%s paths=%s primary=%s/%s",
        incident.id, a.verdict, a.share, a.candidate_count, a.paths,
        analysis.primary_dimension, analysis.primary_cohort,
    )
    return analysis
