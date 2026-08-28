"""The wire contract between the emitting processes and observability-service.

This is a custom JSON projection of OTel spans, not OTLP. See v1.2 §7.5.
"""

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
