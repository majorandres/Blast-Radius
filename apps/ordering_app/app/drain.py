"""Stop generating, wait for what is in flight, flush what was recorded (§22).

Drain narrows the reset race; it does not close it. Closing it is the ingest
fence's job. The two are complementary and the contract is explicit that both
exist: drain means almost nothing is still moving, and the fence means anything
that still is cannot land.

Returning `in_flight_remaining > 0` is a real answer, not a failure. The reset
continues and the caller is warned.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime

from blastradius_contracts.telemetry import DrainResult
from opentelemetry.sdk.trace import TracerProvider

log = logging.getLogger(__name__)


async def drain(
    generator, provider: TracerProvider, *, drain_timeout_s: int, flush_timeout_s: int
) -> DrainResult:
    generator_stopped = False
    if generator is not None:
        await generator.stop()
        generator_stopped = True

    # In-flight count comes from the traffic semaphore, which is the same
    # counter the red herrings read. One source of truth for "how busy".
    deadline = time.monotonic() + drain_timeout_s
    remaining = generator.in_flight if generator is not None else 0
    while remaining > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        remaining = generator.in_flight

    if remaining:
        log.warning("drain timed out with %s checkouts still in flight", remaining)

    # force_flush pushes whatever the batch processors are holding, so those
    # spans arrive *before* the reset timestamp is taken rather than after it.
    flushed = provider.force_flush(flush_timeout_s * 1000)

    return DrainResult(
        generator_stopped=generator_stopped,
        in_flight_remaining=remaining,
        flush_succeeded=bool(flushed),
        drained_at=datetime.now(UTC),
    )
