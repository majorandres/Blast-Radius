"""POST /internal/reset — the detector clears its own state (§22).

Two things happen here and the order matters.

First `last_reset_ts` is recorded, then the telemetry is deleted. A span batch
already in flight over HTTP when the delete runs is older than that timestamp,
so it is fenced on arrival rather than inserted into a freshly emptied table.
That is what turns "no pre-reset span survives" from a timing assumption into an
invariant, and what lets the reset-race test be deterministic rather than
sleep-based.

Each process clears only what its own role owns. This one owns telemetry and
incidents; it cannot delete `"order"`, `ground_truth`, or `scenario_run`, and
does not try. `DELETE` throughout rather than `TRUNCATE`: TRUNCATE needs a
separate privilege and behaves differently under foreign keys, and at demo scale
DELETE is fast enough to keep the permission model simple (FINAL-03).
"""

import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, Request

from app.db import engine
from app.ingest.fence import fence

log = logging.getLogger(__name__)
router = APIRouter()

_SET_FENCE = sa.text("UPDATE ingest_state SET last_reset_ts = :ts WHERE id = 1")

#: `incident_symptom` cascades on `incident`, but it is deleted explicitly first
#: so the operation does not depend on cascade configuration (§3.9).
_DELETES = (
    "DELETE FROM incident_symptom",
    "DELETE FROM incident",
    "DELETE FROM span",
    "DELETE FROM trace",
)


@router.post("/internal/reset")
async def reset(request: Request) -> dict[str, object]:
    reset_ts = datetime.now(UTC)

    async with engine().connect() as conn:
        # Fence first, delete second.
        await conn.execute(_SET_FENCE, {"ts": reset_ts})
        await conn.commit()
        await fence.load()

        deleted = {}
        for statement in _DELETES:
            result = await conn.execute(sa.text(statement))
            deleted[statement.split()[-1]] = result.rowcount
        await conn.commit()

    # The evaluator holds consecutive-breach counters in memory; a reset that
    # left them set would let a single post-reset breach open an incident.
    detection = getattr(request.app.state, "detection", None)
    if detection is not None:
        detection.reset_state()

    log.info("reset: fence=%s deleted=%s", reset_ts.isoformat(), deleted)
    return {"last_reset_ts": reset_ts.isoformat(), "deleted": deleted}
