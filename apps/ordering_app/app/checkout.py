"""Checkout: the §6.2 span tree, and nothing else.

This module owns the shape of the trace. It performs no I/O of its own -- each
dependency module owns exactly one span and its attribution domain, so the CC-A
rule is stated once per dependency instead of being scattered here.

Tree (v1.2 §6.2). `loyalty_tier_lookup` is a child of `pricing`, `promo.handle`
is a child of `promo.apply` across the process boundary, and everything else is
a direct child of `checkout`:

    checkout                    SERVER    ordering-app
      validate_order            INTERNAL  ordering-app
      pricing                   INTERNAL  ordering-app
        loyalty_tier_lookup     INTERNAL  ordering-app
      db.pool_acquire           CLIENT    order-datastore
      promo.apply               CLIENT    promo-provider    (has_promo only)
      payment.authorize         CLIENT    payment-gateway
      db.persist_order          CLIENT    order-datastore
      analytics.publish         INTERNAL  ordering-app      blocking=false
      confirmation              INTERNAL  ordering-app
"""

import asyncio
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from blastradius_contracts.attributes import (
    DOMAIN_ORDERING_APP,
    OP_ANALYTICS_PUBLISH,
    OP_CHECKOUT,
    OP_CONFIRMATION,
    OP_LOYALTY_TIER_LOOKUP,
    OP_PRICING,
    OP_VALIDATE_ORDER,
    ORDER_CHANNEL_KEY,
    ORDER_HAS_PROMO_KEY,
    ORDER_ID_KEY,
    ORDER_PAYMENT_METHOD_KEY,
)
from blastradius_contracts.otel import blastradius_span
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy.ext.asyncio import AsyncEngine

from app.dependencies import db, herrings, payment
from app.dependencies.promo_client import PromoUnavailable, apply_promo

tracer = trace.get_tracer("ordering-app")

# Healthy baselines. A ~400ms p95 is the v1.2 §11 target, and these sum to it.
VALIDATE_MS = (4, 12)
PRICING_MS = (10, 22)
ANALYTICS_MS = (3, 8)
CONFIRMATION_MS = (2, 6)


@dataclass(frozen=True)
class Order:
    id: uuid.UUID
    channel: str
    has_promo: bool
    payment_method: str


@dataclass(frozen=True)
class CheckoutResult:
    order_id: uuid.UUID
    trace_id: str
    status: str


async def _sleep(rng: random.Random, bounds: tuple[int, int]) -> None:
    await asyncio.sleep(rng.uniform(*bounds) / 1000)


async def run_checkout(
    order: Order,
    *,
    rng: random.Random,
    engine: AsyncEngine,
    promo_client: httpx.AsyncClient,
    promo_base_url: str,
    promo_timeout_ms: int,
) -> CheckoutResult:
    with blastradius_span(
        tracer,
        OP_CHECKOUT,
        domain=DOMAIN_ORDERING_APP,
        kind=SpanKind.SERVER,
        attributes={
            ORDER_ID_KEY: str(order.id),
            ORDER_CHANNEL_KEY: order.channel,
            ORDER_HAS_PROMO_KEY: order.has_promo,
            ORDER_PAYMENT_METHOD_KEY: order.payment_method,
        },
    ) as root:
        trace_id = format(root.get_span_context().trace_id, "032x")
        status = "CONFIRMED"
        try:
            with blastradius_span(
                tracer, OP_VALIDATE_ORDER, domain=DOMAIN_ORDERING_APP
            ):
                await _sleep(rng, VALIDATE_MS)

            with blastradius_span(tracer, OP_PRICING, domain=DOMAIN_ORDERING_APP):
                await _sleep(rng, PRICING_MS)
                # Red herring 1. A shared semaphore takes this from ~8ms to
                # ~45ms under load: the largest relative rise in the system,
                # and about one percent of a multi-second trace.
                with blastradius_span(
                    tracer, OP_LOYALTY_TIER_LOOKUP, domain=DOMAIN_ORDERING_APP
                ):
                    await herrings.loyalty_tier_lookup(rng)

            async with db.acquire(engine) as conn:
                if order.has_promo:
                    await apply_promo(
                        promo_client,
                        promo_base_url,
                        promo_timeout_ms,
                        str(order.id),
                        order.channel,
                    )

                await payment.authorize(rng, order.payment_method)

                await db.persist_order(
                    conn,
                    order_id=order.id,
                    trace_id=trace_id,
                    channel=order.channel,
                    has_promo=order.has_promo,
                    payment_method=order.payment_method,
                    status=status,
                    created_ts=datetime.now(UTC),
                )
        except (PromoUnavailable, payment.PaymentDeclined, db.PoolExhausted, RuntimeError):
            status = "FAILED"
            root.set_status(Status(StatusCode.ERROR, "checkout failed"))

        # Red herring 2. Non-blocking, so it is excluded from the §12.3 error
        # walk, and it fails independently of the checkout outcome -- which is
        # why "the deepest ERROR span anywhere" is the wrong algorithm. Note it
        # runs on the success path too, and never changes `status`.
        with blastradius_span(
            tracer, OP_ANALYTICS_PUBLISH, domain=DOMAIN_ORDERING_APP, blocking=False
        ) as analytics:
            await _sleep(rng, ANALYTICS_MS)
            if herrings.analytics_fails(rng):
                analytics.set_status(Status(StatusCode.ERROR, "analytics publish failed"))

        if status == "CONFIRMED":
            with blastradius_span(tracer, OP_CONFIRMATION, domain=DOMAIN_ORDERING_APP):
                await _sleep(rng, CONFIRMATION_MS)

        return CheckoutResult(order_id=order.id, trace_id=trace_id, status=status)
