"""The wire contract between the emitting processes and observability-service.

This is a custom JSON projection of OTel spans, not OTLP. See v1.2 §7.5.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SpanKind = Literal["INTERNAL", "CLIENT", "SERVER"]
SpanStatus = Literal["OK", "ERROR"]


class SpanEnvelope(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None
    emitting_service: str
    attribution_domain: str
    span_kind: SpanKind
    operation: str
    start_unix_nano: int
    end_unix_nano: int
    status: SpanStatus
    blocking: bool = True
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)


class SpanBatch(BaseModel):
    spans: list[SpanEnvelope] = Field(max_length=500)


class DrainResult(BaseModel):
    """What an emitting process reports after being asked to go quiet (§22).

    Reported rather than assumed: if in-flight work is still outstanding when
    the timeout expires, the reset proceeds anyway -- the ingest fence makes
    late spans harmless -- but the caller is told, instead of a silent race
    being papered over.
    """

    generator_stopped: bool
    in_flight_remaining: int
    flush_succeeded: bool
    drained_at: datetime
