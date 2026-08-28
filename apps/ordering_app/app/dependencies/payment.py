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
    OP_PAYMENT_AUTHORIZE,
    PAYMENT_METHOD_KEY,
)
from blastradius_contracts.otel import blastradius_span
from opentelemetry import trace
from opentelemetry.trace import SpanKind

tracer = trace.get_tracer("ordering-app")

#: Healthy baseline. Faults are Day 2; nothing here injects failure.
BASE_LATENCY_MS = (70, 140)


class PaymentDeclined(Exception):
    """Authorization failed. Raised after the span is marked ERROR."""


async def authorize(rng: random.Random, payment_method: str) -> None:
    with blastradius_span(
        tracer,
        OP_PAYMENT_AUTHORIZE,
        domain=DOMAIN_PAYMENT_GATEWAY,
        kind=SpanKind.CLIENT,
        attributes={PAYMENT_METHOD_KEY: payment_method},
    ):
        await asyncio.sleep(rng.uniform(*BASE_LATENCY_MS) / 1000)
