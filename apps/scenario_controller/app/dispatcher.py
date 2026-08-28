"""Fault dispatch: ramp, hold, clear.

Runs the scenario's lifecycle as a background task and drives the state machine
alongside it. Faults ramp from zero rather than snapping to full strength,
because a step change makes detection latency meaningless — the detector would
be reacting to an event no real dependency produces.

Every outbound call has an explicit timeout (v1.2 §23). A dispatch failure ends
the run rather than leaving the system degraded with the controller unaware.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.scenarios import Scenario, scale
from app.state_machine import complete, transition

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 5.0
RAMP_STEPS = 5


class Dispatcher:
    def __init__(
        self,
        engine: AsyncEngine,
        client: httpx.AsyncClient,
        *,
        promo_url: str,
        ordering_url: str,
        ramp_s: int,
        hold_s: int,
    ) -> None:
        self._engine = engine
        self._client = client
        self._promo_url = promo_url.rstrip("/")
        self._ordering_url = ordering_url.rstrip("/")
        self._ramp_s = ramp_s
        self._hold_s = hold_s
        self._task: asyncio.Task[None] | None = None

    async def clear_all(self) -> None:
        """Return both services to healthy. Safe to call at any time."""
        await self._put(f"{self._promo_url}/_faults", {})
        await self._put(f"{self._ordering_url}/_faults", {})

    def start(self, run_id, scenario: Scenario) -> None:
        self._task = asyncio.create_task(self._run(run_id, scenario))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _put(self, url: str, payload: dict) -> None:
        response = await self._client.put(url, json=payload, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()

    async def _apply(self, scenario: Scenario, factor: float) -> None:
        if scenario.promo_faults:
            await self._put(f"{self._promo_url}/_faults", scale(scenario.promo_faults, factor))
        if scenario.ordering_faults:
            await self._put(
                f"{self._ordering_url}/_faults", scale(scenario.ordering_faults, factor)
            )

    async def _run(self, run_id, scenario: Scenario) -> None:
        async with self._engine.connect() as conn:
            try:
                await transition(conn, run_id, "INJECTING")
                step_delay = self._ramp_s / RAMP_STEPS
                for step in range(1, RAMP_STEPS + 1):
                    await self._apply(scenario, step / RAMP_STEPS)
                    await asyncio.sleep(step_delay)
                log.info("scenario %s at full strength", scenario.name)

                await transition(conn, run_id, "ACTIVE")
                await asyncio.sleep(self._hold_s)

                await transition(conn, run_id, "RECOVERING")
                await self.clear_all()
                log.info("scenario %s cleared", scenario.name)

                await complete(conn, run_id, datetime.now(UTC))
                log.info("scenario %s COMPLETE", scenario.name)
            except asyncio.CancelledError:
                # Stopped by hand. Clear the fault before unwinding, or the
                # system stays degraded with no run to explain it.
                await self.clear_all()
                with contextlib.suppress(Exception):
                    await complete(conn, run_id, datetime.now(UTC))
                raise
            except Exception:
                log.exception("scenario %s dispatch failed", scenario.name)
                await self.clear_all()
                with contextlib.suppress(Exception):
                    await complete(conn, run_id, datetime.now(UTC))
