"""ordering-app.

Build step 2 (gate zero): enough to prove W3C trace context propagates across
the promo hop. The full §6.2 span tree lands at step 4, the traffic generator
at step 7.
"""

import uuid
from contextlib import asynccontextmanager

import httpx
from blastradius_contracts.attributes import (
    BLOCKING_KEY,
    DOMAIN_KEY,
    DOMAIN_ORDERING_APP,
    OP_CHECKOUT,
    ORDER_CHANNEL_KEY,
    ORDER_HAS_PROMO_KEY,
    ORDER_ID_KEY,
    ORDER_PAYMENT_METHOD_KEY,
)
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import SpanKind

from app.config import settings
from app.dependencies.promo_client import PromoUnavailable, apply_promo
from app.faults import OrderingFaults, get_faults, set_faults
from app.telemetry.setup import configure_tracing

provider = configure_tracing(settings.service_name, settings.otlp_endpoint)
tracer = trace.get_tracer(settings.service_name)
HTTPXClientInstrumentor().instrument(tracer_provider=provider)

state: dict[str, httpx.AsyncClient] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["promo"] = httpx.AsyncClient()
    yield
    await state["promo"].aclose()
    provider.shutdown()


app = FastAPI(title="ordering-app", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)


@app.post("/_debug/checkout")
async def debug_checkout() -> dict[str, object]:
    """Gate-zero trigger: one checkout root span that crosses the promo hop."""
    order_id = str(uuid.uuid4())
    with tracer.start_as_current_span(
        OP_CHECKOUT,
        kind=SpanKind.SERVER,
        attributes={
            DOMAIN_KEY: DOMAIN_ORDERING_APP,
            BLOCKING_KEY: True,
            ORDER_ID_KEY: order_id,
            ORDER_CHANNEL_KEY: "mobile",
            ORDER_HAS_PROMO_KEY: True,
            ORDER_PAYMENT_METHOD_KEY: "card",
        },
    ) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        try:
            promo = await apply_promo(
                state["promo"],
                settings.promo_provider_url,
                settings.promo_client_timeout_ms,
                order_id,
                "mobile",
            )
        except PromoUnavailable:
            promo = None
        return {"order_id": order_id, "trace_id": trace_id, "promo": promo}


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
