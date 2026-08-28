"""Incident lifecycle (v1.2 §10).

    PENDING -> OPEN -> RECOVERING -> CLOSED

Two consecutive breached evaluations open an incident. `breach_persistence`
exists because a single bad window is noise: at DEMO's 5s cadence, one slow
batch of traces would otherwise raise an incident every few minutes and the
healthy soak would never be clean.

All breaches during an open incident append `incident_symptom` rows rather than
creating new incidents. An incident is a thing that happened, not a thing that
was noticed repeatedly -- and Day 3's reveal scores against exactly one.

`baseline_snapshot` is frozen on PENDING -> OPEN and never recomputed. If it
were recomputed as the incident aged, the incident window would slowly become
its own baseline and impact would trend toward UNAFFECTED no matter how bad
things got.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from app.detection.slo import Evaluation, baseline_snapshot

log = logging.getLogger(__name__)

ACTIVE_STATES = ("PENDING", "OPEN", "RECOVERING")


@dataclass
class Incident:
    id: uuid.UUID
    state: str
    first_breach_ts: datetime
    opened_ts: datetime | None
    consecutive_breaches: int = 0
    consecutive_clean: int = 0


_SELECT_ACTIVE = sa.text(
    "SELECT id, state, first_breach_ts, opened_ts FROM incident"
    " WHERE state = ANY(:states) ORDER BY first_breach_ts DESC LIMIT 1"
)
_INSERT = sa.text(
    "INSERT INTO incident (id, state, first_breach_ts, severity)"
    " VALUES (:id, CAST(:state AS incident_state), :first_breach_ts, :severity)"
)
_SET_STATE = sa.text(
    "UPDATE incident SET state = CAST(:state AS incident_state) WHERE id = :id"
)
_OPEN = sa.text(
    "UPDATE incident SET state = 'OPEN', opened_ts = :opened_ts, severity = :severity,"
    " baseline_snapshot = CAST(:baseline AS jsonb) WHERE id = :id"
)
_CLOSE = sa.text(
    "UPDATE incident SET state = 'CLOSED', closed_ts = :closed_ts WHERE id = :id"
)
_ADD_SYMPTOM = sa.text(
    "INSERT INTO incident_symptom (incident_id, slo_id, breached_ts, observed_value)"
    " VALUES (:incident_id, :slo_id, :breached_ts, :observed_value)"
    " ON CONFLICT (incident_id, slo_id, breached_ts) DO NOTHING"
)


def severity_of(evaluation: Evaluation) -> str:
    """Both SLOs breached is worse than one. Nothing subtler is claimed."""
    return "high" if len(evaluation.breached_readings) > 1 else "medium"


class IncidentTracker:
    """Holds the consecutive-evaluation counters between passes.

    These live in memory rather than in the database because they describe the
    evaluator's run, not the incident. On restart the counters reset and the
    detector simply needs two more breached windows -- which is the correct
    conservative behaviour, not a lost fact.
    """

    def __init__(self, *, breach_persistence: int, recovery_persistence: int) -> None:
        self._breach_persistence = breach_persistence
        self._recovery_persistence = recovery_persistence
        self._current: Incident | None = None

    @property
    def current(self) -> Incident | None:
        return self._current

    async def load(self, conn: AsyncConnection) -> None:
        row = (await conn.execute(_SELECT_ACTIVE, {"states": list(ACTIVE_STATES)})).first()
        if row is not None:
            self._current = Incident(
                id=row[0], state=row[1], first_breach_ts=row[2], opened_ts=row[3]
            )

    async def observe(
        self, conn: AsyncConnection, evaluation: Evaluation, *,
        baseline_window_s: int, baseline_guard_s: int,
    ) -> Incident | None:
        if not evaluation.evaluated:
            return self._current
        if evaluation.breached:
            await self._on_breach(conn, evaluation, baseline_window_s, baseline_guard_s)
        else:
            await self._on_clean(conn, evaluation)
        return self._current

    async def _on_breach(
        self, conn: AsyncConnection, evaluation: Evaluation,
        baseline_window_s: int, baseline_guard_s: int,
    ) -> None:
        incident = self._current
        if incident is None:
            incident = Incident(
                id=uuid.uuid4(), state="PENDING", first_breach_ts=evaluation.ts,
                opened_ts=None, consecutive_breaches=1,
            )
            await conn.execute(_INSERT, {
                "id": incident.id, "state": "PENDING",
                "first_breach_ts": incident.first_breach_ts,
                "severity": severity_of(evaluation),
            })
            self._current = incident
            log.info("incident %s PENDING at %s", incident.id, evaluation.ts.isoformat())
        else:
            incident.consecutive_breaches += 1
            incident.consecutive_clean = 0
            if incident.state == "RECOVERING":
                # It came back. The same incident, not a new one.
                incident.state = "OPEN"
                await conn.execute(_SET_STATE, {"id": incident.id, "state": "OPEN"})
                log.info("incident %s RECOVERING -> OPEN", incident.id)

        await self._record_symptoms(conn, incident, evaluation)

        if (
            incident.state == "PENDING"
            and incident.consecutive_breaches >= self._breach_persistence
        ):
            import json

            baseline = await baseline_snapshot(
                conn, first_breach_ts=incident.first_breach_ts,
                window_s=baseline_window_s, guard_s=baseline_guard_s,
            )
            incident.state = "OPEN"
            incident.opened_ts = evaluation.ts
            await conn.execute(_OPEN, {
                "id": incident.id, "opened_ts": incident.opened_ts,
                "severity": severity_of(evaluation), "baseline": json.dumps(baseline),
            })
            log.info(
                "incident %s OPEN, baseline n=%s p95=%.0fms threshold=%.0fms",
                incident.id, baseline["n"], baseline["p95_ms"] or 0.0,
                baseline["abnormal_latency_threshold_ms"],
            )

    async def _on_clean(self, conn: AsyncConnection, evaluation: Evaluation) -> None:
        incident = self._current
        if incident is None:
            return

        incident.consecutive_breaches = 0
        incident.consecutive_clean += 1

        if incident.state == "PENDING":
            # It never opened. One bad window, then recovery: noise, and it must
            # not surface as an incident. Closed rather than deleted so the
            # evaluator's history stays auditable.
            await conn.execute(_CLOSE, {"id": incident.id, "closed_ts": evaluation.ts})
            log.info("incident %s discarded: PENDING never reached OPEN", incident.id)
            self._current = None
            return

        if incident.state == "OPEN":
            incident.state = "RECOVERING"
            await conn.execute(_SET_STATE, {"id": incident.id, "state": "RECOVERING"})
            log.info("incident %s OPEN -> RECOVERING", incident.id)
            return

        if (
            incident.state == "RECOVERING"
            and incident.consecutive_clean >= self._recovery_persistence
        ):
            await conn.execute(_CLOSE, {"id": incident.id, "closed_ts": evaluation.ts})
            log.info("incident %s CLOSED", incident.id)
            self._current = None

    async def _record_symptoms(
        self, conn: AsyncConnection, incident: Incident, evaluation: Evaluation
    ) -> None:
        for reading in evaluation.breached_readings:
            await conn.execute(_ADD_SYMPTOM, {
                "incident_id": incident.id, "slo_id": reading.slo_id,
                "breached_ts": evaluation.ts, "observed_value": reading.observed,
            })
