"""System load, shared between the generator and the red herrings.

The traffic semaphore already bounds in-flight checkouts and already exposes a
count for Day 4's drain. That count is the one load signal in the system, so the
herrings read it rather than inventing a second measure that could disagree.

Arrival rate would be the obvious alternative and is the wrong one: it is flat
at 150/min in every scenario and barely moves when a dependency degrades.
In-flight count is what actually rises when checkouts pile up behind a slow
dependency, which is exactly when the herrings need to fire.

**Why the signal is smoothed.** A herring runs *inside* a checkout, so the
instantaneous count it observes is never below one, and at 150 orders/min a
healthy system sits at roughly 0.6 expected concurrency -- meaning the raw
reading alternates between 1 and an occasional 2 with no relation to load. Under
a slow dependency the mean rises to about 2.6, but its *range* still overlaps
the healthy one almost completely. Thresholding the raw count therefore either
fires constantly at idle or never fires at all; both were observed while
calibrating this.

An exponentially-weighted mean separates them cleanly, and it is also the more
faithful model: real systems degrade under sustained pressure, not under a
single concurrent request.
"""

from dataclasses import dataclass, field

#: Slow enough that a brief burst does not register, fast enough that a fault
#: shows up within a few seconds. At ~2.5 admissions/second this averages over
#: roughly the last twenty checkouts.
SMOOTHING_ALPHA = 0.05


@dataclass
class LoadGauge:
    """Set by the traffic generator, read by the herrings.

    `capacity` is recorded for the drain, not for the herrings: it is a safety
    cap at 40, far above the ~1-6 concurrent checkouts this system runs.
    """

    in_flight: int = 0
    capacity: int = 40
    smoothed: float = field(default=0.0)

    def admit(self) -> None:
        self.in_flight += 1
        self.smoothed += SMOOTHING_ALPHA * (self.in_flight - self.smoothed)

    def release(self) -> None:
        self.in_flight -= 1

    def reset(self) -> None:
        self.in_flight = 0
        self.smoothed = 0.0


gauge = LoadGauge()
