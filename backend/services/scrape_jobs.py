"""Background scrape-job service.

The recipe path of `POST /api/translate/scrape` used to block the
request for the entire crawl — 25+ minutes for a 1500-chapter 69shuba
novel. Two problems with that:

1. Browsers / proxies time out long requests at various thresholds; no
   robust upper bound.
2. The user has no visibility into progress — the spinner just says
   "Fetching page and importing chapters…" the whole time.

This module replaces that with a fire-and-forget job pattern:

- `create_job(url) -> int` inserts a `scrape_jobs` row in `pending`
  state and returns its id. The route returns this id to the caller
  immediately.
- `run_job(job_id, url, cookies)` is fired via `asyncio.create_task`
  from the route. It calls `scrape_url(...)` with a progress callback
  that writes to the row on every chapter fetch. The task survives
  the request's lifecycle — closing the browser tab or navigating
  away does NOT cancel it (the task is owned by the FastAPI process,
  not the request).
- `get_job(job_id)` reads the current state for the frontend poller.

Recipes opt into progress reporting by accepting a `progress` keyword
on their `scrape()` method. Each recipe calls
`await progress(step, current, total)` at meaningful points; the
service routes that through `update_progress` on its own DB
connection (the recipe's request-scoped conn closes the moment the
route returns the job_id).

Non-recipe scrapes (the generic trafilatura path) don't currently
use this — they're fast enough that blocking works. If we ever need
to background those too, the route layer would call `create_job` +
`run_job` uniformly and the generic path inside `scrape_url` would
just receive `progress=None` and silently no-op.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from backend.db import open_conn

logger = logging.getLogger(__name__)


ProgressFn = Callable[[str, int, int], Awaitable[None]]


async def create_job(url: str) -> int:
    """Insert a pending scrape_jobs row and return the new id."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO scrape_jobs (url, status) VALUES (?, 'pending')",
            (url,),
        )
        await conn.commit()
        return cur.lastrowid


async def update_progress(
    job_id: int, step: str, current: int, total: int,
) -> None:
    """Recipe-callable progress writer. Uses its own short-lived conn so
    the recipe's request-scoped conn isn't touched."""
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE scrape_jobs SET step = ?, current = ?, total = ?, "
            "status = 'running' WHERE id = ?",
            (step, current, total, job_id),
        )
        await conn.commit()


async def set_scraped_title(job_id: int, title: str) -> None:
    """Stamp the title once the recipe has fetched it. Lets the
    frontend show "Importing «苟在初圣魔门当人材» — 33 / 1424" instead
    of a bare URL during the long fetch loop."""
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE scrape_jobs SET scraped_title = ? WHERE id = ?",
            (title, job_id),
        )
        await conn.commit()


async def mark_done(job_id: int, novel_id: int) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE scrape_jobs SET status = 'done', novel_id = ?, "
            "finished_at = datetime('now') WHERE id = ?",
            (novel_id, job_id),
        )
        await conn.commit()


async def mark_error(
    job_id: int, message: str, kind: str | None = None,
) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE scrape_jobs SET status = 'error', error_message = ?, "
            "error_kind = ?, finished_at = datetime('now') WHERE id = ?",
            (message, kind or "unknown", job_id),
        )
        await conn.commit()


async def get_job(job_id: int) -> dict | None:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id, url, status, step, current, total, novel_id, "
            "scraped_title, error_message, error_kind, started_at, "
            "finished_at FROM scrape_jobs WHERE id = ?",
            (job_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "job_id": row["id"],
            "url": row["url"],
            "status": row["status"],
            "step": row["step"],
            "current": row["current"],
            "total": row["total"],
            "novel_id": row["novel_id"],
            "scraped_title": row["scraped_title"],
            "error_message": row["error_message"],
            "error_kind": row["error_kind"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }


async def run_job(
    job_id: int, url: str, cookies: str | None,
) -> None:
    """Background runner. As of 2026-05-26 this is a thin wrapper around
    `import_runner.start_from_recipe` — the runner owns the resumable
    skeleton+fill loop and handles its own scrape_jobs state updates
    (set_scraped_title, update_progress, mark_done, mark_error). This
    function exists to preserve the public `scrape_jobs.spawn(...)` →
    `run_job(...)` API surface used by routes/translate.py."""
    from backend.services.import_runner import start_from_recipe  # noqa: PLC0415
    await start_from_recipe(job_id, url, cookies)


def spawn(job_id: int, url: str, cookies: str | None) -> None:
    """Schedule run_job as a background asyncio task on the running
    event loop. The route returns immediately; the task survives.

    asyncio.create_task() needs a running event loop — under FastAPI's
    Uvicorn worker this is always true when called from inside a route
    handler. We hold a reference to the task in a module-level set so
    Python's garbage collector can't reclaim it mid-run (per the
    asyncio docs).
    """
    task = asyncio.create_task(run_job(job_id, url, cookies))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# Strong refs to in-flight tasks so the GC doesn't reclaim them.
# asyncio.create_task only weakly references its tasks; without this
# set, a long-running scrape can be collected mid-run on a busy loop.
_BACKGROUND_TASKS: set[asyncio.Task] = set()
