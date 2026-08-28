"""Seeded Poisson traffic generator (v1.2 §8).

Arrivals are exponential, not a fixed sleep. A perfectly flat baseline makes p95
meaningless, and p95_latency is one of the two SLOs the detector opens incidents
on -- a metronome would hand Day 2 a degenerate distribution to threshold
against.

The semaphore bounds in-flight checkouts and is also the drain mechanism on
Day 4, so in-flight count is derived from it rather than tracked separately.

Cohort draws are reproducible under a fixed seed; wall-clock timing is not, and
that is fine. Day 2 needs the same cohort *sequence* to replay a scenario, not
the same microsecond.
"""

import asyncio
import contextlib
import logging
import random
import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.checkout import Order, run_checkout
from app.traffic.load import gauge

log = logging.getLogger(__name__)

# v1.2 §8. Independent draws, not a joint distribution.
CHANNELS = (("mobile", 55), ("web", 35), ("aggregator", 10))
PAYMENT_METHODS = (("card", 60), ("wallet", 25), ("other", 15))
HAS_PROMO_PCT = 35


def _weighted(rng: random.Random, choices: tuple[tuple[str, int], ...]) -> str:
    return rng.choices([c for c, _ in choices], weights=[w for _, w in choices])[0]


def draw_order(rng: random.Random) -> Order:
    return Order(
        id=uuid.uuid4(),
        channel=_weighted(rng, CHANNELS),
        has_promo=rng.randint(1, 100) <= HAS_PROMO_PCT,
        payment_method=_weighted(rng, PAYMENT_METHODS),
    )


class TrafficGenerator:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        promo_client: httpx.AsyncClient,
        promo_base_url: str,
        promo_timeout_ms: int,
        rate_per_min: int,
        max_concurrency: int,
        seed: int,
    ) -> None:
        self._engine = engine
        self._promo_client = promo_client
        self._promo_base_url = promo_base_url
        self._promo_timeout_ms = promo_timeout_ms
        self._rate_per_min = rate_per_min
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rng = random.Random(seed)
        gauge.capacity = max_concurrency
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def in_flight(self) -> int:
        """Derived from the semaphore, which Day 4's drain also reads."""
        return self._max_concurrency - self._semaphore._value

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self) -> None:
        mean_interval = 60.0 / self._rate_per_min
        log.info("traffic: %s orders/min, semaphore %s", self._rate_per_min,
                 self._max_concurrency)
        while True:
            await asyncio.sleep(self._rng.expovariate(1.0 / mean_interval))
            task = asyncio.create_task(self._one())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _one(self) -> None:
        order = draw_order(self._rng)
        async with self._semaphore:
            # Counted explicitly rather than derived from semaphore internals,
            # so the herrings read an exact number at both edges.
            gauge.admit()
            try:
                await run_checkout(
                    order,
                    rng=self._rng,
                    engine=self._engine,
                    promo_client=self._promo_client,
                    promo_base_url=self._promo_base_url,
                    promo_timeout_ms=self._promo_timeout_ms,
                )
            except Exception:
                # A checkout that blows up must not kill the generator. The
                # failure is already recorded on the trace.
                log.exception("checkout raised")
            finally:
                gauge.release()
