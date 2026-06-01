"""Resumable-import runner: crash / cancel / resume / drain invariants.

The runner mirrors the translator's translate_queued + drain_on_startup
pattern. These tests pin the durability guarantees:

1. **Crash mid-fetch leaves partial state.** A fault at chapter 5/10 must
   leave the novel with chapters 1-4 committed + status='in_progress'.
2. **Drain re-resumes recipe scrapes.** drain_imports_on_startup spawns
   the runner for in-progress novels with pending skeleton URLs.
3. **Cancel flips to paused but keeps partial.** Mid-loop status flip
   exits the loop at the next iteration with chapters intact.
4. **Resume picks up where left off.** A paused → in_progress flip
   re-fires the runner and fills only the remaining chapters.
5. **Bulk / EPUB incremental insert.** insert_chapters_incrementally
   commits in batches; a crash mid-batch is detectable by drain.

Recipe is stubbed via a `FakeRecipe` that records every fetch_chapter
call and supports fault injection ("fail at chapter N").
"""

from __future__ import annotations

import asyncio

import pytest

from backend.db import init_db, open_conn
from backend.services import import_runner, scrape_jobs
from backend.services.parser import ParsedChapter
from backend.services.scraper import ScrapeError
from backend.services.scrapers.base import (
    BaseRecipe,
    FetchedChapter,
    PlannedChapterRef,
    RecipePlan,
)


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in ("chapters", "novels", "scrape_jobs"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await conn.commit()


async def _drain_runner_tasks() -> None:
    """Await any background runner tasks left over from a prior test.

    drain_imports_on_startup() / spawn_resume() schedule fill-loop tasks via
    import_runner._spawn(); if a previous test returns before those settle,
    they keep mutating the shared temp DB during the next test. Awaiting them
    here (with a bounded shield) makes each test own a quiet DB at setup time
    and prevents cross-test contamination of novels / chapters / scrape_jobs.
    """
    for _ in range(50):
        pending = [t for t in import_runner._RUNNER_TASKS if not t.done()]
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=5.0,
            )
        except asyncio.TimeoutError:
            return


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _drain_runner_tasks()
    await _reset_db()
    yield
    await _drain_runner_tasks()
    await _reset_db()


# ============================================================
# FakeRecipe: catalog + per-chapter fetch under test control
# ============================================================

class FakeRecipe(BaseRecipe):
    """A test-only recipe that emits a fixed chapter list and lets each
    test inject faults at specific chapter indices."""
    name = "fake"
    default_genre = None

    def __init__(self, *, chapter_count: int = 10, fail_at: int | None = None):
        self.chapter_count = chapter_count
        self.fail_at = fail_at
        self.fetched_indices: list[int] = []

    def matches(self, hostname: str) -> bool:
        return hostname == "fake.test"

    async def plan(self, url, *, cookies, fetch, progress=None) -> RecipePlan:
        chapters = tuple(
            PlannedChapterRef(
                chapter_num=i,
                title_zh=f"第{i}章",
                source_url=f"https://fake.test/ch/{i}",
                printed_num=i,
            )
            for i in range(1, self.chapter_count + 1)
        )
        return RecipePlan(
            title="Test Novel",
            source_url=url,
            catalog_url=url,
            cover_url=None,
            chapters=chapters,
            recipe_state={"referer": url},
        )

    async def fetch_chapter(
        self, planned, *, cookies, fetch, recipe_state,
    ) -> FetchedChapter:
        idx = planned.chapter_num
        self.fetched_indices.append(idx)
        if self.fail_at is not None and idx == self.fail_at:
            raise ScrapeError(f"fake: injected fault at chapter {idx}")
        return FetchedChapter(
            title_zh=planned.title_zh,
            original_text=f"chapter {idx} body text " * 5,
        )


def _patch_recipe(monkeypatch, recipe: FakeRecipe) -> None:
    """Inject FakeRecipe into the dispatcher so the runner picks it up
    for our test URLs."""
    monkeypatch.setattr(
        "backend.services.import_runner.recipe_dispatch",
        lambda host: recipe if host == "fake.test" else None,
    )


# ============================================================
# Tests
# ============================================================

async def test_happy_path_fills_all_chapters(monkeypatch):
    """No fault → every skeleton chapter ends up filled + novel done."""
    recipe = FakeRecipe(chapter_count=10)
    _patch_recipe(monkeypatch, recipe)

    job_id = await scrape_jobs.create_job("https://fake.test/novel/1")
    await import_runner.start_from_recipe(job_id, "https://fake.test/novel/1", None)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT import_status FROM novels WHERE title = 'Test Novel'"
        )
        row = await cur.fetchone()
        assert row["import_status"] == "done"

        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM chapters c "
            "JOIN novels n ON c.novel_id = n.id "
            "WHERE n.title = 'Test Novel' AND c.import_fetched_at IS NOT NULL"
        )
        assert (await cur.fetchone())["n"] == 10

    job = await scrape_jobs.get_job(job_id)
    assert job["status"] == "done"


async def test_crash_mid_fetch_leaves_partial(monkeypatch):
    """Injected fault at chapter 6 → novel ends in_progress → paused with
    5 chapters committed."""
    recipe = FakeRecipe(chapter_count=10, fail_at=6)
    _patch_recipe(monkeypatch, recipe)

    job_id = await scrape_jobs.create_job("https://fake.test/novel/2")
    await import_runner.start_from_recipe(job_id, "https://fake.test/novel/2", None)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id, import_status FROM novels WHERE title = 'Test Novel'"
        )
        row = await cur.fetchone()
        # The runner pauses on a per-chapter ScrapeError — partial novel kept.
        assert row["import_status"] == "paused"
        novel_id = row["id"]

        cur = await conn.execute(
            "SELECT COUNT(*) AS filled FROM chapters "
            "WHERE novel_id = ? AND import_fetched_at IS NOT NULL",
            (novel_id,),
        )
        assert (await cur.fetchone())["filled"] == 5  # chapters 1-5

        cur = await conn.execute(
            "SELECT COUNT(*) AS pending FROM chapters "
            "WHERE novel_id = ? AND import_fetched_at IS NULL "
            "AND import_source_url IS NOT NULL",
            (novel_id,),
        )
        assert (await cur.fetchone())["pending"] == 5  # chapters 6-10


async def test_resume_picks_up_where_left_off(monkeypatch):
    """After a partial crash, calling resume_recipe_import refills the
    remaining chapters and leaves the novel done."""
    # Phase 1: crash at chapter 4.
    recipe = FakeRecipe(chapter_count=8, fail_at=4)
    _patch_recipe(monkeypatch, recipe)
    job_id = await scrape_jobs.create_job("https://fake.test/novel/3")
    await import_runner.start_from_recipe(job_id, "https://fake.test/novel/3", None)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id FROM novels WHERE title = 'Test Novel'"
        )
        novel_id = (await cur.fetchone())["id"]

    # Phase 2: swap in a no-fault recipe, flip back to in_progress, resume.
    healed = FakeRecipe(chapter_count=8)
    _patch_recipe(monkeypatch, healed)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE novels SET import_status = 'in_progress' WHERE id = ?",
            (novel_id,),
        )
        await conn.commit()
    await import_runner.resume_recipe_import(novel_id)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT import_status FROM novels WHERE id = ?", (novel_id,),
        )
        assert (await cur.fetchone())["import_status"] == "done"
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE novel_id = ? "
            "AND import_fetched_at IS NOT NULL", (novel_id,),
        )
        assert (await cur.fetchone())[0] == 8

    # Resume only fetched the missing chapters, not chapters 1-3 again.
    assert healed.fetched_indices == [4, 5, 6, 7, 8]


async def test_cancel_during_fill_exits_cleanly(monkeypatch):
    """A status flip to 'paused' mid-fetch causes the runner to exit at
    the next batch boundary. Chapters fetched before the cancel survive.

    Deterministic by construction: the cancel is fired from inside the
    recipe's fetch_chapter the moment chapter 3 is reached, after looking
    the novel_id up directly from the DB. The skeleton (the novel row
    carrying title 'Test Novel') is committed by _create_novel_skeleton
    BEFORE the fill loop calls fetch_chapter for chapter 1, so the
    novel_id is always resolvable here, with no cross-task polling race.
    """

    cancel_event = asyncio.Event()
    cancelled_novel_id: dict = {}

    class CancelingRecipe(FakeRecipe):
        async def fetch_chapter(
            self, planned, *, cookies, fetch, recipe_state,
        ) -> FetchedChapter:
            result = await super().fetch_chapter(
                planned, cookies=cookies, fetch=fetch, recipe_state=recipe_state,
            )
            if planned.chapter_num == 3:
                # Resolve our own novel_id from the committed skeleton and
                # cancel. cancel_import flips import_status to 'paused';
                # the fill loop's between-batch status check then exits.
                async with open_conn() as conn:
                    cur = await conn.execute(
                        "SELECT id FROM novels WHERE title = 'Test Novel'"
                    )
                    nid = (await cur.fetchone())["id"]
                flipped = await import_runner.cancel_import(nid)
                assert flipped is True, "cancel_import should flip in_progress"
                cancelled_novel_id["id"] = nid
                cancel_event.set()
            return result

    recipe = CancelingRecipe(chapter_count=10)
    _patch_recipe(monkeypatch, recipe)

    job_id = await scrape_jobs.create_job("https://fake.test/novel/4")
    # Run the runner to completion inline. No separate watcher task is
    # needed: the cancel is self-contained in the recipe, so the result
    # does not depend on event-loop scheduling order.
    await import_runner.start_from_recipe(
        job_id, "https://fake.test/novel/4", None,
    )

    assert cancel_event.is_set(), "cancel hook never fired"
    novel_id = cancelled_novel_id["id"]

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT import_status FROM novels WHERE id = ?", (novel_id,),
        )
        # The cancel always fires (deterministic), so the novel must end
        # paused, never 'done'. This is the core invariant the test pins.
        assert (await cur.fetchone())["import_status"] == "paused"
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE novel_id = ? "
            "AND import_fetched_at IS NOT NULL",
            (novel_id,),
        )
        filled = (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE novel_id = ?", (novel_id,),
        )
        total = (await cur.fetchone())[0]

    # _drive_fill only re-checks import_status at the TOP of the while
    # loop, between batches, not within a batch's for-loop. The first
    # batch (LIMIT 25, but only 10 rows exist) is processed fully before
    # the status re-check sees 'paused', so all 10 chapters commit. The
    # contract under test: no committed chapter is lost (>= the 3 fetched
    # before cancel) and every skeleton row is preserved.
    assert filled >= 3, f"expected >=3 committed chapters, got {filled}"
    assert filled == 10, (
        f"first batch should finish before the status re-check, got {filled}"
    )
    assert total == 10, "skeleton rows must be preserved, not deleted"


async def test_drain_resumes_in_progress_recipe(monkeypatch):
    """drain_imports_on_startup spawns the runner for any recipe novel
    still in import_status='in_progress' with pending skeleton URLs."""
    # Phase 1: partial crash.
    recipe = FakeRecipe(chapter_count=6, fail_at=4)
    _patch_recipe(monkeypatch, recipe)
    job_id = await scrape_jobs.create_job("https://fake.test/novel/5")
    await import_runner.start_from_recipe(job_id, "https://fake.test/novel/5", None)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id FROM novels WHERE title = 'Test Novel'"
        )
        novel_id = (await cur.fetchone())["id"]
        # Simulate the "crash" state: status was 'paused' after the
        # injected fault; flip back to in_progress to mimic a server
        # that died mid-fetch.
        await conn.execute(
            "UPDATE novels SET import_status = 'in_progress' WHERE id = ?",
            (novel_id,),
        )
        await conn.commit()

    # Phase 2: drain. Swap in healed recipe.
    healed = FakeRecipe(chapter_count=6)
    _patch_recipe(monkeypatch, healed)
    await import_runner.drain_imports_on_startup()
    # drain spawns a background task; wait for it to settle.
    for _ in range(200):
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT import_status FROM novels WHERE id = ?", (novel_id,),
            )
            if (await cur.fetchone())["import_status"] == "done":
                break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("drain did not complete the import within timeout")


async def test_drain_flips_orphan_bulk_to_paused(monkeypatch):
    """A novel left in 'in_progress' with no pending skeleton URLs is a
    bulk/EPUB partial. Drain marks it paused (the source is gone)."""
    # Create a novel directly: in_progress, chapters with no
    # import_source_url, none pending.
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, import_status) "
            "VALUES (?, 'txt', 'in_progress')",
            ("Orphan Bulk",),
        )
        novel_id = cur.lastrowid
        for i in range(1, 4):
            await conn.execute(
                "INSERT INTO chapters (novel_id, chapter_num, original_text, "
                "status, import_fetched_at) "
                "VALUES (?, ?, ?, 'pending', datetime('now'))",
                (novel_id, i, f"body {i}"),
            )
        await conn.commit()

    await import_runner.drain_imports_on_startup()

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT import_status FROM novels WHERE id = ?", (novel_id,),
        )
        assert (await cur.fetchone())["import_status"] == "paused"
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE novel_id = ?", (novel_id,),
        )
        assert (await cur.fetchone())[0] == 3  # chapters preserved


async def test_insert_chapters_incrementally_commits_per_batch():
    """Bulk path: insert_chapters_incrementally commits in batches.
    Verifies the novel ends in 'done' with all chapters present."""
    chapters = [
        ParsedChapter(
            chapter_num=i,
            title_zh=f"第{i}章",
            original_text=f"body of chapter {i}",
        )
        for i in range(1, 121)  # 120 chapters → batches of 50,50,20
    ]
    novel_id = await import_runner.insert_chapters_incrementally(
        title="Bulk Test",
        decoded_chapters=chapters,
        source_type="txt",
        source_url=None,
        genre=None,
        source_language="zh",
        batch_size=50,
    )

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT import_status FROM novels WHERE id = ?", (novel_id,),
        )
        assert (await cur.fetchone())["import_status"] == "done"
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE novel_id = ?", (novel_id,),
        )
        assert (await cur.fetchone())[0] == 120


async def test_cancel_is_idempotent_on_non_in_progress():
    """cancel_import on a novel that isn't in_progress is a no-op."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, import_status) "
            "VALUES (?, 'paste', 'done')",
            ("Done Novel",),
        )
        novel_id = cur.lastrowid
        await conn.commit()
    flipped = await import_runner.cancel_import(novel_id)
    assert flipped is False
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT import_status FROM novels WHERE id = ?", (novel_id,),
        )
        assert (await cur.fetchone())["import_status"] == "done"
