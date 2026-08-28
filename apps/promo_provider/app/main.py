import asyncio
import random
from contextlib import asynccontextmanager

from blastradius_contracts.attributes import (
    DOMAIN_PROMO_PROVIDER,
    ERROR_KIND_KEY,
    ERROR_KIND_UPSTREAM_ERROR,
    OP_PROMO_HANDLE,
    PARENT_SPAN_ID_HEADER,
)
from blastradius_contracts.exporter import BlastRadiusSpanExporter
from blastradius_contracts.otel import blastradius_span, configure_tracing
from fastapi import FastAPI, Header, HTTPException, Request
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel

from app.config import settings
from app.faults import PromoFaults, get_faults, set_faults

#: Longer than any sane client timeout, so a "timeout" fault reliably produces
#: a client-side abort rather than a slow success.
_TIMEOUT_STALL_S = 30.0
_rng = random.Random(1337)

provider = configure_tracing(
    settings.service_name,
    settings.otlp_endpoint,
    # Dual export (v1.2 ง7.1): genuine OTLP to Jaeger, custom JSON projection
    # to the detector. Two pipelines, one TracerProvider, one truthful resource.
    extra_exporters=[BlastRadiusSpanExporter(settings.observability_ingest_url, settings.service_name)],
)
tracer = trace.get_tracer(settings.service_name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    provider.shutdown()


app = FastAPI(title="promo-provider", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)


class PromoRequest(BaseModel):
    order_id: str
    channel: str


class PromoResponse(BaseModel):
    discount_pct: float
    promo_code: str


@app.post("/promo/apply", response_model=PromoResponse)
async def apply_promo(
    body: PromoRequest,
    request: Request,
    blastradius_parent: str | None = Header(default=None, alias=PARENT_SPAN_ID_HEADER),
) -> PromoResponse:
    """The `promo.handle` span of v1.2 ยง6.2.

    Created manually rather than relying on the FastAPI auto span, so it carries
    `blastradius.domain` and so the exporter can re-parent it onto `promo.apply`
    across the filtered auto spans.
    """
    faults = get_faults()
    with blastradius_span(
        tracer,
        OP_PROMO_HANDLE,
        domain=DOMAIN_PROMO_PROVIDER,
        kind=SpanKind.SERVER,
        parent_span_id=blastradius_parent,
    ) as span:
        # A timeout is modelled as an unbounded stall, not an early return. The
        # client aborts at PROMO_CLIENT_TIMEOUT_MS and emits no server span at
        # all, which is the CC-A path attribution has to survive (v1.2 ง6.3).
        if _rng.random() < faults.timeout_prob:
            await asyncio.sleep(_TIMEOUT_STALL_S)

        await asyncio.sleep((settings.base_latency_ms + faults.added_latency_ms) / 1000)

        if _rng.random() < faults.failure_prob:
            span.set_attribute(ERROR_KIND_KEY, ERROR_KIND_UPSTREAM_ERROR)
            span.set_status(Status(StatusCode.ERROR, "promo provider failure"))
            raise HTTPException(status_code=503, detail="promo unavailable")

        return PromoResponse(discount_pct=10.0, promo_code="SAVE10")


@app.put("/_faults", response_model=PromoFaults)
async def put_faults(faults: PromoFaults) -> PromoFaults:
    return set_faults(faults)


@app.get("/_faults", response_model=PromoFaults)
async def read_faults() -> PromoFaults:
    return get_faults()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready"}
