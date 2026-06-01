"""Resumable import runner.

Long-running novel imports (recipe scrapes, bulk file uploads, big
EPUB/DOCX parses) used to materialize the full chapter list in memory
and write everything in one atomic transaction. A crash or restart
midway lost the entire crawl. This module replaces that pattern with
**skeleton + fill**:

1. *Discover.* Call the recipe's `plan()` (for scrapes) or decode the
   uploaded files (for bulk/EPUB) to learn how many chapters exist and
   their per-chapter source URL / decoded content.

2. *Skeleton create.* In one short transaction, INSERT the novel row
   with `import_status='in_progress'` + N pending chapter rows
   (`original_text=''`, `import_source_url=<URL>`, status='pending').
   For bulk / EPUB we don't pre-create skeletons — we INSERT each
   chapter as it's decoded.

3. *Fill.* Loop over the pending chapters, calling
   `recipe.fetch_chapter` (scrape) or pulling from the in-memory list
   (bulk/EPUB) and `_fill_skeleton_chapter` to commit each one. A
   crash leaves partial state intact — any chapter with
   `import_fetched_at IS NULL` is still pending.

4. *Boot recovery.* `drain_imports_on_startup()` scans for novels in
   `import_status='in_progress'`. Scrape-derived novels (those whose
   pending chapters carry `import_source_url`) get auto-resumed by
   re-running the runner. Bulk / EPUB novels can't be resumed — the
   source bytes lived in the request body and are gone — so they flip
   to `'paused'` and wait for the user.

The translator pipeline's analogous pattern is
`chapters.translate_queued` + `queue.drain_on_startup()` in
`services/queue.py`. This module mirrors that shape.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

from backend.db import open_conn
from backend.services import scrape_jobs
from backend.services.parser import (
    ParsedChapter,
    reconcile_chapter_numbers,
)
from backend.services.scraper import ScrapeError, fetch_one
from backend.services.scrapers import dispatch as recipe_dispatch
from backend.services.scrapers.base import (
    BaseRecipe,
    PlannedChapterRef,
)
from backend.services.uploads import (
    PlannedChapter,
    _count_pending_skeletons,
    _create_novel_skeleton,
    _fill_skeleton_chapter,
    _set_novel_import_status,
)

logger = logging.getLogger(__name__)


# Per-novel lock — prevents a manual /resume firing while drain_on_startup
# is already running a runner for the same novel. In-memory only; cross-
# process isn't a concern (single-worker app).
_novel_locks: dict[int, asyncio.Lock] = {}


def _novel_lock(novel_id: int) -> asyncio.Lock:
    lock = _novel_locks.get(novel_id)
    if lock is None:
        lock = asyncio.Lock()
        _novel_locks[novel_id] = lock
    return lock


# Strong refs to running runner tasks so Python's GC can't reclaim them
# mid-loop. asyncio.create_task only weakly references its tasks.
_RUNNER_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _RUNNER_TASKS.add(task)
    task.add_done_callback(_RUNNER_TASKS.discard)
    return task


# ============================================================
# Recipe path: discover + fill, fully resumable.
# ============================================================

async def start_from_recipe(
    job_id: int,
    url: str,
    cookies: str | None,
) -> None:
    """Entry point invoked by routes/translate.py for the /scrape recipe
    branch. Runs the full discover → skeleton → fill flow. Exceptions
    propagate into `scrape_jobs.mark_error`; on success the job is
    marked done and the novel is flipped to `import_status='done'`.

    Owns its own DB connection. Safe to call from `asyncio.create_task`.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower().lstrip()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        recipe = recipe_dispatch(hostname)
        if recipe is None:
            raise ScrapeError(
                f"No recipe matches host {hostname!r}. Paste a URL from a "
                "supported site (piaotia.com, 69shuba.com, ncode.syosetu.com, "
                "uukanshu.cc)."
            )

        # Phase 1: discover. Catalog page → planned chapter list.
        async def progress(step: str, current: int, total: int) -> None:
            await scrape_jobs.update_progress(job_id, step, current, total)

        await scrape_jobs.update_progress(job_id, "fetching_overview", 0, 0)
        plan = await recipe.plan(
            url, cookies=cookies, fetch=fetch_one, progress=progress,
        )

        # Phase 2: skeleton create. Reconcile chapter_num against printed
        # numbers BEFORE the INSERT so the rows land with their canonical
        # values (UNIQUE(novel_id, chapter_num) means we can't fix them
        # later without a migration). We re-use reconcile_chapter_numbers
        # via a list of ParsedChapter-shaped placeholders.
        reconciled_planned = _reconcile_planned(plan.chapters)

        # Detect source language from the FIRST chapter we'll fetch — but
        # we don't have its body yet. Defer to a sample fetch? Simpler:
        # detect from the first chapter title plus a fallback after the
        # first body lands. For now, leave NULL and let _atomic-style
        # callers fill it via a separate path. Source language is just a
        # hint; the translator works without it.
        async with open_conn() as conn:
            novel_id = await _create_novel_skeleton(
                conn,
                title=plan.title,
                planned=[
                    PlannedChapter(
                        chapter_num=p.chapter_num,
                        title_zh=p.title_zh,
                        source_url=p.source_url,
                    )
                    for p in reconciled_planned
                ],
                source_type="url",
                source_url=plan.catalog_url,
                genre=recipe.default_genre,
            )
            await scrape_jobs.set_scraped_title(job_id, plan.title)
            # Stamp the job's novel_id NOW so the frontend can navigate to
            # the in-progress novel while the fill loop runs (the legacy
            # path only stamped this at success).
            await conn.execute(
                "UPDATE scrape_jobs SET novel_id = ? WHERE id = ?",
                (novel_id, job_id),
            )
            await conn.commit()

        logger.info(
            "import_runner: skeleton created for novel %d (%r) — "
            "%d chapters pending", novel_id, plan.title, len(reconciled_planned),
        )

        # Phase 3: fill loop.
        await _drive_fill(
            novel_id=novel_id,
            recipe=recipe,
            cookies=cookies,
            recipe_state=dict(plan.recipe_state),
            job_id=job_id,
            cover_url=plan.cover_url,
            total=len(reconciled_planned),
        )

        # Phase 4: done.
        await scrape_jobs.mark_done(job_id, novel_id)
        logger.info("import_runner: novel %d import complete", novel_id)

    except ScrapeError as e:
        await scrape_jobs.mark_error(
            job_id, str(e), kind=getattr(e, "error_kind", "unknown"),
        )
        logger.info("import_runner: job %d failed: %s", job_id, e)
    except Exception as e:  # noqa: BLE001 — catch-all for background task
        await scrape_jobs.mark_error(
            job_id, f"Unexpected error: {e}", kind="internal",
        )
        logger.exception("import_runner: job %d crashed", job_id)


def _reconcile_planned(
    plan_chapters: tuple[PlannedChapterRef, ...],
) -> list[PlannedChapterRef]:
    """Re-anchor chapter_num against printed_num where present, same
    pattern as parser.reconcile_chapter_numbers. Returns a new list
    sorted by chapter_num so the skeleton inserts land in order."""
    parsed = [
        ParsedChapter(
            chapter_num=p.chapter_num,
            title_zh=p.title_zh,
            original_text="",  # placeholder; reconcile only uses chapter_num + printed_num
            printed_num=p.printed_num,
        )
        for p in plan_chapters
    ]
    reconcile_chapter_numbers(parsed)
    return [
        PlannedChapterRef(
            chapter_num=parsed[i].chapter_num,
            title_zh=plan_chapters[i].title_zh,
            source_url=plan_chapters[i].source_url,
            printed_num=plan_chapters[i].printed_num,
        )
        for i in range(len(plan_chapters))
    ]


async def resume_recipe_import(novel_id: int) -> None:
    """Drain-on-startup callback: re-fire the fill loop for a novel whose
    `import_status='in_progress'` and whose chapters carry
    `import_source_url`. Re-derives the recipe + recipe_state from
    `novels.source_url`; no in-memory plan is needed.

    Idempotent — safe to call on a novel that's already done (the fill
    loop sees zero pending chapters and exits cleanly)."""
    async with _novel_lock(novel_id):
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT source_url, title, import_status FROM novels "
                "WHERE id = ?",
                (novel_id,),
            )
            row = await cur.fetchone()
        if row is None:
            logger.warning("resume_recipe_import: novel %d not found", novel_id)
            return
        if row["import_status"] not in ("in_progress",):
            logger.info(
                "resume_recipe_import: novel %d has import_status=%r — "
                "skipping (only 'in_progress' is auto-resumed)",
                novel_id, row["import_status"],
            )
            return
        source_url = row["source_url"]
        if not source_url:
            logger.warning(
                "resume_recipe_import: novel %d has NULL source_url — "
                "can't determine recipe; marking paused", novel_id,
            )
            async with open_conn() as conn:
                await _set_novel_import_status(conn, novel_id, "paused")
            return

        parsed = urllib.parse.urlparse(source_url)
        hostname = (parsed.hostname or "").lower().lstrip()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        recipe = recipe_dispatch(hostname)
        if recipe is None:
            logger.warning(
                "resume_recipe_import: no recipe for host %r — marking paused",
                hostname,
            )
            async with open_conn() as conn:
                await _set_novel_import_status(conn, novel_id, "paused")
            return

        # Reconstruct recipe_state. Every current recipe stuffs the
        # catalog URL as the Referer; on resume we re-derive it from the
        # novel's source_url. If future recipes need richer state, we'll
        # need to persist it on the novels table.
        recipe_state = {"referer": source_url}

        logger.info(
            "import_runner: resuming novel %d (%r) via %s",
            novel_id, row["title"], recipe.name,
        )
        async with open_conn() as conn:
            pending = await _count_pending_skeletons(conn, novel_id)
        if pending == 0:
            # All chapters filled but novel still flagged in_progress —
            # likely a crash AFTER the last fill commit but BEFORE the
            # status flip. Just clean up.
            async with open_conn() as conn:
                await _set_novel_import_status(conn, novel_id, "done")
            logger.info(
                "import_runner: novel %d had 0 pending; marked done",
                novel_id,
            )
            return

        await _drive_fill(
            novel_id=novel_id,
            recipe=recipe,
            cookies=None,  # resume path doesn't have the original cookies
            recipe_state=recipe_state,
            job_id=None,
            cover_url=None,
            total=pending,
        )


async def _drive_fill(
    *,
    novel_id: int,
    recipe: BaseRecipe,
    cookies: str | None,
    recipe_state: dict,
    job_id: int | None,
    cover_url: str | None,
    total: int,
) -> None:
    """Inner fill loop shared by fresh imports and resume.

    Reads pending skeleton rows in chapter_num order, calls
    `recipe.fetch_chapter` for each, commits via `_fill_skeleton_chapter`.
    Checks `novels.import_status` between iterations — if the user
    cancelled (status flipped to 'paused'), the loop exits cleanly with
    partial state intact.
    """
    done_count = 0
    while True:
        # Pull the next BATCH of pending rows. Re-issuing the SELECT
        # between batches lets the cancel check pick up the user's
        # status flip without holding a transaction open for the whole
        # crawl.
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT import_status FROM novels WHERE id = ?",
                (novel_id,),
            )
            row = await cur.fetchone()
            if row is None or row["import_status"] != "in_progress":
                logger.info(
                    "import_runner: novel %d import_status=%r — "
                    "exiting fill loop",
                    novel_id,
                    row["import_status"] if row else "<deleted>",
                )
                return
            cur = await conn.execute(
                "SELECT id, chapter_num, title_zh, import_source_url "
                "FROM chapters WHERE novel_id = ? "
                "AND import_fetched_at IS NULL "
                "AND import_source_url IS NOT NULL "
                "ORDER BY chapter_num "
                "LIMIT 25",
                (novel_id,),
            )
            batch = await cur.fetchall()
        if not batch:
            # Nothing left to fetch — mark done.
            async with open_conn() as conn:
                await _set_novel_import_status(conn, novel_id, "done")
            if cover_url:
                await _fetch_and_store_cover(novel_id, cover_url, cookies)
            return
        for ch_row in batch:
            planned = PlannedChapterRef(
                chapter_num=ch_row["chapter_num"],
                title_zh=ch_row["title_zh"],
                source_url=ch_row["import_source_url"],
            )
            try:
                fetched = await recipe.fetch_chapter(
                    planned,
                    cookies=cookies,
                    fetch=fetch_one,
                    recipe_state=recipe_state,
                )
            except ScrapeError as e:
                # Per-chapter failure → mark the novel paused and stop.
                # User can hit Resume to retry. Keeping it as 'paused'
                # (not 'error') is honest: the partial novel is still
                # usable; the import just isn't complete.
                logger.warning(
                    "import_runner: novel %d chapter %d failed: %s — "
                    "pausing import",
                    novel_id, ch_row["chapter_num"], e,
                )
                async with open_conn() as conn:
                    await _set_novel_import_status(conn, novel_id, "paused")
                if job_id is not None:
                    await scrape_jobs.mark_error(
                        job_id, str(e),
                        kind=getattr(e, "error_kind", "unknown"),
                    )
                return
            async with open_conn() as conn:
                filled = await _fill_skeleton_chapter(
                    conn, ch_row["id"],
                    title_zh=fetched.title_zh,
                    original_text=fetched.original_text,
                )
            if filled:
                done_count += 1
                if job_id is not None:
                    await scrape_jobs.update_progress(
                        job_id, "fetching_chapters", done_count, total,
                    )


async def _fetch_and_store_cover(
    novel_id: int, cover_url: str, cookies: str | None,
) -> None:
    """Best-effort post-fill cover fetch. Failures never block import
    completion — same policy as the legacy atomic path."""
    try:
        from backend.services.covers import write_cover_for_novel  # noqa: PLC0415
        status, body, content_type, _enc = await fetch_one(
            cover_url, cookies=cookies,
        )
        if status < 400 and content_type.startswith("image/"):
            async with open_conn() as conn:
                await write_cover_for_novel(
                    conn, novel_id, body, source="url",
                )
                await conn.commit()
    except Exception:
        logger.exception(
            "import_runner: cover fetch failed for novel %d (continuing)",
            novel_id,
        )


# ============================================================
# Cancel / resume (user-initiated).
# ============================================================

async def cancel_import(novel_id: int) -> bool:
    """User-initiated cancel. Flips `import_status` from 'in_progress'
    to 'paused' so the runner's fill loop exits at the next
    between-chapter checkpoint. Partial novel stays in the library.
    Returns True if the status was flipped; False if the novel wasn't
    in_progress in the first place."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "UPDATE novels SET import_status = 'paused' "
            "WHERE id = ? AND import_status = 'in_progress'",
            (novel_id,),
        )
        await conn.commit()
        return (cur.rowcount or 0) > 0


def spawn_resume(novel_id: int) -> None:
    """User-initiated resume. Spawns the runner as a background task;
    the route returns immediately."""
    _spawn(resume_recipe_import(novel_id))


# ============================================================
# Boot recovery.
# ============================================================

async def drain_imports_on_startup() -> None:
    """Called from main.py lifespan. For every novel still in
    `import_status='in_progress'`:
    - If it has pending recipe-skeleton chapters (rows with
      import_source_url set + import_fetched_at NULL), re-spawn the
      runner to finish the fetch.
    - Otherwise (no pending skeleton URLs — implies bulk / EPUB whose
      source is gone), flip to 'paused' so the user sees a paused
      badge instead of an in-flight one that never moves.
    """
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id, title, source_url FROM novels "
            "WHERE import_status = 'in_progress'"
        )
        in_progress = await cur.fetchall()
    if not in_progress:
        return
    logger.info(
        "import_runner: draining %d in-progress imports", len(in_progress),
    )
    for row in in_progress:
        novel_id = row["id"]
        async with open_conn() as conn:
            pending = await _count_pending_skeletons(conn, novel_id)
        if pending == 0:
            # Either finished but status not flipped, or non-recipe import
            # whose chapters were INSERTed directly (no skeleton). For the
            # finished case, flip to done. For the bulk/EPUB partial case
            # (source bytes lost), flip to paused. Distinguishing them:
            # check whether ANY chapter in this novel has import_source_url
            # set — if yes, this was a recipe import that completed; if no,
            # this was a bulk/EPUB import whose work is gone.
            async with open_conn() as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM chapters "
                    "WHERE novel_id = ? AND import_source_url IS NOT NULL "
                    "LIMIT 1",
                    (novel_id,),
                )
                has_recipe_chapters = await cur.fetchone() is not None
            new_status = "done" if has_recipe_chapters else "paused"
            async with open_conn() as conn:
                await _set_novel_import_status(conn, novel_id, new_status)
            logger.info(
                "import_runner: novel %d had no pending chapters; "
                "marked %s", novel_id, new_status,
            )
            continue
        # Has pending recipe skeletons → re-spawn the runner.
        logger.info(
            "import_runner: re-spawning runner for novel %d (%r) — "
            "%d chapters pending",
            novel_id, row["title"], pending,
        )
        _spawn(resume_recipe_import(novel_id))


# ============================================================
# Bulk / structured-file imports.
# ============================================================
# Bulk file upload and EPUB/DOCX/HTML parse don't have a re-fetchable
# source — the browser sent the bytes once, and they live in memory
# only while the request is being handled. To get partial-survives-
# crash behavior we just commit each chapter (or small batch) as it's
# decoded, rather than buffering everything for one atomic insert.
# On crash, the novel ends up in 'paused' state with whatever was
# committed; the user keeps the partial or deletes it.


async def insert_chapters_incrementally(
    title: str,
    decoded_chapters: list[ParsedChapter],
    source_type: str,
    source_url: str | None,
    *,
    genre: str | None,
    source_language: str | None,
    batch_size: int = 50,
) -> int:
    """Bulk / EPUB entry point. Creates the novel row in
    `import_status='in_progress'`, then INSERTs the decoded chapters in
    batches of `batch_size`, committing after each batch. On the final
    batch flips to 'done'.

    Differs from `atomic_create_novel`: that one wraps the whole
    novel + every chapter in ONE transaction; this one creates the
    novel in its own short transaction and then commits chapters in
    chunks. A crash between batches leaves the novel with the
    chapters that did commit + `import_status='in_progress'` — the
    boot drain flips it to 'paused'.
    """
    from backend.services.uploads import (
        _INSERT_BATCH_SIZE,
        _insert_novel_row,
    )

    if batch_size <= 0:
        batch_size = _INSERT_BATCH_SIZE

    # Phase 1: novel row + in_progress flag.
    async with open_conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            novel_id = await _insert_novel_row(
                conn, title, source_type, source_url,
                genre=genre, source_language=source_language,
            )
            await conn.execute(
                "UPDATE novels SET import_status = 'in_progress' WHERE id = ?",
                (novel_id,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    # Phase 2: incremental chapter inserts.
    async with open_conn() as conn:
        batch: list[tuple[int, int, str | None, str]] = []
        for ch in decoded_chapters:
            batch.append((novel_id, ch.chapter_num, ch.title_zh, ch.original_text))
            if len(batch) >= batch_size:
                await conn.executemany(
                    "INSERT INTO chapters "
                    "(novel_id, chapter_num, title_zh, original_text, status, "
                    "import_fetched_at) "
                    "VALUES (?, ?, ?, ?, 'pending', datetime('now'))",
                    batch,
                )
                await conn.commit()
                batch.clear()
        if batch:
            await conn.executemany(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, title_zh, original_text, status, "
                "import_fetched_at) "
                "VALUES (?, ?, ?, ?, 'pending', datetime('now'))",
                batch,
            )
            await conn.commit()
        await _set_novel_import_status(conn, novel_id, "done")
    return novel_id
