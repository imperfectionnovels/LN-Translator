"""Free-draft worker: fills chapters.free_draft_text via Google Translate.

Independent of the main translator queue. Owns its own ``FREE_DRAFT_LOCK`` so
free-draft work runs in parallel with LLM translations of *different*
chapters — Google Translate doesn't fight API rate limits or subscription
windows the same way LLM backends do, but the lock still serializes
free-draft calls against each other to keep traffic to Google's public
endpoint modest and reduce throttle risk.

Triggered by two paths:
    * Reader open (``GET /api/chapters/{id}``) on a ``free_draft_status='none'``
      chapter.
    * Translate click — the main translate worker drains a free draft first
      so the LLM PEMT pass has it as a reference input.

State machine on ``chapters.free_draft_status``:
    none → pending → in_progress → done   (happy path)
                                 → error  (caught + logged in ``free_draft_error``)
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiosqlite

from backend.db import open_conn
from backend.services.providers import Provider, load_provider
from backend.services.translators.google_translate_free import (
    GoogleTranslateFreeTranslator,
)

logger = logging.getLogger(__name__)

# Process-global lock — independent of the LLM TRANSLATOR_LOCK so free-draft
# work and LLM translation can run concurrently for different chapters.
FREE_DRAFT_LOCK = asyncio.Lock()

# Strong refs for fire-and-forget worker tasks. Same pattern as queue.py.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def queue_free_draft(novel_id: int, chapter_id: int) -> bool:
    """Mark a chapter's free draft as ``pending`` and spawn a worker.

    Returns True if a worker was spawned (the chapter wasn't already in
    progress / done). Idempotent: a chapter that already has a draft, or
    whose draft is currently in-flight, returns False without spawning.
    """
    async with open_conn() as conn:
        cur = await conn.execute(
            "UPDATE chapters SET free_draft_status = 'pending', free_draft_error = NULL "
            "WHERE id = ? AND novel_id = ? AND free_draft_status IN ('none', 'error')",
            (chapter_id, novel_id),
        )
        await conn.commit()
    if (cur.rowcount or 0) == 0:
        return False
    _spawn(_run_free_draft(novel_id, chapter_id))
    return True


async def maybe_queue_for_open_chapter(novel_id: int, chapter_id: int) -> bool:
    """Convenience trigger for the reader's open-chapter hook.

    Spawns a free-draft worker when the chapter's free draft hasn't been
    successfully filled in yet (status in 'none', 'error'). The reader's
    Polished / Free draft toggle depends on ``chapters.free_draft_text``
    being non-null, so we want the draft to fill in even for chapters that
    are already LLM-translated — the user wants the toggle on those too.
    Previously-errored attempts get a retry on every open, mirroring the
    Refresh free draft button's effect.

    Google Translate has no per-language install step — if the network is
    down, the worker fails cleanly and we surface the error in the UI.
    Caller doesn't care about the result; this is best-effort eager fill.
    """
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT c.free_draft_status "
            "FROM chapters c JOIN novels n ON n.id = c.novel_id "
            "WHERE c.id = ? AND c.novel_id = ?",
            (chapter_id, novel_id),
        )
        row = await cur.fetchone()
    if row is None:
        return False
    if row["free_draft_status"] not in ("none", "error"):
        return False
    return await queue_free_draft(novel_id, chapter_id)


async def drain_on_startup() -> None:
    """Reset any chapters wedged in 'in_progress' back to 'pending' and
    re-spawn workers for everything still 'pending'. Mirrors
    queue.drain_on_startup for the LLM lane. Called from the FastAPI
    lifespan hook on boot."""
    async with open_conn() as conn:
        recovered = await conn.execute(
            "UPDATE chapters SET free_draft_status = 'pending' "
            "WHERE free_draft_status = 'in_progress'"
        )
        await conn.commit()
        if recovered.rowcount:
            logger.info(
                "free-draft drain: %d stuck rows reset in_progress → pending",
                recovered.rowcount,
            )
        cur = await conn.execute(
            "SELECT id, novel_id FROM chapters WHERE free_draft_status = 'pending'"
        )
        rows = await cur.fetchall()
    if not rows:
        return
    logger.info("free-draft drain: %d tasks resumed", len(rows))
    for r in rows:
        _spawn(_run_free_draft(r["novel_id"], r["id"]))


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def _resolve_free_draft_provider(
    conn: aiosqlite.Connection,
) -> Provider | None:
    """Find the first ``google_translate_free`` Provider row. Returns None
    if none exists; the caller synthesizes an ephemeral one (no auth needed)."""
    cur = await conn.execute(
        "SELECT id FROM providers WHERE provider_type = 'google_translate_free' "
        "ORDER BY id LIMIT 1"
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return await load_provider(row["id"])


async def _run_free_draft(novel_id: int, chapter_id: int) -> None:
    """One free-draft task: acquire the free-draft lock, then do the work.

    Errors here ONLY affect the free-draft column — the LLM translation
    queue is untouched. A free-draft failure means the reader sees no
    pre-translate draft AND the LLM call runs without the reference layer;
    both are graceful degrades."""
    async with FREE_DRAFT_LOCK:
        try:
            async with open_conn() as conn:
                await _free_draft_chapter_in_db(conn, novel_id, chapter_id)
        except Exception:
            logger.exception("free-draft worker crashed for ch_id=%d", chapter_id)
            try:
                async with open_conn() as recovery:
                    await recovery.execute(
                        "UPDATE chapters SET "
                        "free_draft_status = 'error', "
                        "free_draft_error = COALESCE(free_draft_error, 'worker crashed') "
                        "WHERE id = ? AND free_draft_status IN ('pending', 'in_progress')",
                        (chapter_id,),
                    )
                    await recovery.commit()
            except Exception:
                logger.exception(
                    "free-draft recovery cleanup also failed for ch_id=%d",
                    chapter_id,
                )


async def _free_draft_chapter_in_db(
    conn: aiosqlite.Connection, novel_id: int, chapter_id: int,
) -> None:
    """Claim the row, run Google Translate, write free_draft_text + completed_at."""
    cur = await conn.execute(
        "SELECT c.original_text, c.title_zh, c.free_draft_status, n.source_language "
        "FROM chapters c JOIN novels n ON n.id = c.novel_id "
        "WHERE c.id = ? AND c.novel_id = ?",
        (chapter_id, novel_id),
    )
    row = await cur.fetchone()
    if row is None:
        return
    if row["free_draft_status"] not in ("pending",):
        return

    claim = await conn.execute(
        "UPDATE chapters SET free_draft_status = 'in_progress' "
        "WHERE id = ? AND novel_id = ? AND free_draft_status = 'pending'",
        (chapter_id, novel_id),
    )
    await conn.commit()
    if (claim.rowcount or 0) == 0:
        return

    source_language = row["source_language"] or "zh"

    provider = await _resolve_free_draft_provider(conn)
    if provider is None:
        # Synthesize an ephemeral Provider so the translator class can
        # construct. This lets the reader's eager free draft fill in even
        # when no google_translate_free provider row exists yet (fresh
        # install, no Settings → Providers walk yet).
        provider = Provider(
            id=-1,
            name="google_translate_free-ephemeral",
            provider_type="google_translate_free",
            base_url=None,
            model_id="google-web",
            params={},
            secret_ref=None,
            is_default=False,
            last_tested_at=None,
            created_at="",
            updated_at="",
        )

    try:
        translator = GoogleTranslateFreeTranslator(provider=provider)
        t0 = time.perf_counter()
        result = await translator.translate_chapter(
            chapter_zh=row["original_text"],
            title_zh=row["title_zh"],
            glossary=[],
            source_language=source_language,
        )
        elapsed = time.perf_counter() - t0
        logger.info(
            "free-draft: chapter %d (%s) translated in %.1fs",
            chapter_id, source_language, elapsed,
        )
        await conn.execute(
            "UPDATE chapters SET "
            "free_draft_text = ?, free_draft_status = 'done', "
            "free_draft_completed_at = datetime('now'), free_draft_error = NULL "
            "WHERE id = ? AND novel_id = ? AND free_draft_status = 'in_progress'",
            (result.translated_text, chapter_id, novel_id),
        )
        await conn.commit()
    except Exception as exc:
        logger.exception("free-draft: chapter %d Google Translate call failed", chapter_id)
        await _persist_free_draft_error(conn, chapter_id, str(exc)[:4000])


async def _persist_free_draft_error(
    conn: aiosqlite.Connection, chapter_id: int, msg: str,
) -> None:
    await conn.execute(
        "UPDATE chapters SET "
        "free_draft_status = 'error', free_draft_error = ? "
        "WHERE id = ? AND free_draft_status IN ('pending', 'in_progress')",
        (msg, chapter_id),
    )
    await conn.commit()
