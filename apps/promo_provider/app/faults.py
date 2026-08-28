"""Fault scaffold. Day 1: structurally present, every behavior a no-op.

v1.2 §5.3 defines PUT /_faults on this service. Day 1 accepts and stores the
switches so the shape is fixed, but nothing reads them to alter behavior.
"""

from pydantic import BaseModel, Field


class PromoFaults(BaseModel):
    added_latency_ms: int = Field(default=0, ge=0)
    timeout_prob: float = Field(default=0.0, ge=0.0, le=1.0)
    failure_prob: float = Field(default=0.0, ge=0.0, le=1.0)


_state = PromoFaults()


def get_faults() -> PromoFaults:
    return _state


def set_faults(faults: PromoFaults) -> PromoFaults:
    global _state
    _state = faults
    return _state
