"""Free-draft worker: fills chapters.free_draft_text via OPUS-MT.

Independent of the main translator queue. Owns its own ``OPUS_MT_LOCK`` so
free-draft work runs in parallel with LLM translations of *different*
chapters — local CPU NMT doesn't fight API rate limits or subscription
windows. The lock still serializes OPUS-MT calls against each other
because CTranslate2 instances are not thread-safe per-translator.

Triggered by two paths:
    * Reader open (``GET /api/chapters/{id}``) on a ``free_draft_status='none'``
      chapter when the novel's source language has an installed OPUS-MT model.
    * Translate click — the main translate worker drains a free draft first
      so the LLM PEMT pass has it as a reference input (Phase 5).

State machine on ``chapters.free_draft_status``:
    none → pending → in_progress → done   (happy path)
                                 → error  (caught + logged in ``free_draft_error``)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiosqlite

from backend.db import open_conn
from backend.services import global_glossary as global_glossary_svc
from backend.services import opus_mt_models
from backend.services.providers import Provider, load_provider
from backend.services.translators.opus_mt import OpusMTTranslator

logger = logging.getLogger(__name__)

# Process-global lock — independent of the LLM TRANSLATOR_LOCK so free-draft
# work and LLM translation can run concurrently for different chapters.
OPUS_MT_LOCK = asyncio.Lock()

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

    Spawns a free-draft worker only when:
      * the chapter is not already translated (status != 'done');
      * the chapter's free draft hasn't started yet (free_draft_status='none');
      * the novel's source language has an OPUS-MT pair *installed* on disk
        — if the pair isn't installed we silently no-op rather than queue
        and fail on every reader open.

    Caller doesn't care about the result; this is best-effort eager fill.
    """
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT c.status, c.free_draft_status, n.source_language "
            "FROM chapters c JOIN novels n ON n.id = c.novel_id "
            "WHERE c.id = ? AND c.novel_id = ?",
            (chapter_id, novel_id),
        )
        row = await cur.fetchone()
    if row is None:
        return False
    if row["status"] == "done":
        return False
    if row["free_draft_status"] != "none":
        return False
    pair = opus_mt_models.pair_for_language(row["source_language"] or "zh")
    if pair is None or not opus_mt_models.is_installed(pair):
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

async def _resolve_opus_mt_provider_for_novel(
    conn: aiosqlite.Connection, source_language: str
) -> Provider | None:
    """Find an existing opus_mt Provider row whose model_id matches the
    novel's source language. Returns the first match; users typically only
    have one OPUS-MT provider per pair."""
    pair = opus_mt_models.pair_for_language(source_language)
    if pair is None:
        return None
    cur = await conn.execute(
        "SELECT id FROM providers WHERE provider_type = 'opus_mt' AND model_id = ? "
        "ORDER BY id LIMIT 1",
        (pair,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return await load_provider(row["id"])


async def _run_free_draft(novel_id: int, chapter_id: int) -> None:
    """One free-draft task: acquire the OPUS-MT lock, then do the work.

    Errors here ONLY affect the free-draft column — the LLM translation
    queue is untouched. A free-draft failure means the reader sees no
    pre-translate draft AND the LLM call (Phase 5) runs without the
    reference layer; both are graceful degrades."""
    async with OPUS_MT_LOCK:
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
    """Claim the row, run OPUS-MT, write free_draft_text + completed_at."""
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
    pair = opus_mt_models.pair_for_language(source_language)
    if pair is None:
        await _persist_free_draft_error(
            conn, chapter_id,
            f"no OPUS-MT pair supports source_language={source_language!r}",
        )
        return
    if not opus_mt_models.is_installed(pair):
        await _persist_free_draft_error(
            conn, chapter_id,
            f"OPUS-MT pair {pair!r} not installed — download from Settings.",
        )
        return

    provider = await _resolve_opus_mt_provider_for_novel(conn, source_language)
    if provider is None:
        # Synthesize an ephemeral Provider so the translator class can
        # construct with model_id=<pair>. This lets the reader's eager free
        # draft fill in even when no opus_mt provider row exists yet.
        provider = Provider(
            id=-1,
            name=f"opus_mt-ephemeral-{pair}",
            provider_type="opus_mt",
            base_url=None,
            model_id=pair,
            params={},
            secret_ref=None,
            is_default=False,
            last_tested_at=None,
            created_at="",
            updated_at="",
        )

    try:
        translator = OpusMTTranslator(provider=provider)
        # Glossary: union of per-novel + global, same as the main queue. The
        # OPUS-MT translator only honors locked entries via placeholder
        # substitution; unlocked entries pass through.
        glossary = await global_glossary_svc.list_for_novel_with_globals(
            conn, novel_id,
        )
        t0 = time.perf_counter()
        result = await translator.translate_chapter(
            chapter_zh=row["original_text"],
            title_zh=row["title_zh"],
            glossary=glossary,
            source_language=source_language,
        )
        elapsed = time.perf_counter() - t0
        logger.info(
            "free-draft: chapter %d (%s) translated in %.1fs",
            chapter_id, pair, elapsed,
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
        logger.exception("free-draft: chapter %d OPUS-MT call failed", chapter_id)
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
