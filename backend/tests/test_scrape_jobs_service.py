"""Direct coverage for backend/services/scrape_jobs.py.

The scrape-job service backs the resumable recipe-import flow: a `scrape_jobs`
row tracks pending -> running -> done|error, recipes write progress into it via
short-lived connections, and `spawn` fires the background runner while holding a
strong reference so the GC can't reclaim the in-flight task.

These tests run against a fresh temp SQLite DB (init_db) and assert the row
state machine end-to-end. The actual crawl boundary is stubbed at
`import_runner.start_from_recipe` (which `run_job` delegates to) so nothing
fetches a URL. The strong-ref registry (`_BACKGROUND_TASKS`) is asserted
directly: a tracked task while in flight, discarded on completion.

The module under test is imported at top level and asserted on directly so the
coverage mapping owns it here.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.db import init_db, open_conn

# Direct import of the module under test, this file is its owning test.
from backend.services import scrape_jobs

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def fresh_db():
    """Create the schema in the temp DB and reset the scrape_jobs table + the
    module-level in-flight task set so tests don't leak state into each other."""
    await init_db()
    async with open_conn() as conn:
        await conn.execute("DELETE FROM scrape_jobs")
        await conn.execute("DELETE FROM novels")
        await conn.commit()
    scrape_jobs._BACKGROUND_TASKS.clear()
    yield
    scrape_jobs._BACKGROUND_TASKS.clear()


async def _raw_status(job_id: int) -> str | None:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT status FROM scrape_jobs WHERE id = ?", (job_id,)
        )
        row = await cur.fetchone()
        return None if row is None else row["status"]


# --------------------------------------------------------------------------- #
# create_job / get_job
# --------------------------------------------------------------------------- #


async def test_create_job_inserts_pending_row():
    """create_job returns a real row id and the row starts in 'pending'."""
    job_id = await scrape_jobs.create_job("https://example.com/book/1")
    assert isinstance(job_id, int)
    assert job_id > 0
    assert await _raw_status(job_id) == "pending"

    job = await scrape_jobs.get_job(job_id)
    assert job is not None
    assert job["url"] == "https://example.com/book/1"
    assert job["status"] == "pending"
    # Fresh row defaults: counters zeroed, nothing finished yet.
    assert job["current"] == 0
    assert job["total"] == 0
    assert job["novel_id"] is None
    assert job["finished_at"] is None


async def test_get_job_missing_returns_none():
    """get_job returns None for an id that was never inserted."""
    assert await scrape_jobs.get_job(123456) is None


async def test_get_job_returns_full_shape():
    """The poller shape carries every field the frontend reads, keyed by
    job_id (not the raw 'id' column)."""
    job_id = await scrape_jobs.create_job("https://example.com/x")
    job = await scrape_jobs.get_job(job_id)
    assert set(job) == {
        "job_id",
        "url",
        "status",
        "step",
        "current",
        "total",
        "novel_id",
        "scraped_title",
        "error_message",
        "error_kind",
        "started_at",
        "finished_at",
    }
    assert job["job_id"] == job_id


# --------------------------------------------------------------------------- #
# state transitions: update_progress / set_scraped_title / mark_done / mark_error
# --------------------------------------------------------------------------- #


async def test_update_progress_moves_to_running_and_writes_counters():
    """update_progress flips a pending job to 'running' and stores the step +
    chapter counters."""
    job_id = await scrape_jobs.create_job("https://example.com/y")
    await scrape_jobs.update_progress(job_id, "fetching_chapters", 33, 1424)

    job = await scrape_jobs.get_job(job_id)
    assert job["status"] == "running"
    assert job["step"] == "fetching_chapters"
    assert job["current"] == 33
    assert job["total"] == 1424


async def test_set_scraped_title_persists_without_changing_status():
    """The title stamp is independent of the status, the row stays 'pending'
    until progress/done/error moves it."""
    job_id = await scrape_jobs.create_job("https://example.com/z")
    await scrape_jobs.set_scraped_title(job_id, "苟在初圣魔门当人材")

    job = await scrape_jobs.get_job(job_id)
    assert job["scraped_title"] == "苟在初圣魔门当人材"
    assert job["status"] == "pending"


async def test_mark_done_records_novel_and_finished_at():
    """mark_done is the terminal success transition: status 'done', the linked
    novel_id, and a finished_at timestamp. The novel_id FK references a real
    novel row (open_conn enforces foreign keys), so seed one first."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('Done Novel', 'url')"
        )
        novel_id = cur.lastrowid
        await conn.commit()

    job_id = await scrape_jobs.create_job("https://example.com/done")
    await scrape_jobs.update_progress(job_id, "writing", 10, 10)
    await scrape_jobs.mark_done(job_id, novel_id=novel_id)

    job = await scrape_jobs.get_job(job_id)
    assert job["status"] == "done"
    assert job["novel_id"] == novel_id
    assert job["finished_at"] is not None


async def test_mark_error_records_message_and_kind():
    """mark_error is the terminal failure transition: status 'error', the
    message, and a default error_kind when none is supplied."""
    job_id = await scrape_jobs.create_job("https://example.com/err")
    await scrape_jobs.mark_error(job_id, "timed out fetching catalog")

    job = await scrape_jobs.get_job(job_id)
    assert job["status"] == "error"
    assert job["error_message"] == "timed out fetching catalog"
    assert job["error_kind"] == "unknown"
    assert job["finished_at"] is not None


async def test_mark_error_keeps_explicit_kind():
    """An explicit kind is preserved verbatim (not overwritten by the default)."""
    job_id = await scrape_jobs.create_job("https://example.com/cf")
    await scrape_jobs.mark_error(job_id, "blocked", kind="cloudflare")

    job = await scrape_jobs.get_job(job_id)
    assert job["error_kind"] == "cloudflare"
    assert job["error_message"] == "blocked"


# --------------------------------------------------------------------------- #
# run_job delegates to import_runner.start_from_recipe (stubbed boundary)
# --------------------------------------------------------------------------- #


async def test_run_job_delegates_to_start_from_recipe(monkeypatch):
    """run_job is a thin wrapper: it calls import_runner.start_from_recipe with
    the job id, url, and cookies and runs no crawl of its own."""
    calls: list[tuple] = []

    async def _fake_start(job_id, url, cookies):
        calls.append((job_id, url, cookies))

    monkeypatch.setattr(
        "backend.services.import_runner.start_from_recipe", _fake_start
    )

    await scrape_jobs.run_job(7, "https://example.com/run", "sid=abc")
    assert calls == [(7, "https://example.com/run", "sid=abc")]


# --------------------------------------------------------------------------- #
# spawn, fire-and-forget with the strong-ref registry
# --------------------------------------------------------------------------- #


async def test_spawn_tracks_task_then_discards(monkeypatch):
    """spawn schedules run_job on the loop, holds a strong ref in
    _BACKGROUND_TASKS while in flight, and the done-callback discards it on
    completion."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_run_job(job_id, url, cookies):
        started.set()
        await release.wait()

    monkeypatch.setattr(scrape_jobs, "run_job", _fake_run_job)

    assert scrape_jobs._BACKGROUND_TASKS == set()
    scrape_jobs.spawn(11, "https://example.com/spawn", None)
    await started.wait()

    # Exactly one strong ref held while the task is pending.
    assert len(scrape_jobs._BACKGROUND_TASKS) == 1
    task = next(iter(scrape_jobs._BACKGROUND_TASKS))
    assert not task.done()

    release.set()
    await task
    # done-callback runs on the next loop tick.
    await asyncio.sleep(0)
    assert scrape_jobs._BACKGROUND_TASKS == set()


async def test_spawn_passes_through_args(monkeypatch):
    """spawn forwards (job_id, url, cookies) verbatim to run_job."""
    received: list[tuple] = []
    done = asyncio.Event()

    async def _fake_run_job(job_id, url, cookies):
        received.append((job_id, url, cookies))
        done.set()

    monkeypatch.setattr(scrape_jobs, "run_job", _fake_run_job)

    scrape_jobs.spawn(99, "https://example.com/args", "cookie=1")
    await done.wait()
    await asyncio.sleep(0)  # let the done-callback drain the registry
    assert received == [(99, "https://example.com/args", "cookie=1")]


async def test_spawn_discards_even_when_run_job_raises(monkeypatch):
    """A failing run_job still gets discarded from the registry (the
    done-callback fires on exception too), so a crashed crawl doesn't leak a
    permanent strong ref."""
    boom = asyncio.Event()

    async def _fake_run_job(job_id, url, cookies):
        boom.set()
        raise RuntimeError("crawl exploded")

    monkeypatch.setattr(scrape_jobs, "run_job", _fake_run_job)

    scrape_jobs.spawn(13, "https://example.com/boom", None)
    await boom.wait()
    task = next(iter(scrape_jobs._BACKGROUND_TASKS))
    # The exception is retrieved so the loop doesn't log "never retrieved".
    with pytest.raises(RuntimeError, match="crawl exploded"):
        await task
    await asyncio.sleep(0)
    assert scrape_jobs._BACKGROUND_TASKS == set()
