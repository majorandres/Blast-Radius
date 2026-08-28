"""The ingest fence (v1.2 §7.4).

**Invariant: no span whose `end_ts` precedes `last_reset_ts` is ever ingested.**

Draining and force-flushing on reset narrows the race; this closes it. An export
batch already in flight over HTTP when the delete runs cannot survive, because
its spans are older than the reset timestamp. That turns "no pre-reset span
survives" from a timing assumption into an invariant, and makes the reset-race
test deterministic instead of sleep-based.

Only the ingest-side half exists on Day 1; the reset workflow arrives on Day 4.
"""

from datetime import UTC, datetime

import sqlalchemy as sa

from app.db import engine


class Fence:
    def __init__(self) -> None:
        self._last_reset_ts = datetime.min.replace(tzinfo=UTC)
        self._fenced_total = 0

    @property
    def last_reset_ts(self) -> datetime:
        return self._last_reset_ts

    @property
    def fenced_total(self) -> int:
        return self._fenced_total

    async def load(self) -> datetime:
        """Read the persisted value into memory. Called at startup and after update."""
        async with engine().connect() as conn:
            row = (
                await conn.execute(sa.text("SELECT last_reset_ts FROM ingest_state WHERE id = 1"))
            ).first()
        if row and row[0] is not None:
            # '-infinity' comes back from asyncpg as a *naive* datetime.min.
            # Span end times are always tz-aware, so normalise here rather than
            # at every comparison site.
            value = row[0]
            self._last_reset_ts = value if value.tzinfo else value.replace(tzinfo=UTC)
        return self._last_reset_ts

    def is_fenced(self, end_ts: datetime) -> bool:
        return end_ts < self._last_reset_ts

    def record_fenced(self, count: int) -> None:
        self._fenced_total += count


fence = Fence()
