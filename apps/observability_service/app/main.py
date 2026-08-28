"""observability-service -- the detector.

Day 1 builds ingest only. SLO evaluation, incidents, attribution, and blast
radius arrive on Day 2.

This service makes zero outbound calls. That is what makes the isolation claim
in v1.2 §1.2 checkable rather than merely asserted.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import dispose_engine, init_engine, load_reference_ids
from app.ingest.api import router as ingest_router
from app.ingest.fence import fence

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine(settings.database_url_detector)
    app.state.service_ids, app.state.domain_ids = await load_reference_ids()
    await fence.load()
    log.info("ready: services=%s domains=%s last_reset_ts=%s",
             sorted(app.state.service_ids), sorted(app.state.domain_ids),
             fence.last_reset_ts.isoformat())
    yield
    await dispose_engine()


app = FastAPI(title="observability-service", lifespan=lifespan)
app.include_router(ingest_router)


@app.exception_handler(RequestValidationError)
async def malformed_batch(request: Request, exc: RequestValidationError) -> JSONResponse:
    """A malformed batch is logged and dropped, never a 500 (v1.2 §23)."""
    log.warning("malformed request to %s: %s", request.url.path, exc.errors()[:3])
    return JSONResponse(status_code=400, content={"error": {"code": "VALIDATION_FAILED"}})


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "status": "ok",
        "service": settings.service_name,
        "last_reset_ts": fence.last_reset_ts.isoformat(),
        "fenced_total": fence.fenced_total,
    }


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready"}
