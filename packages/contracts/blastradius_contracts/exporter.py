"""BlastRadiusSpanExporter -- the detector's half of the dual export.

This is not an OTLP receiver client. It is a custom JSON projection of spans
emitted by a real SDK, and `DECISIONS.md` must say so plainly (v1.2 §7.5).
Jaeger receives genuine OTLP on the other pipeline.

Three rules, in order:

1. Drop any span without `blastradius.domain`. That removes every
   auto-instrumentation span from the detector's view while Jaeger keeps them.
2. Take the parent from `blastradius.parent_span_id`, never from the OTel
   parent, because the OTel parent is frequently an auto span that was dropped.
3. Map ReadableSpan -> SpanEnvelope.

Export runs on the BatchSpanProcessor's worker thread, so the HTTP client is
synchronous. Failure is logged and reported, never raised into the application.
"""

import json
import logging
import time
from collections.abc import Sequence
from typing import Any

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind as OtelSpanKind
from opentelemetry.trace import StatusCode

from blastradius_contracts.attributes import (
    BLOCKING_KEY,
    DOMAIN_KEY,
    PARENT_SPAN_ID_KEY,
)
from blastradius_contracts.telemetry import SpanBatch, SpanEnvelope

log = logging.getLogger(__name__)

RETRY_DELAYS_S = (0.2, 0.4, 0.8)
DEFAULT_TIMEOUT_S = 5.0

#: Promoted to dedicated columns at ingest, so they do not belong in `attributes`.
_CONTROL_KEYS = frozenset({DOMAIN_KEY, BLOCKING_KEY, PARENT_SPAN_ID_KEY})

_KIND_NAMES = {
    OtelSpanKind.INTERNAL: "INTERNAL",
    OtelSpanKind.CLIENT: "CLIENT",
    OtelSpanKind.SERVER: "SERVER",
}


def _scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return None if value is None else str(value)


def to_envelope(span: ReadableSpan, emitting_service: str) -> SpanEnvelope | None:
    """Project one ReadableSpan, or None if it is not a contract span."""
    raw = dict(span.attributes or {})
    domain = raw.get(DOMAIN_KEY)
    if not isinstance(domain, str):
        return None

    kind = _KIND_NAMES.get(span.kind)
    if kind is None:
        log.warning("dropping contract span with unsupported kind: %s", span.kind)
        return None

    context = span.get_span_context()
    parent = raw.get(PARENT_SPAN_ID_KEY)
    attributes = {k: v for k, v in ((k, _scalar(v)) for k, v in raw.items()
                                    if k not in _CONTROL_KEYS) if v is not None}

    return SpanEnvelope(
        trace_id=format(context.trace_id, "032x"),
        span_id=format(context.span_id, "016x"),
        parent_span_id=parent if isinstance(parent, str) else None,
        emitting_service=emitting_service,
        attribution_domain=domain,
        span_kind=kind,
        operation=span.name,
        start_unix_nano=span.start_time or 0,
        end_unix_nano=span.end_time or 0,
        status="ERROR" if span.status.status_code is StatusCode.ERROR else "OK",
        blocking=bool(raw.get(BLOCKING_KEY, True)),
        attributes=attributes,
    )


class BlastRadiusSpanExporter(SpanExporter):
    def __init__(
        self,
        ingest_url: str,
        emitting_service: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = ingest_url
        self._service = emitting_service
        self._timeout = timeout_s
        self._client = client or httpx.Client(timeout=timeout_s)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        envelopes = [e for e in (to_envelope(s, self._service) for s in spans) if e]
        if not envelopes:
            return SpanExportResult.SUCCESS

        payload = SpanBatch(spans=envelopes).model_dump(mode="json")
        for attempt, delay in enumerate((*RETRY_DELAYS_S, None)):
            try:
                response = self._client.post(self._url, json=payload, timeout=self._timeout)
                if response.status_code < 400:
                    return SpanExportResult.SUCCESS
                # A 4xx is our bug, not a transient fault; retrying cannot help.
                if response.status_code < 500:
                    log.error("span export rejected %s: %s", response.status_code,
                              response.text[:500])
                    return SpanExportResult.FAILURE
                log.warning("span export attempt %s got %s", attempt + 1, response.status_code)
            except httpx.HTTPError as exc:
                log.warning("span export attempt %s failed: %s", attempt + 1, exc)
            if delay is not None:
                time.sleep(delay)

        log.error("span export failed after %s attempts, dropping %s spans",
                  len(RETRY_DELAYS_S) + 1, len(envelopes))
        return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        self._client.close()
