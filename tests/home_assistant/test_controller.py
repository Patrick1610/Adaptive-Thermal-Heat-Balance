"""Deterministic push-controller queue and stale-work qualification."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from custom_components.athb.controller import ZoneController


def test_event_storm_keeps_one_running_and_one_latest_request() -> None:
    async def scenario() -> tuple[list[tuple[int, int]], ZoneController[int, int]]:
        release = asyncio.Event()
        published: list[tuple[int, int]] = []

        async def executor(job: Callable[[], int]) -> int:
            await release.wait()
            return job()

        controller = ZoneController(
            executor=executor,
            calculate=lambda value: value * 2,
            publish=lambda generation, result: published.append((generation, result)),
            global_semaphore=asyncio.Semaphore(2),
        )
        controller.request(0)
        await asyncio.sleep(0)
        for value in range(1, 10_000):
            controller.request(value)
        assert controller.pending_count == 1
        assert controller.maximum_pending == 1
        release.set()
        await controller.async_wait_idle()
        return published, controller

    published, controller = asyncio.run(scenario())
    assert published == [(10_000, 19_998)]
    assert controller.completed_jobs == 2
    assert controller.stale_jobs == 1


def test_stop_invalidates_and_cancels_owned_job_without_publication() -> None:
    async def scenario() -> tuple[list[int], ZoneController[int, int]]:
        wait = asyncio.Event()
        published: list[int] = []

        async def executor(job: Callable[[], int]) -> int:
            await wait.wait()
            return job()

        controller = ZoneController(
            executor=executor,
            calculate=lambda value: value,
            publish=lambda _generation, result: published.append(result),
            global_semaphore=asyncio.Semaphore(2),
        )
        controller.request(1)
        await asyncio.sleep(0)
        await controller.async_stop()
        assert controller.request(2) == controller.generation
        return published, controller

    published, controller = asyncio.run(scenario())
    assert published == []
    assert controller.pending_count == 0
