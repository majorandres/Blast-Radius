"""The frontend read API (v1.2 §5.1).

Everything the dashboard needs, and nothing that would tell a caller whether a
scenario is running. These endpoints are unauthenticated and identical for every
caller, which is what lets `scenario-controller` validate a reveal over the same
public route the frontend polls without observability being able to tell the
difference (§5.4).

All of it reads `trace`, `span`, and `incident`. None of it reads `"order"` --
the detector role cannot, and under a datastore fault the row is missing
precisely for the worst-affected transactions.
"""

from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request

from app.db import engine

router = APIRouter(prefix="/api")

#: v1.2 §23: resource exhaustion. The window is capped regardless of request.
MAX_TIMESERIES_MINUTES = 60

_HEALTH = sa.text(
    """
    SELECT
      count(*) FILTER (WHERE root_end_ts >= now() - interval '60 seconds') AS orders_last_min,
      count(*) AS n,
      count(*) FILTER (WHERE checkout_status = 'CONFIRMED')::numeric
        / NULLIF(count(*), 0) AS success_ratio,
      percentile_cont(0.95) WITHIN GROUP (ORDER BY root_duration_ms) AS p95_ms
    FROM trace
    WHERE root_span_id IS NOT NULL
      AND root_end_ts >= now() - make_interval(secs => :window_s)
    """
)

_ACTIVE_INCIDENT_STATE = sa.text(
    "SELECT state::text FROM incident WHERE state::text = ANY(ARRAY['OPEN','RECOVERING'])"
    " ORDER BY first_breach_ts DESC LIMIT 1"
)

_TOPOLOGY_NODES = sa.text(
    """
    SELECT d.id, d.name, d.kind::text AS kind, d.display_order,
           count(s.span_id) AS span_count,
           count(s.span_id) FILTER (WHERE s.status = 'ERROR') AS error_count,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY s.duration_ms) AS p95_ms
    FROM domain d
    LEFT JOIN span s ON s.attribution_domain_id = d.id
                    AND s.start_ts >= now() - make_interval(secs => :window_s)
    GROUP BY d.id, d.name, d.kind, d.display_order
    ORDER BY d.display_order
    """
)

_TOPOLOGY_EDGES = sa.text(
    "SELECT caller.name AS caller, callee.name AS callee"
    " FROM domain_edge e"
    " JOIN domain caller ON caller.id = e.caller_domain_id"
    " JOIN domain callee ON callee.id = e.callee_domain_id"
)

_TIMESERIES = sa.text(
    """
    SELECT to_timestamp(floor(extract(epoch FROM root_end_ts) / :bucket_s) * :bucket_s)
             AT TIME ZONE 'UTC' AS bucket,
           count(*) AS n,
           count(*) FILTER (WHERE checkout_status = 'CONFIRMED')::numeric
             / NULLIF(count(*), 0) AS success_ratio,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY root_duration_ms) AS p95_ms
    FROM trace
    WHERE root_span_id IS NOT NULL
      AND root_end_ts >= now() - make_interval(mins => :minutes)
    GROUP BY 1 ORDER BY 1
    """
)

_INCIDENTS = sa.text(
    """
    SELECT i.id, i.state::text AS state, i.first_breach_ts, i.opened_ts, i.closed_ts,
           i.severity, i.verdict::text AS verdict, d.name AS attributed_domain,
           i.attribution_share, i.candidate_trace_count,
           i.primary_dimension, i.primary_cohort, i.narrative, i.narrative_source
    FROM incident i LEFT JOIN domain d ON d.id = i.attributed_domain_id
    WHERE (:only_active = false OR i.state::text = ANY(ARRAY['OPEN','RECOVERING']))
    ORDER BY i.first_breach_ts DESC LIMIT 50
    """
)

_INCIDENT = sa.text(
    """
    SELECT i.id, i.state::text AS state, i.first_breach_ts, i.opened_ts, i.closed_ts,
           i.severity, i.verdict::text AS verdict, d.name AS attributed_domain,
           i.attribution_share, i.candidate_trace_count,
           i.primary_dimension, i.primary_cohort, i.narrative, i.narrative_source
    FROM incident i LEFT JOIN domain d ON d.id = i.attributed_domain_id
    WHERE i.id = :id
    """
)

_EVIDENCE = sa.text(
    "SELECT attribution_detail, baseline_snapshot, impact, concentration,"
    "       verdict::text AS verdict, candidate_trace_count, attribution_share,"
    "       primary_dimension, primary_cohort"
    " FROM incident WHERE id = :id"
)

_SYMPTOMS = sa.text(
    """
    SELECT s.name, count(*) AS breach_count,
           min(sym.breached_ts) AS first_breached_ts,
           max(sym.breached_ts) AS last_breached_ts,
           min(sym.observed_value) AS min_observed,
           max(sym.observed_value) AS max_observed
    FROM incident_symptom sym JOIN slo s ON s.id = sym.slo_id
    WHERE sym.incident_id = :id
    GROUP BY s.name ORDER BY s.name
    """
)


def _float(value) -> float | None:
    return float(value) if value is not None else None


@router.get("/health/current")
async def health_current(request: Request) -> dict:
    """The header strip: throughput, availability, latency, and one word for
    how the system is doing."""
    profile = request.app.state.profile
    async with engine().connect() as conn:
        row = (await conn.execute(_HEALTH, {"window_s": profile.slo_window_s})).mappings().one()
        incident_state = (await conn.execute(_ACTIVE_INCIDENT_STATE)).scalar()

    success = _float(row["success_ratio"])
    p95 = _float(row["p95_ms"])

    if incident_state == "OPEN":
        system_state: Literal["HEALTHY", "RECOVERING", "INCIDENT"] = "INCIDENT"
    elif incident_state == "RECOVERING":
        system_state = "RECOVERING"
    else:
        system_state = "HEALTHY"

    return {
        "orders_per_min": int(row["orders_last_min"] or 0),
        "checkout_success_pct": round(success * 100, 2) if success is not None else None,
        "p95_latency_ms": round(p95) if p95 is not None else None,
        "sample_count": int(row["n"] or 0),
        "system_state": system_state,
        "window_seconds": profile.slo_window_s,
    }


@router.get("/topology")
async def topology(request: Request) -> dict:
    """Domain-level topology, not process-level.

    Jaeger shows the two real processes. This shows the four logical failure
    domains, which is what attribution actually reasons about -- and why
    `payment-gateway` appears here with no process behind it.
    """
    profile = request.app.state.profile
    async with engine().connect() as conn:
        nodes = (await conn.execute(
            _TOPOLOGY_NODES, {"window_s": profile.slo_window_s}
        )).mappings().all()
        edges = (await conn.execute(_TOPOLOGY_EDGES)).mappings().all()

    return {
        "nodes": [
            {
                "name": n["name"],
                "kind": n["kind"],
                "span_count": int(n["span_count"] or 0),
                "error_count": int(n["error_count"] or 0),
                "error_pct": round(100.0 * n["error_count"] / n["span_count"], 2)
                if n["span_count"] else 0.0,
                "p95_ms": round(_float(n["p95_ms"])) if n["p95_ms"] is not None else None,
            }
            for n in nodes
        ],
        "edges": [{"caller": e["caller"], "callee": e["callee"]} for e in edges],
    }


@router.get("/timeseries")
async def timeseries(minutes: int = Query(default=15, ge=1, le=MAX_TIMESERIES_MINUTES)) -> dict:
    bucket_s = 15 if minutes <= 20 else 60
    async with engine().connect() as conn:
        rows = (await conn.execute(
            _TIMESERIES, {"minutes": minutes, "bucket_s": bucket_s}
        )).mappings().all()

    return {
        "bucket_seconds": bucket_s,
        "points": [
            {
                "ts": r["bucket"].replace(tzinfo=UTC).isoformat(),
                "orders": int(r["n"]),
                "checkout_success_pct": round(_float(r["success_ratio"]) * 100, 2)
                if r["success_ratio"] is not None else None,
                "p95_latency_ms": round(_float(r["p95_ms"]))
                if r["p95_ms"] is not None else None,
            }
            for r in rows
        ],
    }


def _incident_dict(row) -> dict:
    return {
        "id": str(row["id"]),
        "state": row["state"],
        "first_breach_ts": row["first_breach_ts"].isoformat(),
        "opened_ts": row["opened_ts"].isoformat() if row["opened_ts"] else None,
        "closed_ts": row["closed_ts"].isoformat() if row["closed_ts"] else None,
        "severity": row["severity"],
        "verdict": row["verdict"],
        "attributed_domain": row["attributed_domain"],
        "attribution_share": _float(row["attribution_share"]),
        "candidate_trace_count": row["candidate_trace_count"],
        "primary_dimension": row["primary_dimension"],
        "primary_cohort": row["primary_cohort"],
        "narrative": row["narrative"],
        "narrative_source": row["narrative_source"],
    }


@router.get("/incidents")
async def list_incidents(state: str | None = Query(default=None)) -> list[dict]:
    async with engine().connect() as conn:
        rows = (await conn.execute(
            _INCIDENTS, {"only_active": state == "active"}
        )).mappings().all()
    return [_incident_dict(r) for r in rows]


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str) -> dict:
    async with engine().connect() as conn:
        row = (await conn.execute(_INCIDENT, {"id": incident_id})).mappings().first()
    if row is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "no such incident"})
    return _incident_dict(row)


@router.get("/incidents/{incident_id}/evidence")
async def incident_evidence(incident_id: str) -> dict:
    """Everything behind the verdict, for the drawer.

    Impact and concentration are returned side by side and never merged: they
    answer different questions, and conflating them is the mistake the split
    exists to prevent (§13.4).
    """
    async with engine().connect() as conn:
        row = (await conn.execute(_EVIDENCE, {"id": incident_id})).mappings().first()
        if row is None:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "no such incident"})
        symptoms = (await conn.execute(_SYMPTOMS, {"id": incident_id})).mappings().all()

    return {
        "verdict": row["verdict"],
        "attribution_share": _float(row["attribution_share"]),
        "candidate_trace_count": row["candidate_trace_count"],
        "attribution_detail": row["attribution_detail"],
        "baseline_snapshot": row["baseline_snapshot"],
        "impact": row["impact"],
        "concentration": row["concentration"],
        "primary_dimension": row["primary_dimension"],
        "primary_cohort": row["primary_cohort"],
        "symptoms": [
            {
                "name": s["name"],
                "breach_count": int(s["breach_count"]),
                "first_breached_ts": s["first_breached_ts"].isoformat(),
                "last_breached_ts": s["last_breached_ts"].isoformat(),
                "min_observed": _float(s["min_observed"]),
                "max_observed": _float(s["max_observed"]),
            }
            for s in symptoms
        ],
    }
