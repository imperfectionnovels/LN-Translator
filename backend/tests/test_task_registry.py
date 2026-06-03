"""Tests for BackgroundTaskRegistry: the strong-ref fire-and-forget helper
shared by the translate queue, free-draft lane, and import runner."""

from __future__ import annotations

import asyncio

import pytest

from backend.services._task_registry import BackgroundTaskRegistry

pytestmark = pytest.mark.asyncio


async def test_spawn_holds_ref_while_pending_then_discards():
    reg = BackgroundTaskRegistry()
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    task = reg.spawn(work())
    await started.wait()
    # Strong ref held while the task is in flight.
    assert task in reg.tasks
    assert len(reg.tasks) == 1

    release.set()
    await task
    # The done-callback discards it from the registry.
    assert task not in reg.tasks
    assert reg.tasks == set()


async def test_spawn_returns_the_task_and_runs_the_coro():
    reg = BackgroundTaskRegistry()
    out: list[int] = []

    async def work():
        out.append(42)
        return "result"

    task = reg.spawn(work())
    assert isinstance(task, asyncio.Task)
    assert await task == "result"
    assert out == [42]


async def test_on_done_fires_after_completion():
    reg = BackgroundTaskRegistry()
    seen: list[asyncio.Task] = []

    async def work():
        return None

    task = reg.spawn(work(), on_done=seen.append)
    await task
    # done-callbacks run on the next loop iteration after the task finishes.
    await asyncio.sleep(0)
    assert seen == [task]
    assert task not in reg.tasks


async def test_separate_registries_are_independent():
    a = BackgroundTaskRegistry()
    b = BackgroundTaskRegistry()
    gate = asyncio.Event()

    async def work():
        await gate.wait()

    ta = a.spawn(work())
    await asyncio.sleep(0)
    assert ta in a.tasks
    assert b.tasks == set()  # b is untouched by a's task
    gate.set()
    await ta
