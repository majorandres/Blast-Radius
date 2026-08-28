"""Reveal: compare what the detector concluded against what was injected.

The flow (v1.2 §5.4):

    1. frontend polls observability for incidents and picks one
    2. frontend POSTs it here with the run id
    3. this service GETs that incident from observability's *public* endpoint
    4. it validates the association
    5. it compares against ground truth and records the result

Step 3 uses the same unauthenticated route the frontend polls, so observability
cannot distinguish it from a dashboard refresh and learns nothing about the run.
That is the only reason this direction of call is permitted at all.

**Why validation exists (FINAL-05).** Without it, a user could hand back an
incident from ten minutes ago -- or from a previous run -- and have it scored
against this run's ground truth. The score would be meaningless, and worse, it
would be meaningless in the flattering direction: pick any old incident that
happens to name the right domain and you always "win".
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 5.0


class IncidentOutsideRunWindow(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class RevealResult:
    scenario_run_id: str
    detected_domain: str | None
    detected_verdict: str
    injected_domain: str
    injected_fault_type: str
    correct: bool
    session_correct: int
    session_total: int


_GROUND_TRUTH = sa.text(
    "SELECT d.name AS injected_domain, g.fault_type"
    " FROM ground_truth g JOIN domain d ON d.id = g.injected_domain_id"
    " WHERE g.scenario_run_id = :run_id"
)
_RUN = sa.text(
    "SELECT id, state::text AS state, started_ts, ended_ts"
    " FROM scenario_run WHERE id = :run_id"
)
_INSERT_REVEAL = sa.text(
    """
    INSERT INTO reveal (scenario_run_id, incident_id, incident_first_breach_ts,
                        detected_domain, detected_verdict, correct, revealed_ts)
    VALUES (:run_id, :incident_id, :first_breach_ts, :detected_domain,
            :detected_verdict, :correct, :revealed_ts)
    ON CONFLICT (scenario_run_id) DO UPDATE SET
      incident_id = EXCLUDED.incident_id,
      incident_first_breach_ts = EXCLUDED.incident_first_breach_ts,
      detected_domain = EXCLUDED.detected_domain,
      detected_verdict = EXCLUDED.detected_verdict,
      correct = EXCLUDED.correct,
      revealed_ts = EXCLUDED.revealed_ts
    """
)
_MARK_REVEALED = sa.text(
    "UPDATE scenario_run SET state = 'REVEALED', revealed_ts = :ts WHERE id = :run_id"
)
_SCORE = sa.text(
    "SELECT count(*) FILTER (WHERE correct) AS correct, count(*) AS total FROM reveal"
)


def validate_window(
    *,
    incident_first_breach_ts: datetime,
    run_started_ts: datetime,
    run_ended_ts: datetime | None,
    recovery_hold_s: int,
    slo_window_s: int,
    now: datetime | None = None,
) -> None:
    """v1.2 §5.4.

        run.started_ts <= incident.first_breach_ts
                       <= run_end + recovery_hold_s + slo_window_s

    The lower bound rejects an incident that began before the fault was armed:
    it cannot have been caused by this run. The upper bound allows detection to
    lag the fault by at most one full SLO window plus the recovery hold, which
    is the longest a correct detection can legitimately take, and rejects
    anything later.
    """
    run_end = run_ended_ts or (now or datetime.now(UTC))
    upper = run_end + timedelta(seconds=recovery_hold_s + slo_window_s)

    if incident_first_breach_ts < run_started_ts:
        raise IncidentOutsideRunWindow(
            f"incident began at {incident_first_breach_ts.isoformat()}, "
            f"before the run started at {run_started_ts.isoformat()}"
        )
    if incident_first_breach_ts > upper:
        raise IncidentOutsideRunWindow(
            f"incident began at {incident_first_breach_ts.isoformat()}, "
            f"after the run's detection window closed at {upper.isoformat()}"
        )


def is_correct(detected_verdict: str, detected_domain: str | None, injected_domain: str) -> bool:
    """Only a confident, correct naming counts.

    AMBIGUOUS is an honest answer and still a miss: the exercise is to identify
    the failing domain, not to narrow it to two. NO_DIAGNOSIS and NO_INCIDENT
    are misses for the same reason. Scoring a hedge as a win would make the
    score meaningless in the flattering direction.
    """
    return detected_verdict == "ATTRIBUTED" and detected_domain == injected_domain


async def fetch_incident(
    client: httpx.AsyncClient, observability_url: str, incident_id: str
) -> dict:
    """Read one incident over the public endpoint the frontend also polls."""
    response = await client.get(
        f"{observability_url.rstrip('/')}/api/incidents/{incident_id}",
        timeout=HTTP_TIMEOUT_S,
    )
    if response.status_code == 404:
        raise IncidentOutsideRunWindow(f"no such incident: {incident_id}")
    response.raise_for_status()
    return response.json()


async def session_score(conn: AsyncConnection) -> tuple[int, int]:
    row = (await conn.execute(_SCORE)).mappings().one()
    return int(row["correct"] or 0), int(row["total"] or 0)


async def reveal(
    conn: AsyncConnection,
    client: httpx.AsyncClient,
    *,
    run_id: uuid.UUID,
    incident_id: str | None,
    observability_url: str,
    recovery_hold_s: int,
    slo_window_s: int,
) -> RevealResult:
    run = (await conn.execute(_RUN, {"run_id": run_id})).mappings().first()
    if run is None:
        raise ValueError("no such run")

    truth = (await conn.execute(_GROUND_TRUTH, {"run_id": run_id})).mappings().first()
    if truth is None:
        raise ValueError("run has no ground truth")

    detected_domain: str | None = None
    detected_verdict = "NO_INCIDENT"
    first_breach_ts: datetime | None = None

    if incident_id is not None:
        incident = await fetch_incident(client, observability_url, incident_id)
        first_breach_ts = datetime.fromisoformat(incident["first_breach_ts"])
        validate_window(
            incident_first_breach_ts=first_breach_ts,
            run_started_ts=run["started_ts"],
            run_ended_ts=run["ended_ts"],
            recovery_hold_s=recovery_hold_s,
            slo_window_s=slo_window_s,
        )
        detected_verdict = incident["verdict"] or "NO_DIAGNOSIS"
        detected_domain = incident["attributed_domain"]

    correct = is_correct(detected_verdict, detected_domain, truth["injected_domain"])

    revealed_ts = datetime.now(UTC)
    await conn.execute(_INSERT_REVEAL, {
        "run_id": run_id,
        "incident_id": incident_id,
        "first_breach_ts": first_breach_ts,
        "detected_domain": detected_domain,
        "detected_verdict": detected_verdict,
        "correct": correct,
        "revealed_ts": revealed_ts,
    })
    if run["state"] != "REVEALED":
        await conn.execute(_MARK_REVEALED, {"run_id": run_id, "ts": revealed_ts})
    await conn.commit()

    session_correct, session_total = await session_score(conn)
    log.info("run %s revealed: detected=%s truth=%s correct=%s",
             run_id, detected_domain, truth["injected_domain"], correct)

    return RevealResult(
        scenario_run_id=str(run_id),
        detected_domain=detected_domain,
        detected_verdict=detected_verdict,
        injected_domain=truth["injected_domain"],
        injected_fault_type=truth["fault_type"],
        correct=correct,
        session_correct=session_correct,
        session_total=session_total,
    )
