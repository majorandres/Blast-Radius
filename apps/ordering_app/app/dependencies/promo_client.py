"""The `promo.apply` span and the real HTTP boundary to promo-provider.

CC-A (v1.2 §6.3): this is a CLIENT span, so its attribution domain is the
*peer* -- `promo-provider` -- never the emitter. That holds whether or not the
peer ever responded, which is what makes attribution stable when the call times
out and no server span exists at all.
"""

from typing import Any

import httpx
from blastradius_contracts.attributes import (
    DOMAIN_PROMO_PROVIDER,
    ERROR_KIND_KEY,
    ERROR_KIND_TIMEOUT,
    ERROR_KIND_UPSTREAM_ERROR,
    HTTP_STATUS_CODE_KEY,
    OP_PROMO_APPLY,
    PARENT_SPAN_ID_HEADER,
)
from blastradius_contracts.otel import blastradius_span, format_span_id
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

tracer = trace.get_tracer("ordering-app")


class PromoUnavailable(Exception):
    """The promo dependency failed. Raised after the span is marked ERROR."""


async def apply_promo(
    client: httpx.AsyncClient, base_url: str, timeout_ms: int, order_id: str, channel: str
) -> dict[str, Any] | None:
    with blastradius_span(
        tracer, OP_PROMO_APPLY, domain=DOMAIN_PROMO_PROVIDER, kind=SpanKind.CLIENT
    ) as span:
        # Carry the logical parent across the auto-instrumented HTTP boundary.
        # Five auto spans sit between this span and promo.handle; the header is
        # what lets the exporter collapse them back to the contract's tree.
        headers = {PARENT_SPAN_ID_HEADER: format_span_id(span)}
        try:
            response = await client.post(
                f"{base_url.rstrip('/')}/promo/apply",
                json={"order_id": order_id, "channel": channel},
                headers=headers,
                timeout=timeout_ms / 1000,
            )
        except httpx.TimeoutException as exc:
            span.set_attribute(ERROR_KIND_KEY, ERROR_KIND_TIMEOUT)
            span.set_status(Status(StatusCode.ERROR, "promo client timeout"))
            raise PromoUnavailable("promo timeout") from exc
        except httpx.HTTPError as exc:
            span.set_attribute(ERROR_KIND_KEY, ERROR_KIND_UPSTREAM_ERROR)
            span.set_status(Status(StatusCode.ERROR, "promo transport error"))
            raise PromoUnavailable("promo transport error") from exc

        span.set_attribute(HTTP_STATUS_CODE_KEY, response.status_code)
        if response.status_code >= 500:
            span.set_attribute(ERROR_KIND_KEY, ERROR_KIND_UPSTREAM_ERROR)
            span.set_status(Status(StatusCode.ERROR, "promo upstream error"))
            raise PromoUnavailable(f"promo returned {response.status_code}")

        return response.json()
