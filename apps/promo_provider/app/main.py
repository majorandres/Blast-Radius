import asyncio
from contextlib import asynccontextmanager

from blastradius_contracts.attributes import (
    DOMAIN_PROMO_PROVIDER,
    OP_PROMO_HANDLE,
    PARENT_SPAN_ID_HEADER,
)
from blastradius_contracts.exporter import BlastRadiusSpanExporter
from blastradius_contracts.otel import blastradius_span, configure_tracing
from fastapi import FastAPI, Header, Request
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from app.config import settings
from app.faults import PromoFaults, get_faults, set_faults

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
    with blastradius_span(
        tracer,
        OP_PROMO_HANDLE,
        domain=DOMAIN_PROMO_PROVIDER,
        kind=SpanKind.SERVER,
        parent_span_id=blastradius_parent,
    ):
        await asyncio.sleep(settings.base_latency_ms / 1000)
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
