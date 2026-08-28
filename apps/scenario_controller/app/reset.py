"""POST /api/reset — put the whole system back to a clean baseline (§22).

The order is the design. Faults are cleared and generation stopped *before*
anything is deleted, so no in-flight checkout produces a span that lands in a
freshly emptied table. The reset timestamp is taken before the delete, so a
batch already crossing the wire is rejected on arrival rather than inserted.

Each process clears only what its own role owns, and the deletion order within
each respects that role's foreign keys (§3.9). No step needs a privilege its
service does not already hold, which is why the whole sequence can run without
weakening the isolation the project is built on.

A drain that times out is a warning, not a failure: the fence makes late spans
harmless. Any other failure stops the sequence and leaves the system quiet
rather than half-reset -- a half-reset system looks healthy and is not.

Developer affordance. Not exposed in the demo UI.
"""

import logging
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 30.0

#: reveal -> ground_truth -> scenario_run: children before parents.
_SCENARIO_DELETES = (
    "DELETE FROM reveal",
    "DELETE FROM ground_truth",
    "DELETE FROM scenario_run",
)


class ResetFailed(Exception):
    def __init__(self, step: str, cause: str) -> None:
        super().__init__(f"reset failed at {step}: {cause}")
        self.step = step
        self.cause = cause


async def reset_all(
    engine: AsyncEngine,
    client: httpx.AsyncClient,
    *,
    ordering_url: str,
    promo_url: str,
    observability_url: str,
) -> dict:
    ordering = ordering_url.rstrip("/")
    promo = promo_url.rstrip("/")
    observability = observability_url.rstrip("/")
    warnings: list[str] = []
    report: dict[str, object] = {}

    async def call(step: str, method: str, url: str, json: dict | None = None) -> dict:
        try:
            response = await client.request(method, url, json=json, timeout=HTTP_TIMEOUT_S)
            response.raise_for_status()
            return response.json() if response.content else {}
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise ResetFailed(step, str(exc)) from exc

    # 1-2. Clear the fault switches first. Nothing new should be degraded from
    # here on, so the traffic that drains is healthy traffic.
    await call("clear ordering faults", "PUT", f"{ordering}/_faults", {})
    await call("clear promo faults", "PUT", f"{promo}/_faults", {})

    # 3-4. Stop generating and let what is in flight finish.
    ordering_drain = await call("drain ordering", "POST", f"{ordering}/internal/drain")
    promo_drain = await call("drain promo", "POST", f"{promo}/internal/drain")
    report["drain"] = {"ordering": ordering_drain, "promo": promo_drain}

    for name, result in (("ordering-app", ordering_drain), ("promo-provider", promo_drain)):
        if result.get("in_flight_remaining"):
            warnings.append(
                f"DRAIN_TIMEOUT: {name} still had {result['in_flight_remaining']} in flight"
            )
        if not result.get("flush_succeeded", True):
            warnings.append(f"DRAIN_TIMEOUT: {name} force_flush did not complete")

    # 5-6. The detector records the fence before deleting, so anything still
    # crossing the wire is rejected on arrival.
    report["observability"] = await call(
        "reset observability", "POST", f"{observability}/internal/reset"
    )

    # 7. The app clears its own order state.
    report["ordering"] = await call("reset ordering", "POST", f"{ordering}/internal/reset")

    # 8. And this service clears its own, on its own role.
    deleted: dict[str, int] = {}
    async with engine.connect() as conn:
        for statement in _SCENARIO_DELETES:
            result = await conn.execute(sa.text(statement))
            deleted[statement.split()[-1]] = result.rowcount
        await conn.commit()
    report["scenario"] = {"deleted": deleted}

    # 9. Baseline traffic returns only once everything is empty.
    await call("resume ordering", "POST", f"{ordering}/internal/resume")

    report["warnings"] = warnings
    report["reset_at"] = datetime.now(UTC).isoformat()
    log.info("reset complete: %s warnings", len(warnings))
    return report
