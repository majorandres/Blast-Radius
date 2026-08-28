"""Fault scaffold. Day 1: structurally present, every behavior a no-op.

v1.2 §5.3 defines PUT /_faults on this service with payment, db, and traffic
switches. Day 1 accepts and stores them so the shape is fixed; nothing reads
them to alter behavior.
"""

from pydantic import BaseModel, Field


class PaymentFaults(BaseModel):
    failure_prob: float = Field(default=0.0, ge=0.0, le=1.0)
    added_latency_ms: int = Field(default=0, ge=0)
    payment_method: str | None = None


class DbFaults(BaseModel):
    extra_concurrency: int = Field(default=0, ge=0)


class TrafficFaults(BaseModel):
    rate_multiplier: float = Field(default=1.0, gt=0.0)


class OrderingFaults(BaseModel):
    payment: PaymentFaults = PaymentFaults()
    db: DbFaults = DbFaults()
    traffic: TrafficFaults = TrafficFaults()


_state = OrderingFaults()


def get_faults() -> OrderingFaults:
    return _state


def set_faults(faults: OrderingFaults) -> OrderingFaults:
    global _state
    _state = faults
    return _state
