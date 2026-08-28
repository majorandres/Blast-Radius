"""Database access for the detector role.

This service makes zero outbound HTTP calls and holds exactly one grant set:
span, trace, ingest_state, and read-only reference data. It cannot read
ground_truth, scenario_run, or "order" -- enforced by Postgres, tested on the
real role (v1.2 §1.2, §3.9).
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None


def engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("engine not initialised")
    return _engine


def init_engine(url: str) -> AsyncEngine:
    global _engine
    _engine = create_async_engine(url, pool_size=10, max_overflow=5, future=True)
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def load_reference_ids() -> tuple[dict[str, int], dict[str, int]]:
    """Name -> id maps for service and domain.

    `service` and `domain` share names where a process is also a domain. They
    are separate tables and are joined by explicit id, never by name (v1.2 §3.2),
    which is why these are two maps and not one.
    """
    async with engine().connect() as conn:
        services = (await conn.execute(sa.text("SELECT name, id FROM service"))).all()
        domains = (await conn.execute(sa.text("SELECT name, id FROM domain"))).all()
    return {r[0]: r[1] for r in services}, {r[0]: r[1] for r in domains}
