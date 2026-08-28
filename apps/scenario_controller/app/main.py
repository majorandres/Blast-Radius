"""scenario-controller — the injector.

Holds scenario lifecycle, ground truth, and fault dispatch. It reads and writes
only `scenario_run` and `ground_truth`, on a role that cannot read `span`,
`trace`, or `incident` at all.

The detector has no route into this service: no grant, no shared table, no
import, and no call. Isolation runs both ways, and this half of it is enforced
by the same grants as the other (v1.2 §1.3, §3.9).
"""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from blastradius_contracts.profiles import detection_profile, scenario_profile
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.dispatcher import Dispatcher
from app.reveal import IncidentOutsideRunWindow, reveal, session_score
from app.scenarios import IMPLEMENTED, SCENARIOS
from app.state_machine import (
    ScenarioAlreadyActive,
    arm,
    complete,
    current_run,
    latest_run,
    revealable_run,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

state: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    profile = scenario_profile()
    state["engine"] = create_async_engine(settings.database_url_scenario, future=True)
    state["client"] = httpx.AsyncClient()
    state["profile"] = profile
    # The detector's SLO window bounds how long a correct detection may lag the
    # fault (§5.4). Reading a detector *constant* is the safe direction: nothing
    # about this run flows the other way.
    state["slo_window_s"] = detection_profile().slo_window_s
    state["dispatcher"] = Dispatcher(
        state["engine"], state["client"],
        promo_url=settings.promo_provider_url,
        ordering_url=settings.ordering_app_url,
        ramp_s=profile.ramp_s, hold_s=profile.hold_s,
    )
    log.info("controller ready: profile=%s ramp=%ss hold=%ss",
             profile.name, profile.ramp_s, profile.hold_s)
    yield
    await state["dispatcher"].stop()
    await state["client"].aclose()
    await state["engine"].dispose()


app = FastAPI(title="scenario-controller", lifespan=lifespan)


class InjectRequest(BaseModel):
    mode: str = Field(default="blind", pattern="^(blind|known)$")
    scenario: str | None = Field(default=None, pattern="^[ABC]$")
    seed: int | None = None
    profile: str | None = Field(default=None, pattern="^(DEMO|REALISTIC)$")


class ScenarioRun(BaseModel):
    id: str
    state: str
    mode: str
    profile: str
    seed: int
    #: Withheld in blind mode. The frontend holds this response, and the whole
    #: exercise is to diagnose without it.
    scenario: str | None
    started_ts: datetime
    ended_ts: datetime | None = None
    revealed_ts: datetime | None = None


def _to_run(row: dict, *, reveal_scenario: bool) -> ScenarioRun:
    return ScenarioRun(
        id=str(row["id"]),
        state=row["state"],
        mode=row["mode"],
        profile=row["profile"],
        seed=row["seed"],
        scenario=row["scenario"] if reveal_scenario else None,
        started_ts=row["started_ts"],
        ended_ts=row["ended_ts"],
        revealed_ts=row["revealed_ts"],
    )


@app.post("/api/scenarios/inject", response_model=ScenarioRun, status_code=201)
async def inject(request: InjectRequest) -> ScenarioRun:
    import random

    name = request.scenario or "A"
    if name not in SCENARIOS:
        raise HTTPException(400, {"code": "VALIDATION_FAILED", "message": f"unknown scenario {name}"})
    if name not in IMPLEMENTED:
        raise HTTPException(
            400, {"code": "VALIDATION_FAILED", "message": f"scenario {name} is not yet wired"}
        )

    scenario = SCENARIOS[name]
    seed = request.seed if request.seed is not None else random.randint(1, 2**31 - 1)
    profile = request.profile or settings.profile

    async with state["engine"].connect() as conn:
        try:
            # Ground truth is written here, before anything is dispatched.
            run_id = await arm(
                conn,
                scenario=scenario.name,
                injected_domain=scenario.injected_domain,
                fault_type=scenario.fault_type,
                mode=request.mode,
                profile=profile,
                seed=seed,
                started_ts=datetime.now(UTC),
            )
        except ScenarioAlreadyActive as exc:
            raise HTTPException(409, {
                "code": "SCENARIO_ALREADY_ACTIVE",
                "message": f"run {exc.run_id} is {exc.state}",
            }) from exc

        state["dispatcher"].start(run_id, scenario)
        row = await latest_run(conn)

    log.info("armed run %s scenario=%s mode=%s seed=%s", run_id, scenario.name,
             request.mode, seed)
    return _to_run(row, reveal_scenario=request.mode != "blind")


@app.get("/api/scenarios/current", response_model=ScenarioRun | None)
async def get_current() -> ScenarioRun | None:
    """The run the UI is working with: still running, or finished but not yet
    revealed. `current_run` stays stricter and governs the one-at-a-time rule."""
    async with state["engine"].connect() as conn:
        row = await revealable_run(conn)
    if row is None:
        return None
    return _to_run(row, reveal_scenario=row["mode"] != "blind")


@app.post("/api/scenarios/{run_id}/stop", response_model=ScenarioRun)
async def stop(run_id: str) -> ScenarioRun:
    async with state["engine"].connect() as conn:
        row = await current_run(conn)
        if row is None or str(row["id"]) != run_id:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "no such active run"})

        await state["dispatcher"].stop()
        await state["dispatcher"].clear_all()
        if row["state"] not in ("COMPLETE", "REVEALED"):
            from contextlib import suppress

            with suppress(Exception):
                await complete(conn, row["id"], datetime.now(UTC))

        updated = await latest_run(conn)
    return _to_run(updated, reveal_scenario=updated["mode"] != "blind")


class RevealRequest(BaseModel):
    incident_id: str | None = None


@app.post("/api/scenarios/{run_id}/reveal")
async def reveal_run(run_id: str, request: RevealRequest) -> dict:
    import uuid as _uuid

    async with state["engine"].connect() as conn:
        try:
            result = await reveal(
                conn, state["client"],
                run_id=_uuid.UUID(run_id),
                incident_id=request.incident_id,
                observability_url=settings.observability_url,
                recovery_hold_s=state["profile"].recovery_hold_s,
                slo_window_s=state["slo_window_s"],
            )
        except IncidentOutsideRunWindow as exc:
            # Nothing is written and the run is not scored. The user may retry
            # with a different incident.
            raise HTTPException(409, {
                "code": "INCIDENT_OUTSIDE_RUN_WINDOW", "message": exc.message,
            }) from exc
        except ValueError as exc:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": str(exc)}) from exc

    return {
        "scenario_run_id": result.scenario_run_id,
        "detected_domain": result.detected_domain,
        "detected_verdict": result.detected_verdict,
        "injected_domain": result.injected_domain,
        "injected_fault_type": result.injected_fault_type,
        "correct": result.correct,
        "session_correct": result.session_correct,
        "session_total": result.session_total,
    }


@app.get("/api/session/score")
async def get_score() -> dict:
    async with state["engine"].connect() as conn:
        correct, total = await session_score(conn)
    return {"correct": correct, "total": total}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready"}
