"""Day 1 test fixtures.

Real PostgreSQL throughout. Nothing here mocks the database: grants and SQL
semantics are what these tests exist to check, and a mock would assert only that
the mock behaves like the mock.

Tests never truncate. The traffic generator is running against the same database
while they execute, so every test invents its own trace ids and asserts only on
rows it created. That keeps the suite honest against a live system rather than
requiring a quiesced one.
"""

import os
import secrets
import time
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

os.environ.setdefault(
    "DATABASE_URL_DETECTOR",
    "postgresql+asyncpg://blastradius_detector:detector@postgres:5432/blastradius",
)


@pytest_asyncio.fixture
async def client():
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://obs") as c:
            c.app = app
            yield c


@pytest_asyncio.fixture
async def db(client):
    from app.db import engine

    async with engine().connect() as conn:
        yield conn


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def envelope(
    trace_id: str,
    *,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    operation: str = "checkout",
    emitting_service: str = "ordering-app",
    attribution_domain: str = "ordering-app",
    span_kind: str = "SERVER",
    status: str = "OK",
    blocking: bool = True,
    duration_ms: int = 100,
    end_ts: datetime | None = None,
    attributes: dict | None = None,
) -> dict:
    end = end_ts or datetime.now(UTC)
    end_nano = int(end.timestamp() * 1e9)
    return {
        "trace_id": trace_id,
        "span_id": span_id or new_span_id(),
        "parent_span_id": parent_span_id,
        "emitting_service": emitting_service,
        "attribution_domain": attribution_domain,
        "span_kind": span_kind,
        "operation": operation,
        "start_unix_nano": end_nano - duration_ms * 1_000_000,
        "end_unix_nano": end_nano,
        "status": status,
        "blocking": blocking,
        "attributes": attributes or {},
    }


def root_attributes(order_id: str, channel: str, has_promo: bool, payment_method: str) -> dict:
    return {
        "order.id": order_id,
        "order.channel": channel,
        "order.has_promo": has_promo,
        "order.payment_method": payment_method,
    }


async def trace_head(db, trace_id: str):
    return (
        await db.execute(
            sa.text("SELECT * FROM trace WHERE trace_id = :t"), {"t": trace_id}
        )
    ).mappings().first()


async def span_rows(db, trace_id: str):
    return (
        await db.execute(
            sa.text("SELECT * FROM span WHERE trace_id = :t ORDER BY start_ts"),
            {"t": trace_id},
        )
    ).mappings().all()
