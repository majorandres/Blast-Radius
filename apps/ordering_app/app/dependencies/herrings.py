"""The two red herrings (v1.2 §14.3).

These are permanent properties of the system, on in every scenario including the
healthy baseline. They are not faults and nothing injects them. They exist so
that attribution has to be right for the right reason rather than accidentally.

**Herring one — `loyalty_tier_lookup`.** ~8ms at baseline, ~45ms under load.
That is the largest *relative* rise anywhere in the system, roughly 5.6x, and it
is about one percent of a multi-second trace. Any detector that ranks spans by
how much they grew picks this every time. Ranking by share of the root's wall
time does not.

**Herring two — `analytics.publish`.** Fails at up to 15% under load,
independently of whether the checkout succeeded, and is non-blocking. A detector
that takes "the deepest ERROR span anywhere" blames `ordering-app` on traces
that completed perfectly well. Descending only through blocking spans is what
stops it.

**Deviation from §14.3, recorded.** The contract specifies that herring one gets
its load sensitivity "via a shared semaphore". At 150 orders/min a semaphore
guarding an ~8ms section inside a ~400ms checkout is essentially never
contended: expected concurrent occupancy is about 0.02, so the observed rise was
luck rather than load, averaging 8.2ms against a 37ms peak. The semaphore is
retained -- it is real, it bounds concurrency, and it does queue at higher
throughput -- but the load-dependent component is read from the same in-flight
gauge herring two uses. The specified *observable* (8ms -> 45ms under load) is
what the detector is tested against, and that is now delivered reliably rather
than by coincidence.
"""

import asyncio
import random

from app.traffic.load import gauge

# --- shared load curve -----------------------------------------------------
#: Thresholds on the *smoothed* in-flight mean, calibrated against measurement.
#: Sampled on the running system at 150 orders/min: the raw count is 0-1 when
#: healthy and 1-6 (mean 2.6) under a slow dependency -- ranges that overlap far
#: too much to threshold directly. The smoothed mean separates them, sitting
#: near 1.0 healthy and climbing past 2.5 under load. See `traffic/load.py`.
#: Measured on the running system: smoothed 1.33-1.48 healthy, 3.1-4.5 under a
#: slow promo dependency. The band sits in that gap with margin on both sides,
#: so the herrings are silent at baseline and saturated under fault.
IDLE_IN_FLIGHT = 1.6
LOADED_IN_FLIGHT = 2.6


def load_factor(in_flight: float | None = None) -> float:
    """0.0 when idle, 1.0 when loaded, linear between."""
    n = gauge.smoothed if in_flight is None else in_flight
    if n <= IDLE_IN_FLIGHT:
        return 0.0
    if n >= LOADED_IN_FLIGHT:
        return 1.0
    return (n - IDLE_IN_FLIGHT) / (LOADED_IN_FLIGHT - IDLE_IN_FLIGHT)


# --- herring one -----------------------------------------------------------
LOYALTY_SLOTS = 4
LOYALTY_BASE_MS = (6.0, 9.0)
#: Added at full load, taking a ~8ms lookup to ~45ms.
LOYALTY_LOAD_MS = 37.0

_loyalty_semaphore: asyncio.Semaphore | None = None


def loyalty_semaphore() -> asyncio.Semaphore:
    """Created lazily: an asyncio primitive must be built inside the loop."""
    global _loyalty_semaphore
    if _loyalty_semaphore is None:
        _loyalty_semaphore = asyncio.Semaphore(LOYALTY_SLOTS)
    return _loyalty_semaphore


def loyalty_delay_ms(rng: random.Random, in_flight: float | None = None) -> float:
    return rng.uniform(*LOYALTY_BASE_MS) + LOYALTY_LOAD_MS * load_factor(in_flight)


async def loyalty_tier_lookup(rng: random.Random) -> None:
    """Hold a slot for a load-dependent time.

    The semaphore is genuine contention and bounds concurrency; the load term is
    what makes the rise track load at demo throughput. See the module docstring.
    """
    async with loyalty_semaphore():
        await asyncio.sleep(loyalty_delay_ms(rng) / 1000)


# --- herring two -----------------------------------------------------------
IDLE_FAILURE_PROB = 0.01
LOADED_FAILURE_PROB = 0.15


def analytics_failure_prob(in_flight: float | None = None) -> float:
    """Linear between an idle floor and a loaded ceiling.

    Deliberately independent of the checkout's outcome: a failing analytics
    publish says nothing about whether the order went through, which is the
    whole trap.
    """
    factor = load_factor(in_flight)
    return IDLE_FAILURE_PROB + factor * (LOADED_FAILURE_PROB - IDLE_FAILURE_PROB)


def analytics_fails(rng: random.Random) -> bool:
    return rng.random() < analytics_failure_prob()
