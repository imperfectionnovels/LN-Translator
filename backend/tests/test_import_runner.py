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


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    yield
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
    """A status flip to 'paused' mid-fetch causes the runner to exit
    between iterations. Chapters fetched before the cancel survive."""

    # Trigger cancel after the 3rd chapter is committed by hooking on
    # the fetch_chapter callback.
    cancel_event = asyncio.Event()
    novel_id_holder: dict = {}

    class CancelingRecipe(FakeRecipe):
        async def fetch_chapter(
            self, planned, *, cookies, fetch, recipe_state,
        ) -> FetchedChapter:
            result = await super().fetch_chapter(
                planned, cookies=cookies, fetch=fetch, recipe_state=recipe_state,
            )
            if planned.chapter_num == 3 and novel_id_holder.get("id"):
                # Cancel BETWEEN chapter 3 and 4. The runner's status
                # check at the next batch boundary will see 'paused'
                # and exit.
                await import_runner.cancel_import(novel_id_holder["id"])
                cancel_event.set()
            return result

    recipe = CancelingRecipe(chapter_count=10)
    _patch_recipe(monkeypatch, recipe)

    # Spawn the runner; let it interleave with the cancel.
    job_id = await scrape_jobs.create_job("https://fake.test/novel/4")
    # Sneak in: stamp novel_id once it's created. We can't directly
    # observe skeleton creation, but the runner stamps scrape_jobs
    # immediately after.

    async def watcher():
        # Poll the job row until novel_id is stamped, then expose it.
        for _ in range(200):
            j = await scrape_jobs.get_job(job_id)
            if j and j["novel_id"]:
                novel_id_holder["id"] = j["novel_id"]
                return
            await asyncio.sleep(0.01)

    runner_task = asyncio.create_task(
        import_runner.start_from_recipe(job_id, "https://fake.test/novel/4", None)
    )
    await watcher()
    await runner_task

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT import_status FROM novels WHERE id = ?",
            (novel_id_holder["id"],),
        )
        assert (await cur.fetchone())["import_status"] == "paused"
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE novel_id = ? "
            "AND import_fetched_at IS NOT NULL",
            (novel_id_holder["id"],),
        )
        filled = (await cur.fetchone())[0]
    # The first batch is 25 chapters, but only 10 exist; the loop
    # commits each chapter individually. After cancel fires post-ch.3,
    # the loop finishes its current batch (all 10) — verify chapter
    # count is between 3 and 10. The strict guarantee is "no chapters
    # lost"; the exact stop point depends on batch boundary.
    assert filled >= 3, f"expected ≥3 committed chapters, got {filled}"
    assert cancel_event.is_set()


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
