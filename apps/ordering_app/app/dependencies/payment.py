"""The `payment-gateway` domain.

A logical dependency, not a process: there is no payment service to run and
Jaeger must not show one. The span is CLIENT and attributed to the peer domain
(CC-A), which is what makes it a legal attribution target even though nothing
downstream emits anything.
"""

import asyncio
import random

from blastradius_contracts.attributes import (
    DOMAIN_PAYMENT_GATEWAY,
    ERROR_KIND_KEY,
    ERROR_KIND_UPSTREAM_ERROR,
    OP_PAYMENT_AUTHORIZE,
    PAYMENT_METHOD_KEY,
)
from blastradius_contracts.otel import blastradius_span
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

tracer = trace.get_tracer("ordering-app")

#: Healthy baseline.
BASE_LATENCY_MS = (70, 140)


class PaymentDeclined(Exception):
    """Authorization failed. Raised after the span is marked ERROR."""


async def authorize(rng: random.Random, payment_method: str, faults=None) -> None:
    """Authorize a payment against the `payment-gateway` domain.

    Scenario B degrades one payment method only. That partiality is the whole
    point: wallets exist across every channel, so every channel's failure rate
    rises while no channel *explains* anything. Payment method does.
    """
    targeted = (
        faults is not None
        and faults.failure_prob > 0
        and (faults.payment_method is None or faults.payment_method == payment_method)
    )

    with blastradius_span(
        tracer,
        OP_PAYMENT_AUTHORIZE,
        domain=DOMAIN_PAYMENT_GATEWAY,
        kind=SpanKind.CLIENT,
        attributes={PAYMENT_METHOD_KEY: payment_method},
    ) as span:
        latency = rng.uniform(*BASE_LATENCY_MS)
        if targeted:
            latency += faults.added_latency_ms
        await asyncio.sleep(latency / 1000)

        if targeted and rng.random() < faults.failure_prob:
            span.set_attribute(ERROR_KIND_KEY, ERROR_KIND_UPSTREAM_ERROR)
            span.set_status(Status(StatusCode.ERROR, "payment declined"))
            raise PaymentDeclined(f"payment declined for {payment_method}")
