"""Await in-flight promo calls, then flush (§22).

promo-provider generates no traffic of its own, so there is nothing to stop --
only requests to finish. It tracks its own in-flight count because it has no
semaphore to derive one from.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime

from blastradius_contracts.telemetry import DrainResult
from opentelemetry.sdk.trace import TracerProvider

log = logging.getLogger(__name__)


class InFlight:
    """A plain counter around the request handler."""

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> "InFlight":
        self.count += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self.count -= 1


in_flight = InFlight()


async def drain(
    provider: TracerProvider, *, drain_timeout_s: int, flush_timeout_s: int
) -> DrainResult:
    deadline = time.monotonic() + drain_timeout_s
    while in_flight.count > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    if in_flight.count:
        log.warning("drain timed out with %s promo calls still in flight", in_flight.count)

    flushed = provider.force_flush(flush_timeout_s * 1000)

    return DrainResult(
        generator_stopped=True,   # nothing to stop; vacuously true
        in_flight_remaining=in_flight.count,
        flush_succeeded=bool(flushed),
        drained_at=datetime.now(UTC),
    )
