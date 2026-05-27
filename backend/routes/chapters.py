import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from backend.db import get_conn
from backend.models import (
    Chapter,
    ChapterSummary,
    EditParagraphRequest,
)
from backend.services import queue as queue_svc
from backend.services.pre_check import chapter_pre_check

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/novels/{novel_id}/chapters")
async def list_chapters(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> list[ChapterSummary]:
    cur = await conn.execute(
        "SELECT chapter_num, title_zh, title_en, status, translate_queued "
        "FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    rows = await cur.fetchall()
    return [
        ChapterSummary(
            chapter_num=r["chapter_num"],
            title_zh=r["title_zh"],
            title_en=r["title_en"],
            status=r["status"],
            translate_queued=bool(r["translate_queued"]),
        )
        for r in rows
    ]


@router.get("/novels/{novel_id}/chapters/{chapter_num}")
async def get_chapter(
    novel_id: int, chapter_num: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> Chapter:
    cur = await conn.execute(
        "SELECT id, novel_id, chapter_num, title_zh, title_en, original_text, "
        "translated_text, status, error_msg, translate_queued, "
        "glossary_merge_error, translation_degraded, "
        "refinement_status, refined_text, refinement_error, refined_at, "
        "refined_by_provider_id, "
        "free_draft_text, free_draft_status, free_draft_error, "
        "free_draft_completed_at, translated_by_provider_id "
        "FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    # On-demand free-draft trigger. When the reader opens a chapter that
    # doesn't have a draft yet AND the novel's language has an installed
    # OPUS-MT pair, kick off the worker so a draft is ready by the time the
    # user clicks Translate (or as a standalone "rough draft" for free-tier
    # users). Best-effort: failures are silent here — the user can always
    # click Translate explicitly. Spawns into the free-draft lane (its own
    # OPUS_MT_LOCK), so this does not delay any in-flight LLM translation.
    try:
        from backend.services import free_draft_queue
        # _spawn (not bare asyncio.create_task) keeps a strong reference
        # in free_draft_queue._background_tasks so the loop can't GC the
        # task before it starts — silently dropping the user's free draft.
        free_draft_queue._spawn(
            free_draft_queue.maybe_queue_for_open_chapter(novel_id, r["id"])
        )
    except Exception:
        pass
    return Chapter(
        id=r["id"],
        novel_id=r["novel_id"],
        chapter_num=r["chapter_num"],
        title_zh=r["title_zh"],
        title_en=r["title_en"],
        original_text=r["original_text"],
        translated_text=r["translated_text"],
        status=r["status"],
        error_msg=r["error_msg"],
        translate_queued=bool(r["translate_queued"]),
        glossary_merge_error=r["glossary_merge_error"],
        translation_degraded=bool(r["translation_degraded"]),
        refinement_status=r["refinement_status"] or "none",
        refined_text=r["refined_text"],
        refinement_error=r["refinement_error"],
        refined_at=r["refined_at"],
        refined_by_provider_id=r["refined_by_provider_id"],
        free_draft_text=r["free_draft_text"],
        free_draft_status=r["free_draft_status"] or "none",
        free_draft_error=r["free_draft_error"],
        free_draft_completed_at=r["free_draft_completed_at"],
        translated_by_provider_id=r["translated_by_provider_id"],
    )


@router.get("/novels/{novel_id}/search")
async def search_chapters(
    novel_id: int,
    q: str,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Full-text search across this novel's chapters via FTS5."""
    query = (q or "").strip()
    if not query:
        return {"matches": []}
    sql = """
        SELECT c.chapter_num, c.title_en, c.title_zh, c.status,
               snippet(chapter_fts, 2, '<mark>', '</mark>', '…', 18) AS snippet
        FROM chapter_fts
        JOIN chapters c ON c.id = chapter_fts.rowid
        WHERE c.novel_id = ? AND chapter_fts MATCH ?
        ORDER BY c.chapter_num
        LIMIT 200
    """
    rows = None
    try:
        cur = await conn.execute(sql, (novel_id, query))
        rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        try:
            safe = query.replace('"', '""')
            cur = await conn.execute(sql, (novel_id, f'"{safe}"'))
            rows = await cur.fetchall()
        except aiosqlite.OperationalError as e2:
            raise HTTPException(status_code=503, detail=f"search unavailable: {e2}")
    return {
        "matches": [
            {
                "chapter_num": r["chapter_num"],
                "title_en": r["title_en"],
                "title_zh": r["title_zh"],
                "status": r["status"],
                "snippet": r["snippet"],
            }
            for r in rows
        ],
    }


@router.get("/novels/{novel_id}/chapters/{chapter_num}/saturation")
async def chapter_saturation(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Pre-flight checks for a single chapter: glossary-candidate CN runs and
    OCR-issue heuristics. Cheap, no LLM call."""
    from backend.services import glossary as glossary_svc
    from backend.services.parser import detect_ocr_issues
    cur = await conn.execute(
        "SELECT original_text FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    entries = await glossary_svc.list_for_novel(conn, novel_id)
    existing_zh = {e.term_zh for e in entries if e.term_zh}
    candidates = glossary_svc.detect_candidate_terms(r["original_text"], existing_zh)
    ocr_issues = detect_ocr_issues(r["original_text"])
    return {
        "candidates": candidates,
        "glossary_size": len(entries),
        "ocr_issues": ocr_issues,
    }


@router.post("/novels/{novel_id}/chapters/{chapter_num}/retranslate")
async def retranslate_chapter(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Queue a chapter for translation. Works on pending, done, or errored
    rows. Clears banner state so a re-translation never shows stale quality
    flags under it. Refuses (409) when the row is currently being processed."""
    cur = await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    reset_ids = await queue_svc.reset_chapters_for_retranslate(
        conn, novel_id, [r["id"]]
    )
    if not reset_ids:
        cur = await conn.execute(
            "SELECT status FROM chapters WHERE id = ?",
            (r["id"],),
        )
        post = await cur.fetchone()
        if post and post["status"] == "translating":
            detail = "chapter is currently being translated — wait for it to finish, then retry."
        else:
            detail = "chapter could not be re-queued (concurrent state change)."
        raise HTTPException(status_code=409, detail=detail)
    queue_svc.spawn_translate_worker(novel_id, reset_ids[0])
    return {"status": "queued"}


@router.post("/novels/{novel_id}/chapters/{chapter_num}/retry-refinement")
async def retry_refinement(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Re-queue a chapter for its refinement pass.

    Useful when refinement_status='error' and the user wants to retry
    without re-running the translator. Refuses (409) when the chapter has
    no draft yet (status != 'done'), when refinement is already pending /
    in-progress, or when the novel has no refinement_provider_id.
    """
    cur = await conn.execute(
        "SELECT id, status, refinement_status FROM chapters "
        "WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if r["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail="chapter must be translated (status='done') before refinement can retry.",
        )
    cur = await conn.execute(
        "SELECT refinement_provider_id FROM novels WHERE id = ?",
        (novel_id,),
    )
    novel_row = await cur.fetchone()
    if novel_row is None or novel_row["refinement_provider_id"] is None:
        raise HTTPException(
            status_code=409,
            detail="this novel has no refinement provider configured. "
            "Set one in the per-novel settings on the library page, then retry.",
        )
    current = r["refinement_status"]
    if current in ("pending", "in_progress"):
        raise HTTPException(
            status_code=409,
            detail=f"refinement already {current}; nothing to retry.",
        )
    # 'none' / 'done' / 'error' all allowed — flip back to 'pending' and let
    # the worker re-run. Clears refined_text + refinement_error so the
    # previous outcome doesn't show through during the retry window.
    await conn.execute(
        "UPDATE chapters SET refinement_status = 'pending', "
        "refined_text = NULL, refinement_error = NULL, refined_at = NULL "
        "WHERE id = ?",
        (r["id"],),
    )
    await conn.commit()
    queue_svc.spawn_refine_worker(novel_id, r["id"])
    return {"status": "queued"}


@router.post("/novels/{novel_id}/chapters/{chapter_num}/refresh-free-draft")
async def refresh_free_draft(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Clear this chapter's existing OPUS-MT free draft and re-queue
    generation.

    Useful when the OPUS-MT model output is broken (placeholder tokens,
    repeated phrases, garbage) and the user has installed a working model.
    Without this route, the stuck `free_draft_text` would otherwise pollute
    every retranslate via the PEMT reference block — there is no other
    path to overwrite it short of a direct SQL UPDATE.

    Refuses (409) when a free-draft worker is already in flight or when
    the novel's source language has no OPUS-MT pair installed (resetting
    would just leave the row stuck at 'pending' with an unrunnable worker).
    """
    cur = await conn.execute(
        "SELECT c.id, c.free_draft_status, n.source_language "
        "FROM chapters c JOIN novels n ON n.id = c.novel_id "
        "WHERE c.novel_id = ? AND c.chapter_num = ?",
        (novel_id, chapter_num),
    )
    r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if r["free_draft_status"] == "in_progress":
        raise HTTPException(
            status_code=409,
            detail=(
                "free draft is currently being generated — wait for it to "
                "finish, then retry."
            ),
        )
    from backend.services import opus_mt_models  # noqa: PLC0415
    pair = opus_mt_models.pair_for_language(r["source_language"] or "zh")
    if pair is None or not opus_mt_models.is_installed(pair):
        raise HTTPException(
            status_code=409,
            detail=(
                f"no OPUS-MT model installed for source_language="
                f"{r['source_language']!r}. Install one from Settings, then retry."
            ),
        )
    # Clear the body so the reader doesn't display stale garbage during the
    # regeneration window. Status flips to 'none' so queue_free_draft will
    # accept it (its WHERE clause matches 'none' or 'error' only).
    await conn.execute(
        "UPDATE chapters SET free_draft_text = NULL, "
        "free_draft_error = NULL, free_draft_status = 'none', "
        "free_draft_completed_at = NULL "
        "WHERE id = ?",
        (r["id"],),
    )
    await conn.commit()
    from backend.services import free_draft_queue  # noqa: PLC0415
    spawned = await free_draft_queue.queue_free_draft(novel_id, r["id"])
    if not spawned:
        # Concurrent state change between our reset and queue_free_draft.
        # Surface as 409 so the UI can prompt for retry instead of looking
        # silently successful.
        raise HTTPException(
            status_code=409,
            detail="free-draft state changed during refresh — try again.",
        )
    return {"status": "queued"}


@router.delete("/novels/{novel_id}/chapters/{chapter_num}/queue")
async def cancel_chapter_queue(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Remove a chapter from the queue. Clears `translate_queued` so the
    waiting worker task skips the row when it eventually acquires the lock.
    A chapter currently being processed (status='translating') cannot be
    cancelled mid-LLM-call."""
    cur = await conn.execute(
        "SELECT id, status, translate_queued FROM chapters "
        "WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="chapter not found")

    in_flight_translate = r["status"] == "translating"
    cancelled_translate = bool(r["translate_queued"]) and not in_flight_translate

    if cancelled_translate or in_flight_translate:
        await conn.execute(
            "UPDATE chapters SET translate_queued = 0 WHERE id = ?",
            (r["id"],),
        )
        await conn.commit()

    return {
        "cancelled_translate": 1 if cancelled_translate else 0,
        "in_flight_translate": 1 if in_flight_translate else 0,
    }


@router.get("/novels/{novel_id}/chapters/{chapter_num}/pre-check")
async def chapter_pre_check_endpoint(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Pre-translation sanity checks for a chapter. The reader fetches
    this before lighting the Translate button so the user sees length /
    glossary / OCR-shape warnings without paying for an LLM round-trip."""
    cur = await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    warnings = await chapter_pre_check(conn, novel_id, chapter_num)
    return {"warnings": warnings}


@router.post("/novels/{novel_id}/chapters/{chapter_num}/edit-paragraph")
async def edit_paragraph(
    novel_id: int,
    chapter_num: int,
    payload: EditParagraphRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Capture a user paragraph edit. Updates the chapter's body at the
    given paragraph index and records a style_edits row so future translator
    prompts learn from the phrasing.

    `payload.source` selects which body to edit:
      - 'draft' (default) → chapters.translated_text
      - 'refined'         → chapters.refined_text

    The reader picks the source matching which body it currently displays
    (the refined body when refinement_status='done', the draft otherwise).
    style_edits rows look the same regardless of source — they're (before,
    after) pairs of user-preferred phrasing that the translator's prompt
    folds in as future "preferred rewrites" examples.

    Strict equality on `before_md` against the chosen body detects
    concurrent retranslates / refinements (409)."""
    after_text = payload.after_text.strip()
    if not after_text:
        raise HTTPException(status_code=400, detail="after_text must not be whitespace-only")
    if payload.before_md.strip() == after_text:
        return {"ok": True, "noop": True}

    cur = await conn.execute(
        "SELECT id, translated_text, refined_text, refinement_status "
        "FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="chapter not found")

    if payload.source == "refined":
        body = r["refined_text"] or ""
        target_column = "refined_text"
        if not body:
            # The reader's edit form is only visible when refinement_status
            # ='done' AND refined_text is non-empty; reaching this branch
            # means the reader and the DB disagree. 409 (not 400) signals
            # "page is stale" — same retry semantics as the race guard.
            raise HTTPException(
                status_code=409,
                detail=(
                    "chapter has no refined text to edit — refinement may "
                    "have been cleared or never completed. Refresh and retry."
                ),
            )
    else:
        body = r["translated_text"] or ""
        target_column = "translated_text"

    chunks = body.split("\n\n")
    if payload.paragraph_index >= len(chunks):
        raise HTTPException(
            status_code=409,
            detail=(
                f"paragraph_index {payload.paragraph_index} out of range "
                f"(chapter has {len(chunks)} paragraphs) — refresh and retry"
            ),
        )
    if chunks[payload.paragraph_index] != payload.before_md:
        raise HTTPException(
            status_code=409,
            detail="paragraph content has changed since the page loaded — refresh and retry",
        )
    chunks[payload.paragraph_index] = after_text
    new_body = "\n\n".join(chunks)
    # f-string interpolating target_column is safe because it's hard-coded
    # to one of two literal column names above — not user input.
    await conn.execute(
        f"UPDATE chapters SET {target_column} = ? WHERE id = ?",
        (new_body, r["id"]),
    )
    await conn.execute(
        "INSERT INTO style_edits (novel_id, chapter_id, before_text, after_text) "
        "VALUES (?, ?, ?, ?)",
        (novel_id, r["id"], payload.before_md, after_text),
    )
    # F26 (2026-05-25): re-run observers after the paragraph edit so the
    # QA dashboard stays current. Without this, observations recorded
    # at the original commit linger and reference paragraphs the user
    # has already fixed — stale noise. Best-effort: failure to re-run
    # leaves the old observations in place but does not fail the edit.
    try:
        await _refresh_observations_for_chapter(
            conn, novel_id, r["id"], new_body,
        )
    except Exception:
        logger.exception(
            "edit_paragraph: failed to refresh observations for chapter %d "
            "(edit committed, observations may be stale)", chapter_num,
        )
    await conn.commit()
    return {"ok": True}


async def _refresh_observations_for_chapter(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_id: int,
    new_body: str,
) -> None:
    """F26 helper: re-run the deterministic observer set against the
    updated body and replace the chapter's observation rows. Mirrors the
    DELETE-then-INSERT pattern in queue.py so the panel sees one
    atomic update, not a phantom empty state between deletes and inserts.

    Reads `novels.disabled_observers` so user-muted kinds aren't
    re-emitted by an edit either."""
    import json as _json  # noqa: PLC0415

    from backend.services import glossary as glossary_svc  # noqa: PLC0415
    from backend.services import global_glossary as global_glossary_svc  # noqa: PLC0415
    from backend.services.observations import (  # noqa: PLC0415
        normalize_observer_outputs,
    )
    from backend.services.queue import _body_correctness_observations  # noqa: PLC0415

    # Get the chapter's original source text + novel's muted observers.
    cur = await conn.execute(
        "SELECT c.original_text, n.disabled_observers "
        "FROM chapters c JOIN novels n ON n.id = c.novel_id "
        "WHERE c.id = ?", (chapter_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return
    glossary = await global_glossary_svc.list_for_novel_with_globals(
        conn, novel_id,
    )
    raw_msgs = list(_body_correctness_observations(
        row["original_text"], new_body, glossary,
    ))
    normalized = list(normalize_observer_outputs(raw_msgs))
    if row["disabled_observers"]:
        try:
            muted = set(_json.loads(row["disabled_observers"]) or [])
            normalized = [o for o in normalized if o.kind not in muted]
        except Exception:
            pass
    await conn.execute(
        "DELETE FROM chapter_observations WHERE chapter_id = ?",
        (chapter_id,),
    )
    if normalized:
        await conn.executemany(
            "INSERT INTO chapter_observations "
            "(chapter_id, kind, severity, paragraph_index, excerpt) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (chapter_id, o.kind, o.severity, o.paragraph_index, o.excerpt)
                for o in normalized
            ],
        )


# F22 (2026-05-25): per-chapter translation attempts log + "show prompt"
# diagnostic. Powers the edit-mode-only "View translation attempts" and
# "View last prompt" panels on the reader.

@router.get("/novels/{novel_id}/chapters/{chapter_num}/attempts")
async def list_chapter_translation_attempts(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> list[dict]:
    """Recent translation attempts for one chapter, newest first. Includes
    parse_error when the envelope failed to parse so the user can see
    WHY the translator fell back to plain text."""
    cur = await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    ch = await cur.fetchone()
    if ch is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    from backend.services.translation_attempts import list_for_chapter  # noqa: PLC0415
    rows = await list_for_chapter(conn, ch["id"])
    return [
        {
            "id": r.id,
            "chapter_id": r.chapter_id,
            "provider_id": r.provider_id,
            "model_id": r.model_id,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "status": r.status,
            "parse_error": r.parse_error,
            "retry_count": r.retry_count,
        }
        for r in rows
    ]


@router.get("/novels/{novel_id}/chapters/{chapter_num}/last-prompt")
async def get_chapter_last_prompt(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Return the most-recent attempt's prompt_snapshot. Power-user
    debug surface — lets you see exactly what the LLM received and
    diagnose mistranslations against the actual context that produced
    them. 404 if no attempts have been recorded for the chapter yet."""
    cur = await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    ch = await cur.fetchone()
    if ch is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    from backend.services.translation_attempts import latest_prompt  # noqa: PLC0415
    snapshot = await latest_prompt(conn, ch["id"])
    return {"prompt": snapshot}
