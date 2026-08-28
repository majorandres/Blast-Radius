"""Shared OpenTelemetry helpers for the two emitting processes.

Lives here rather than being duplicated per service for the same reason
`attributes.py` does: both emitters must agree exactly, and Day 2 detection
logic depends on the agreement holding.

The central idea is `blastradius_span`. Every one of the eleven contract spans
(v1.2 §6.2) goes through it, and it records the enclosing *contract* span's id
explicitly in `blastradius.parent_span_id`. The exporter then reads parentage
from that attribute alone and never from the OTel parent.

That indirection is what lets the auto-instrumentation stay switched on. The
httpx and FastAPI instrumentors emit spans of their own -- five of them sit
between `promo.apply` and `promo.handle` -- and Jaeger keeps every one, which is
the honest picture. The detector sees only spans carrying `blastradius.domain`,
re-parented onto the tree the contract describes. Inferring parentage by looking
for a surviving parent would not work, because a parent can land in a different
export batch.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, SpanKind

from blastradius_contracts.attributes import (
    BLOCKING_KEY,
    DOMAIN_KEY,
    PARENT_SPAN_ID_KEY,
)

#: v1.2 §7.1 -- identical settings on both export pipelines.
BATCH_KWARGS: dict[str, int] = {"max_export_batch_size": 200, "schedule_delay_millis": 2000}

#: The enclosing contract span's id, or None at the root of a checkout.
_current_parent: ContextVar[str | None] = ContextVar("blastradius_parent", default=None)


def format_span_id(span: Span) -> str:
    return format(span.get_span_context().span_id, "016x")


def current_parent_id() -> str | None:
    """The id the next contract span will record as its parent.

    Read by the promo client, which sends it across the wire so `promo.handle`
    can be re-parented onto `promo.apply`.
    """
    return _current_parent.get()


@contextmanager
def blastradius_span(
    tracer: trace.Tracer,
    operation: str,
    *,
    domain: str,
    kind: SpanKind = SpanKind.INTERNAL,
    blocking: bool = True,
    attributes: dict[str, Any] | None = None,
    parent_span_id: str | None = None,
    root: bool = False,
) -> Iterator[Span]:
    """Open one contract span.

    `parent_span_id` overrides the contextvar and is used exactly once: on
    `promo.handle`, whose logical parent lives in another process.

    `root=True` detaches from ambient context entirely. A checkout is always the
    start of its own trace, and it must not inherit whatever span happened to be
    active when its task was created. That is not hypothetical: starting the
    traffic generator from inside a request handler -- which is what
    `/internal/resume` does after a reset -- makes `asyncio.create_task` capture
    that request's span, and every checkout then lands on one enormous trace.
    """
    attrs: dict[str, Any] = {DOMAIN_KEY: domain, BLOCKING_KEY: blocking}
    if attributes:
        attrs.update(attributes)

    parent = None if root else (
        parent_span_id if parent_span_id is not None else _current_parent.get()
    )
    if parent:
        attrs[PARENT_SPAN_ID_KEY] = parent

    # An empty Context has no active span, so the SDK starts a new trace.
    start_context = otel_context.Context() if root else None

    with tracer.start_as_current_span(
        operation, kind=kind, attributes=attrs, context=start_context
    ) as span:
        token = _current_parent.set(format_span_id(span))
        try:
            yield span
        finally:
            _current_parent.reset(token)


def configure_tracing(
    service_name: str,
    otlp_endpoint: str,
    extra_exporters: list[SpanExporter] | None = None,
) -> TracerProvider:
    """One TracerProvider, one truthful resource, and one processor per pipeline.

    Jaeger receives genuine OTLP. Anything in `extra_exporters` -- in practice
    the BlastRadius exporter -- gets its own BatchSpanProcessor (v1.2 §7.1).
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"),
            **BATCH_KWARGS,
        )
    )
    for exporter in extra_exporters or []:
        provider.add_span_processor(BatchSpanProcessor(exporter, **BATCH_KWARGS))

    trace.set_tracer_provider(provider)
    return provider
