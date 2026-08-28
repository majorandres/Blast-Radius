"""The detection loop.

Runs on its own cadence inside observability-service. It reads telemetry, writes
incidents, and calls nothing. That is the entire surface: no outbound HTTP, no
knowledge of whether a fault is active, no signal that a scenario exists at all
(v1.2 §1.2, §1.3).

The loop is deliberately dull. Everything interesting lives in `slo.evaluate`
and `IncidentTracker`, both of which are pure enough to test against a fixture
without a running system.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from blastradius_contracts.profiles import DetectionProfile

from app.db import engine
from app.detection.analysis import analyse_and_persist
from app.detection.incidents import IncidentTracker
from app.detection.slo import evaluate

log = logging.getLogger(__name__)


class DetectionEngine:
    def __init__(self, profile: DetectionProfile, narrative_provider=None) -> None:
        self._profile = profile
        self._narrative_provider = narrative_provider
        self._tracker = IncidentTracker(
            breach_persistence=profile.breach_persistence,
            recovery_persistence=profile.recovery_persistence,
        )
        self._task: asyncio.Task[None] | None = None
        self._last_evaluation = None

    @property
    def tracker(self) -> IncidentTracker:
        return self._tracker

    @property
    def last_evaluation(self):
        return self._last_evaluation

    async def start(self) -> None:
        async with engine().connect() as conn:
            await self._tracker.load(conn)
        if self._tracker.current is not None:
            log.info("resuming active incident %s (%s)",
                     self._tracker.current.id, self._tracker.current.state)
        self._task = asyncio.create_task(self._run())

    def reset_state(self) -> None:
        """Drop the in-memory incident and its counters.

        The tracker holds consecutive-breach counts between passes. After a
        reset those describe a system that no longer exists, and leaving them
        set would let a single post-reset breach open an incident immediately.
        """
        self._tracker = IncidentTracker(
            breach_persistence=self._profile.breach_persistence,
            recovery_persistence=self._profile.recovery_persistence,
        )
        self._last_evaluation = None

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        p = self._profile
        log.info("detection loop: profile=%s interval=%ss window=%ss min_samples=%s",
                 p.name, p.slo_eval_interval_s, p.slo_window_s, p.slo_min_samples)
        while True:
            await asyncio.sleep(p.slo_eval_interval_s)
            try:
                await self.evaluate_once()
            except Exception:
                # A failed pass must not kill detection. The next tick retries,
                # and the consecutive counters are unchanged by a pass that
                # never produced a reading.
                log.exception("detection pass failed")

    async def evaluate_once(self) -> None:
        p = self._profile
        async with engine().connect() as conn:
            evaluation = await evaluate(
                conn,
                window_s=p.slo_window_s,
                settle_s=p.trace_settle_s,
                min_samples=p.slo_min_samples,
                now=datetime.now(UTC),
            )
            incident = await self._tracker.observe(
                conn, evaluation,
                baseline_window_s=p.baseline_window_s,
                baseline_guard_s=p.baseline_guard_s,
            )
            # Re-analysed every pass while the incident runs. The window grows,
            # so the diagnosis sharpens; the baseline it is measured against
            # was frozen at open and never moves.
            if incident is not None and incident.state in ("OPEN", "RECOVERING"):
                await analyse_and_persist(conn, incident, p, self._narrative_provider)
            await conn.commit()

        self._last_evaluation = evaluation
        if evaluation.breached:
            log.info(
                "breach: %s (n=%s)",
                ", ".join(f"{r.name}={r.observed:.4g}" for r in evaluation.breached_readings),
                evaluation.sample_count,
            )
