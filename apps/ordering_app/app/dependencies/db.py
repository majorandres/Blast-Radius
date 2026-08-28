"""The `order-datastore` domain: a genuinely bounded connection pool.

Both spans here are CLIENT spans attributed to `order-datastore`, not to the
emitting `ordering-app` process (CC-A). The pool is bounded from Day 1 --
`pool_size=10, max_overflow=0, pool_timeout=5` -- because Scenario C on Day 4
saturates it for real. Wait time is measured, never injected.
"""

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import sqlalchemy as sa
from blastradius_contracts.attributes import (
    DB_POOL_WAIT_MS_KEY,
    DOMAIN_ORDER_DATASTORE,
    ERROR_KIND_KEY,
    ERROR_KIND_POOL_TIMEOUT,
    OP_DB_PERSIST_ORDER,
    OP_DB_POOL_ACQUIRE,
)
from blastradius_contracts.otel import blastradius_span
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

tracer = trace.get_tracer("ordering-app")

INSERT_ORDER = sa.text(
    'INSERT INTO "order" (id, trace_id, channel, has_promo, payment_method, status, created_ts)'
    " VALUES (:id, :trace_id, :channel, :has_promo, :payment_method, :status, :created_ts)"
)


def make_engine(url: str, pool_size: int, pool_timeout: int) -> AsyncEngine:
    return create_async_engine(
        url, pool_size=pool_size, max_overflow=0, pool_timeout=pool_timeout, future=True
    )


class PoolExhausted(Exception):
    """The pool timed out. Raised after the span is marked ERROR."""


@asynccontextmanager
async def acquire(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """`db.pool_acquire`.

    The span closes as soon as the connection is in hand, before the caller does
    any work with it -- v1.2 §6.2 lists pool_acquire and persist_order as
    siblings under checkout, not as nested spans.
    """
    with blastradius_span(
        tracer, OP_DB_POOL_ACQUIRE, domain=DOMAIN_ORDER_DATASTORE, kind=SpanKind.CLIENT
    ) as span:
        started = time.perf_counter()
        try:
            conn = await engine.connect()
        except PoolTimeout as exc:
            span.set_attribute(ERROR_KIND_KEY, ERROR_KIND_POOL_TIMEOUT)
            span.set_status(Status(StatusCode.ERROR, "pool acquire timeout"))
            raise PoolExhausted("pool acquire timeout") from exc
        span.set_attribute(DB_POOL_WAIT_MS_KEY, (time.perf_counter() - started) * 1000)

    try:
        yield conn
    finally:
        await conn.close()


async def persist_order(
    conn: AsyncConnection,
    *,
    order_id: uuid.UUID,
    trace_id: str,
    channel: str,
    has_promo: bool,
    payment_method: str,
    status: str,
    created_ts: datetime,
) -> None:
    """`db.persist_order`.

    RC4: when this fails the `"order"` row is never written, but blast radius
    reads `trace`, which is unaffected. Day 4 tests that explicitly.
    """
    with blastradius_span(
        tracer, OP_DB_PERSIST_ORDER, domain=DOMAIN_ORDER_DATASTORE, kind=SpanKind.CLIENT
    ) as span:
        try:
            await conn.execute(
                INSERT_ORDER,
                {
                    "id": order_id,
                    "trace_id": trace_id,
                    "channel": channel,
                    "has_promo": has_promo,
                    "payment_method": payment_method,
                    "status": status,
                    "created_ts": created_ts,
                },
            )
            await conn.commit()
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, "order persist failed"))
            raise RuntimeError("order persist failed") from exc
