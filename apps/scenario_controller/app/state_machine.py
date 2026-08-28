"""Scenario lifecycle (v1.2 §9).

    IDLE -> ARMED -> INJECTING -> ACTIVE -> RECOVERING -> COMPLETE -> REVEALED

Two rules carry the weight.

**Ground truth is written in the IDLE -> ARMED transaction, before any fault is
dispatched.** If the controller crashed between dispatching a fault and
recording what it was, the system would be degraded with no record of why — and
worse, a subsequent reveal would score against nothing. Recording first means
the only possible inconsistency is a scenario that was armed but never injected,
which is harmless and visible.

**One non-terminal run at a time.** A second inject returns 409. Overlapping
faults would make ground truth ambiguous, and there would be no honest answer to
give at reveal.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

STATES = (
    "IDLE", "ARMED", "INJECTING", "ACTIVE", "RECOVERING", "COMPLETE", "REVEALED",
)

TERMINAL = frozenset({"COMPLETE", "REVEALED"})
ACTIVE_STATES = tuple(s for s in STATES if s not in TERMINAL and s != "IDLE")

LEGAL: dict[str, frozenset[str]] = {
    "IDLE": frozenset({"ARMED"}),
    "ARMED": frozenset({"INJECTING", "COMPLETE"}),
    "INJECTING": frozenset({"ACTIVE", "RECOVERING", "COMPLETE"}),
    "ACTIVE": frozenset({"RECOVERING", "COMPLETE"}),
    "RECOVERING": frozenset({"COMPLETE"}),
    "COMPLETE": frozenset({"REVEALED"}),
    "REVEALED": frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"illegal transition {current} -> {target}")
        self.current = current
        self.target = target


class ScenarioAlreadyActive(Exception):
    def __init__(self, run_id: uuid.UUID, state: str) -> None:
        super().__init__(f"scenario {run_id} is {state}")
        self.run_id = run_id
        self.state = state


def check_transition(current: str, target: str) -> None:
    if target not in LEGAL.get(current, frozenset()):
        raise IllegalTransition(current, target)


_CURRENT = sa.text(
    "SELECT id, state::text AS state, mode, profile, seed, scenario,"
    "       started_ts, ended_ts, revealed_ts"
    " FROM scenario_run WHERE state = ANY(:states)"
    " ORDER BY started_ts DESC LIMIT 1"
)
#: The most recent run that has not been revealed yet. A COMPLETE run is still
#: revealable -- revealing is what you do *after* the fault has run its course --
#: so this deliberately differs from `current_run`, which enforces one active
#: run at a time and must not count a finished one.
_REVEALABLE = sa.text(
    "SELECT id, state::text AS state, mode, profile, seed, scenario,"
    "       started_ts, ended_ts, revealed_ts"
    " FROM scenario_run WHERE state <> 'REVEALED'"
    " ORDER BY started_ts DESC LIMIT 1"
)
_LATEST = sa.text(
    "SELECT id, state::text AS state, mode, profile, seed, scenario,"
    "       started_ts, ended_ts, revealed_ts"
    " FROM scenario_run ORDER BY started_ts DESC LIMIT 1"
)
_INSERT_RUN = sa.text(
    "INSERT INTO scenario_run (id, state, mode, profile, seed, scenario, started_ts)"
    " VALUES (:id, CAST(:state AS scenario_state), :mode, :profile, :seed,"
    "         :scenario, :started_ts)"
)
_INSERT_GROUND_TRUTH = sa.text(
    "INSERT INTO ground_truth (scenario_run_id, injected_domain_id, fault_type, started_ts)"
    " SELECT :run_id, d.id, :fault_type, :started_ts FROM domain d WHERE d.name = :domain"
)
_SET_STATE = sa.text(
    "UPDATE scenario_run SET state = CAST(:state AS scenario_state) WHERE id = :id"
)
_END_RUN = sa.text(
    "UPDATE scenario_run SET state = CAST(:state AS scenario_state), ended_ts = :ended_ts"
    " WHERE id = :id"
)
_END_GROUND_TRUTH = sa.text(
    "UPDATE ground_truth SET ended_ts = :ended_ts WHERE scenario_run_id = :run_id"
)


async def current_run(conn: AsyncConnection) -> dict | None:
    row = (await conn.execute(_CURRENT, {"states": list(ACTIVE_STATES)})).mappings().first()
    return dict(row) if row else None


async def revealable_run(conn: AsyncConnection) -> dict | None:
    row = (await conn.execute(_REVEALABLE)).mappings().first()
    return dict(row) if row else None


async def latest_run(conn: AsyncConnection) -> dict | None:
    row = (await conn.execute(_LATEST)).mappings().first()
    return dict(row) if row else None


async def arm(
    conn: AsyncConnection,
    *,
    scenario: str,
    injected_domain: str,
    fault_type: str,
    mode: str,
    profile: str,
    seed: int,
    started_ts: datetime,
) -> uuid.UUID:
    """IDLE -> ARMED, with ground truth written in the same transaction.

    Nothing has been dispatched when this returns. The system is still healthy
    and the answer is already recorded.
    """
    existing = await current_run(conn)
    if existing is not None:
        raise ScenarioAlreadyActive(existing["id"], existing["state"])

    run_id = uuid.uuid4()
    await conn.execute(_INSERT_RUN, {
        "id": run_id, "state": "ARMED", "mode": mode, "profile": profile,
        "seed": seed, "scenario": scenario, "started_ts": started_ts,
    })
    result = await conn.execute(_INSERT_GROUND_TRUTH, {
        "run_id": run_id, "fault_type": fault_type,
        "started_ts": started_ts, "domain": injected_domain,
    })
    if result.rowcount != 1:
        raise ValueError(f"unknown injected domain: {injected_domain!r}")

    await conn.commit()
    return run_id


async def transition(conn: AsyncConnection, run_id: uuid.UUID, target: str) -> None:
    row = (await conn.execute(
        sa.text("SELECT state::text FROM scenario_run WHERE id = :id"), {"id": run_id}
    )).scalar_one()
    check_transition(row, target)
    await conn.execute(_SET_STATE, {"id": run_id, "state": target})
    await conn.commit()


async def complete(conn: AsyncConnection, run_id: uuid.UUID, ended_ts: datetime) -> None:
    row = (await conn.execute(
        sa.text("SELECT state::text FROM scenario_run WHERE id = :id"), {"id": run_id}
    )).scalar_one()
    check_transition(row, "COMPLETE")
    await conn.execute(_END_RUN, {"id": run_id, "state": "COMPLETE", "ended_ts": ended_ts})
    await conn.execute(_END_GROUND_TRUTH, {"run_id": run_id, "ended_ts": ended_ts})
    await conn.commit()
