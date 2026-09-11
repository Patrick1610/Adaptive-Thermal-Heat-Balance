"""Push-only, bounded zone calculation orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TypeVar

SnapshotT = TypeVar("SnapshotT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class CalculationRequest[SnapshotT]:
    generation: int
    snapshot: SnapshotT


class ZoneController[SnapshotT, ResultT]:
    """Run one job per zone, retain one latest request, and drop stale results."""

    def __init__(
        self,
        *,
        executor: Callable[[Callable[[], ResultT]], Awaitable[ResultT]],
        calculate: Callable[[SnapshotT], ResultT],
        publish: Callable[[int, ResultT], None],
        global_semaphore: asyncio.Semaphore,
    ) -> None:
        self._executor = executor
        self._calculate = calculate
        self._publish = publish
        self._semaphore = global_semaphore
        self._generation = 0
        self._running: asyncio.Task[None] | None = None
        self._latest: CalculationRequest[SnapshotT] | None = None
        self._stopped = False
        self.completed_jobs = 0
        self.stale_jobs = 0
        self.maximum_pending = 0

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def pending_count(self) -> int:
        return int(self._latest is not None)

    def request(self, snapshot: SnapshotT) -> int:
        if self._stopped:
            return self._generation
        self._generation += 1
        request = CalculationRequest(self._generation, snapshot)
        if self._running is not None and not self._running.done():
            self._latest = request
            self.maximum_pending = max(self.maximum_pending, 1)
        else:
            self._running = asyncio.create_task(self._run(request))
        return request.generation

    def invalidate(self) -> int:
        self._generation += 1
        self._latest = None
        return self._generation

    async def _run(self, request: CalculationRequest[SnapshotT]) -> None:
        current: CalculationRequest[SnapshotT] | None = request
        while current is not None and not self._stopped:
            snapshot = current.snapshot
            async with self._semaphore:
                result = await self._executor(partial(self._calculate, snapshot))
            self.completed_jobs += 1
            if not self._stopped and current.generation == self._generation:
                self._publish(current.generation, result)
            else:
                self.stale_jobs += 1
            current = self._latest
            self._latest = None

    async def async_stop(self) -> None:
        self._stopped = True
        self.invalidate()
        running = self._running
        if running is not None and not running.done():
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running

    async def async_wait_idle(self) -> None:
        running = self._running
        if running is not None:
            await asyncio.shield(running)
