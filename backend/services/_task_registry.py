"""Strong-reference registry for fire-and-forget asyncio tasks.

`asyncio.create_task` keeps only a weak reference to its task, so without an
external reference the event loop can garbage-collect a still-pending task and
silently drop the work (a queued chapter never translated, an import worker
never run). The fix is the same in every worker lane: hold a strong reference
in a set until the task's done-callback fires, then discard it.

This was hand-rolled identically in the translate queue, the free-draft lane,
and the import runner. `BackgroundTaskRegistry` owns that pattern once. Each
lane keeps its OWN registry instance rather than sharing one, so a lane's
shutdown can cancel/await only its own tasks without reaching into another
lane's lifecycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class BackgroundTaskRegistry:
    """Holds strong references to in-flight fire-and-forget tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        on_done: Callable[[asyncio.Task], None] | None = None,
    ) -> asyncio.Task:
        """Schedule `coro`, hold a strong reference until it completes, then
        discard it from the registry. `on_done`, if given, runs after the
        discard for lane-specific cleanup (e.g. clearing a per-chapter slot)."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _finalize(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if on_done is not None:
                on_done(t)

        task.add_done_callback(_finalize)
        return task

    @property
    def tasks(self) -> set[asyncio.Task]:
        """The live strong-reference set. A lane's shutdown iterates a copy of
        this to cancel and await its in-flight tasks."""
        return self._tasks
