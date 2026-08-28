"""ordering-app.

Build step 4: the full ยง6.2 span tree behind a debug trigger. The traffic
generator (step 7) replaces the trigger as the production entry path.
"""

import random
import uuid
from contextlib import asynccontextmanager

import httpx
from blastradius_contracts.exporter import BlastRadiusSpanExporter
from blastradius_contracts.otel import configure_tracing
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from app.checkout import Order, run_checkout
from app.config import settings
from app.dependencies import db
from app.faults import OrderingFaults, get_faults, set_faults
from app.traffic.generator import TrafficGenerator

provider = configure_tracing(
    settings.service_name,
    settings.otlp_endpoint,
    # Dual export (v1.2 ง7.1): genuine OTLP to Jaeger, custom JSON projection
    # to the detector. Two pipelines, one TracerProvider, one truthful resource.
    extra_exporters=[BlastRadiusSpanExporter(settings.observability_ingest_url, settings.service_name)],
)
HTTPXClientInstrumentor().instrument(tracer_provider=provider)

state: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["promo"] = httpx.AsyncClient()
    state["engine"] = db.make_engine(
        settings.database_url_app, settings.db_pool_size, settings.db_pool_timeout
    )
    state["rng"] = random.Random(settings.traffic_seed)

    if settings.traffic_enabled:
        generator = TrafficGenerator(
            engine=state["engine"],
            promo_client=state["promo"],
            promo_base_url=settings.promo_provider_url,
            promo_timeout_ms=settings.promo_client_timeout_ms,
            rate_per_min=settings.traffic_base_rate_per_min,
            max_concurrency=settings.traffic_max_concurrency,
            seed=settings.traffic_seed,
        )
        generator.start()
        state["generator"] = generator

    yield

    if "generator" in state:
        await state["generator"].stop()
    await state["promo"].aclose()
    await state["engine"].dispose()
    provider.shutdown()


app = FastAPI(title="ordering-app", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)


@app.post("/_debug/checkout")
async def debug_checkout(
    channel: str = "mobile", has_promo: bool = True, payment_method: str = "card"
) -> dict[str, str]:
    order = Order(
        id=uuid.uuid4(),
        channel=channel,
        has_promo=has_promo,
        payment_method=payment_method,
    )
    result = await run_checkout(
        order,
        rng=state["rng"],
        engine=state["engine"],
        promo_client=state["promo"],
        promo_base_url=settings.promo_provider_url,
        promo_timeout_ms=settings.promo_client_timeout_ms,
    )
    return {
        "order_id": str(result.order_id),
        "trace_id": result.trace_id,
        "status": result.status,
    }


@app.put("/_faults", response_model=OrderingFaults)
async def put_faults(faults: OrderingFaults) -> OrderingFaults:
    return set_faults(faults)


@app.get("/_faults", response_model=OrderingFaults)
async def read_faults() -> OrderingFaults:
    return get_faults()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready"}
