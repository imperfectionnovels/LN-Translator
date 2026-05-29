import re
from urllib.parse import quote

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse

from backend.db import get_conn, open_conn
from backend.genres import is_known_genre
from backend.models import (
    AddNovelGenreRequest,
    MassQueueRequest,
    Novel,
    NovelGenresResponse,
    NovelUpdate,
    NovelWithProgress,
    ReadingPositionUpdate,
)
from backend.services import genres_novel as genres_novel_svc
from backend.services import providers as providers_svc
from backend.services import queue as queue_svc
from backend.services.covers import resolve_cover_path
from backend.services.epub_export import build_epub
from backend.services.providers import load_provider

router = APIRouter()

_UNSAFE_FILENAME_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]+")
_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def _avoid_reserved(stem: str) -> str:
    return f"_{stem}" if stem.upper() in _WINDOWS_RESERVED else stem


def _safe_filename(name: str, ext: str) -> tuple[str, str]:
    """Return (ascii_fallback, utf8_full) for Content-Disposition."""
    raw = _UNSAFE_FILENAME_CHARS.sub("_", name).strip().strip(".") or "novel"
    raw = raw[:80]
    ascii_safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_") or "novel"
    raw = _avoid_reserved(raw)
    ascii_safe = _avoid_reserved(ascii_safe)
    return f"{ascii_safe}.{ext}", f"{raw}.{ext}"


_NOVEL_BASE_COLS = (
    "n.id, n.title, n.source_type, n.source_url, n.created_at, n.style_note, "
    "n.source_language, n.genre, n.custom_style_brief, "
    "n.translator_provider_id, n.refinement_provider_id, "
    # Initiative 2 metadata. All nullable.
    "n.author, n.original_title, n.synopsis, n.status, "
    "n.cover_image_path, n.cover_source, n.series_name, n.series_index, "
    # 2026-05-25 F11: archive timestamp (NULL = active novel).
    "n.deleted_at, "
    # 2026-05-26 resumable imports: drive the library card badge.
    "n.import_status, "
    # 2026-05-28 durable reading position: reader resume + library
    # "Continue reading" sort, server-side instead of localStorage.
    "n.last_read_chapter_num, n.last_read_at"
)

# Cost aggregate — recorded per-chapter spend summed across the novel.
# The per-token-pricing projection that used to live here was removed when
# user-entered pricing fields were dropped (2026-05-26 catalog redesign).
# 2026-05-26 resumable imports: also surface the count of skeleton chapter
# rows still awaiting fetch, so the library card's "Importing 800 / 1500"
# badge can render without a second SELECT round-trip.
_COST_AGGREGATES_SQL = (
    "COALESCE(SUM(c.cost_usd), 0) AS cost_usd, "
    "SUM(CASE WHEN c.import_source_url IS NOT NULL "
    "         AND c.import_fetched_at IS NULL "
    "         THEN 1 ELSE 0 END) AS import_pending_chapters"
)


def _row_to_novel_kwargs(r: aiosqlite.Row) -> dict:
    keys = r.keys()
    return {
        "id": r["id"],
        "title": r["title"],
        "source_type": r["source_type"],
        "source_url": r["source_url"],
        "created_at": r["created_at"],
        "style_note": r["style_note"],
        "source_language": r["source_language"] or "zh",
        "genre": r["genre"],
        "custom_style_brief": r["custom_style_brief"],
        "translator_provider_id": r["translator_provider_id"],
        "refinement_provider_id": r["refinement_provider_id"],
        # Initiative 2 metadata. _keys() guard absorbs old rows that pre-date
        # the migration in the unlikely case the SELECT lost the column.
        "author": r["author"] if "author" in keys else None,
        "original_title": r["original_title"] if "original_title" in keys else None,
        "synopsis": r["synopsis"] if "synopsis" in keys else None,
        "status": r["status"] if "status" in keys else None,
        "cover_image_path": r["cover_image_path"] if "cover_image_path" in keys else None,
        "cover_source": r["cover_source"] if "cover_source" in keys else None,
        "series_name": r["series_name"] if "series_name" in keys else None,
        "series_index": r["series_index"] if "series_index" in keys else None,
        "deleted_at": r["deleted_at"] if "deleted_at" in keys else None,
        # 2026-05-26 resumable imports.
        "import_status": r["import_status"] if "import_status" in keys else None,
        # 2026-05-28 durable reading position.
        "last_read_chapter_num": (
            r["last_read_chapter_num"] if "last_read_chapter_num" in keys else None
        ),
        "last_read_at": r["last_read_at"] if "last_read_at" in keys else None,
    }


@router.get("")
async def list_novels(
    archived: bool = False,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> list[NovelWithProgress]:
    # F11 (2026-05-25): default list filters out soft-deleted novels.
    # ?archived=1 inverts the predicate to show ONLY archived novels
    # (the Archive tab on the library page).
    archive_predicate = (
        "n.deleted_at IS NOT NULL" if archived else "n.deleted_at IS NULL"
    )
    cur = await conn.execute(
        f"""
        SELECT {_NOVEL_BASE_COLS},
               COUNT(c.id) AS total_chapters,
               SUM(CASE WHEN c.status = 'done' THEN 1 ELSE 0 END) AS done_chapters,
               SUM(CASE WHEN c.translate_queued = 1 THEN 1 ELSE 0 END) AS translate_queue,
               SUM(CASE WHEN c.status = 'translating' THEN 1 ELSE 0 END) AS translating_now,
               MIN(c.chapter_num) AS first_chapter_num,
               {_COST_AGGREGATES_SQL}
        FROM novels n
        LEFT JOIN chapters c ON c.novel_id = n.id
        WHERE {archive_predicate}
        GROUP BY n.id
        ORDER BY n.created_at DESC
        """
    )
    rows = await cur.fetchall()
    out: list[NovelWithProgress] = []
    for r in rows:
        out.append(
            NovelWithProgress(
                **_row_to_novel_kwargs(r),
                total_chapters=r["total_chapters"] or 0,
                done_chapters=r["done_chapters"] or 0,
                translate_queue=r["translate_queue"] or 0,
                queue_chapters=r["translate_queue"] or 0,
                translating_now=r["translating_now"] or 0,
                first_chapter_num=r["first_chapter_num"],
                cost_usd=float(r["cost_usd"] or 0.0),
                import_pending_chapters=int(r["import_pending_chapters"] or 0),
            )
        )
    return out


@router.get("/{novel_id}")
async def get_novel(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> NovelWithProgress:
    cur = await conn.execute(
        f"""
        SELECT {_NOVEL_BASE_COLS},
               COUNT(c.id) AS total_chapters,
               SUM(CASE WHEN c.status = 'done' THEN 1 ELSE 0 END) AS done_chapters,
               SUM(CASE WHEN c.translate_queued = 1 THEN 1 ELSE 0 END) AS translate_queue,
               SUM(CASE WHEN c.status = 'translating' THEN 1 ELSE 0 END) AS translating_now,
               MIN(c.chapter_num) AS first_chapter_num,
               {_COST_AGGREGATES_SQL}
        FROM novels n
        LEFT JOIN chapters c ON c.novel_id = n.id
        WHERE n.id = ?
        GROUP BY n.id
        """,
        (novel_id,),
    )
    r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="novel not found")
    return NovelWithProgress(
        **_row_to_novel_kwargs(r),
        total_chapters=r["total_chapters"] or 0,
        done_chapters=r["done_chapters"] or 0,
        translate_queue=r["translate_queue"] or 0,
        queue_chapters=r["translate_queue"] or 0,
        translating_now=r["translating_now"] or 0,
        first_chapter_num=r["first_chapter_num"],
        cost_usd=float(r["cost_usd"] or 0.0),
    )


@router.patch("/{novel_id}")
async def update_novel(
    novel_id: int,
    body: NovelUpdate,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> Novel:
    """Partial update. Every body field is optional; only fields the client
    explicitly sets are updated (Pydantic v2 `model_fields_set` /
    `exclude_unset`). `None` is treated as "clear this field" for nullable
    columns. Empty body → 400.
    """
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    sets: list[str] = []
    params: list[object] = []
    if "title" in updates:
        new_title = (updates["title"] or "").strip()
        if not new_title:
            raise HTTPException(status_code=400, detail="title cannot be blank")
        sets.append("title = ?")
        params.append(new_title)
    if "style_note" in updates:
        sets.append("style_note = ?")
        params.append(updates["style_note"])
    if "source_language" in updates:
        lang = (updates["source_language"] or "zh").strip().lower()
        if not lang:
            raise HTTPException(status_code=400, detail="source_language cannot be blank")
        sets.append("source_language = ?")
        params.append(lang)
    if "genre" in updates:
        genre = updates["genre"]
        if genre is not None:
            genre = genre.strip().lower() or None
            if genre is not None and not is_known_genre(genre):
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown genre {genre!r}; see backend/genres.py for valid keys",
                )
        sets.append("genre = ?")
        params.append(genre)
    if "custom_style_brief" in updates:
        brief = updates["custom_style_brief"]
        # Treat blank string as NULL so the prompt falls back to genre overlay.
        if brief is not None and not brief.strip():
            brief = None
        sets.append("custom_style_brief = ?")
        params.append(brief)
    if "translator_provider_id" in updates:
        pid = updates["translator_provider_id"]
        if pid is not None:
            provider = await load_provider(pid)
            if provider is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"translator_provider_id {pid} does not exist",
                )
        sets.append("translator_provider_id = ?")
        params.append(pid)
    if "refinement_provider_id" in updates:
        pid = updates["refinement_provider_id"]
        if pid is not None:
            provider = await load_provider(pid)
            if provider is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"refinement_provider_id {pid} does not exist",
                )
        sets.append("refinement_provider_id = ?")
        params.append(pid)
    # Initiative 2 metadata. Each accepts None to CLEAR. Empty strings on
    # text fields are normalized to None so SELECTs and UI use NULL as the
    # single "absent" representation instead of an ambiguous "" vs NULL.
    for column in (
        "author", "original_title", "synopsis", "status", "series_name",
    ):
        if column in updates:
            value = updates[column]
            if isinstance(value, str):
                value = value.strip() or None
            sets.append(f"{column} = ?")
            params.append(value)
    if "series_index" in updates:
        sets.append("series_index = ?")
        params.append(updates["series_index"])

    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")

    params.append(novel_id)
    cur = await conn.execute(
        f"UPDATE novels SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    await conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="novel not found")
    cur = await conn.execute(
        "SELECT id, title, source_type, source_url, created_at, style_note, "
        "source_language, genre, custom_style_brief, "
        "translator_provider_id, refinement_provider_id, "
        "author, original_title, synopsis, status, cover_image_path, "
        "cover_source, series_name, series_index "
        "FROM novels WHERE id = ?",
        (novel_id,),
    )
    r = await cur.fetchone()
    return Novel(**_row_to_novel_kwargs(r))


@router.put("/{novel_id}/reading-position")
async def set_reading_position(
    novel_id: int,
    body: ReadingPositionUpdate,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Record the chapter the reader is on so reopening the app resumes there.

    Thin, idempotent, single-UPDATE. The reader fires this (debounced) on every
    chapter open; keeping it off the general PATCH path avoids running the
    high-frequency position write through title/genre/provider validation.
    `last_read_at` is stamped server-side to drive the library "Continue
    reading" sort. We do not verify the chapter exists (see
    ReadingPositionUpdate). Returns a small JSON body rather than 204 so the
    frontend's apiFetch (which always calls res.json()) can consume it.
    """
    cur = await conn.execute(
        "UPDATE novels SET last_read_chapter_num = ?, "
        "last_read_at = datetime('now') WHERE id = ?",
        (body.chapter_num, novel_id),
    )
    await conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="novel not found")
    return {"novel_id": novel_id, "last_read_chapter_num": body.chapter_num}


@router.get("/{novel_id}/cost-estimate")
async def novel_cost_estimate(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Cost projection is gone — user-entered pricing was removed in the
    2026-05-26 catalog redesign. The endpoint stays for API stability so
    older frontends don't 404, but it always returns confidence=no_pricing
    with a null estimate.
    """
    cur = await conn.execute("SELECT id FROM novels WHERE id = ?", (novel_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="novel not found")
    return {
        "novel_id": novel_id,
        "confidence": "no_pricing",
        "estimated_cost_usd": None,
    }


@router.get("/{novel_id}/cost")
async def novel_cost(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Per-novel cost summary. Sums the cost_usd / token columns across
    every chapter that recorded usage. Backends that don't expose tokens
    (older claude_cli, cache hits) contribute NULL columns which COALESCE
    to 0 in the aggregate — the totals are "what we have records for",
    not "the universe of calls", so the user reads them as a floor."""
    cur = await conn.execute("SELECT id FROM novels WHERE id = ?", (novel_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="novel not found")
    cur = await conn.execute(
        "SELECT "
        "  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "  COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens, "
        "  COALESCE(SUM(cost_usd), 0) AS cost_usd, "
        "  COUNT(cost_usd) AS chapters_with_cost "
        "FROM chapters WHERE novel_id = ?",
        (novel_id,),
    )
    row = await cur.fetchone()
    return {
        "novel_id": novel_id,
        "input_tokens": int(row["input_tokens"] or 0),
        "output_tokens": int(row["output_tokens"] or 0),
        "cached_input_tokens": int(row["cached_input_tokens"] or 0),
        "cost_usd": float(row["cost_usd"] or 0.0),
        "chapters_with_cost": int(row["chapters_with_cost"] or 0),
    }


@router.delete("/queue/all")
async def cancel_global_queue(
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Drain every novel's translate queue. Doesn't interrupt the in-flight
    worker — the chapter mid-LLM continues and clears its flag normally."""
    cur = await conn.execute(
        "SELECT "
        " SUM(CASE WHEN translate_queued = 1 AND status != 'translating' THEN 1 ELSE 0 END) AS t_cancel, "
        " SUM(CASE WHEN status = 'translating' THEN 1 ELSE 0 END) AS t_inflight "
        "FROM chapters"
    )
    counts = await cur.fetchone()
    await conn.execute(
        "UPDATE chapters SET translate_queued = 0 "
        "WHERE translate_queued = 1 AND status != 'translating'"
    )
    await conn.commit()
    return {
        "cancelled_translate": counts["t_cancel"] or 0,
        "in_flight_translate": counts["t_inflight"] or 0,
    }


@router.get("/queue/all")
async def list_global_queue(
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Cross-novel translate queue snapshot for the shared queue panel.

    Returns `translate` (queued + in-flight chapters) and `recent` (last
    20 chapters by status transition — done or error — for the Phase E2
    Queue Stack page's right column). The shared queue-panel pill ignores
    `recent`; only the dedicated /queue page reads it.
    """
    cur = await conn.execute(
        """
        SELECT c.id AS chapter_id, c.novel_id, c.chapter_num,
               c.title_en, c.title_zh, c.status,
               c.translate_queued,
               n.title AS novel_title
        FROM chapters c
        JOIN novels n ON n.id = c.novel_id
        WHERE c.translate_queued = 1
        ORDER BY c.novel_id, c.chapter_num
        """
    )
    rows = await cur.fetchall()
    translate: list[dict] = []
    for r in rows:
        translate.append({
            "chapter_id": r["chapter_id"],
            "novel_id": r["novel_id"],
            "novel_title": r["novel_title"],
            "chapter_num": r["chapter_num"],
            "title": r["title_en"] or r["title_zh"] or f"Chapter {r['chapter_num']}",
            "in_flight": r["status"] == "translating",
        })
    # Recent: last 20 'done' or 'error' chapters by translated_at desc.
    # NULL translated_at (pre-Initiative-6 rows) sorts last so historical
    # data doesn't crowd out current activity. The queue page renders
    # these as compact rows with Read / Retry actions per status.
    cur = await conn.execute(
        """
        SELECT c.id AS chapter_id, c.novel_id, c.chapter_num,
               c.title_en, c.title_zh, c.status, c.error_msg,
               c.translated_at,
               n.title AS novel_title
        FROM chapters c
        JOIN novels n ON n.id = c.novel_id
        WHERE c.status IN ('done', 'error')
        ORDER BY (c.translated_at IS NULL), c.translated_at DESC, c.id DESC
        LIMIT 20
        """
    )
    rrows = await cur.fetchall()
    recent: list[dict] = []
    for r in rrows:
        recent.append({
            "chapter_id": r["chapter_id"],
            "novel_id": r["novel_id"],
            "novel_title": r["novel_title"],
            "chapter_num": r["chapter_num"],
            "title": r["title_en"] or r["title_zh"] or f"Chapter {r['chapter_num']}",
            "status": r["status"],
            "error_msg": r["error_msg"],
            "translated_at": r["translated_at"],
        })
    return {
        "translate": translate,
        "translate_depth": len(translate),
        "recent": recent,
    }


@router.delete("/{novel_id}")
async def delete_novel(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> dict:
    # F11 (2026-05-25): DELETE soft-archives instead of hard-deleting.
    # Returns the quantified counts so the UI's confirm dialog can show
    # exactly what was archived ("247 chapters, 89 glossary, $12.40").
    # Hard delete is the separate POST /api/novels/{id}/purge endpoint
    # below; purge requires that the novel was archived first.
    from backend.services.soft_delete import archive_novel  # noqa: PLC0415
    counts = await archive_novel(conn, novel_id)
    return {
        "archived": novel_id,
        "chapters": counts.chapters,
        "glossary_entries": counts.glossary_entries,
        "bookmarks": counts.bookmarks,
        "chapter_observations": counts.chapter_observations,
        "tm_segments": counts.tm_segments,
        "fr_snapshots": counts.fr_snapshots,
        "total_cost_usd": counts.total_cost_usd,
    }


@router.post("/{novel_id}/restore")
async def restore_archived_novel(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> dict:
    """Restore an archived novel back to the active library."""
    from backend.services.soft_delete import restore_novel  # noqa: PLC0415
    await restore_novel(conn, novel_id)
    return {"restored": novel_id}


@router.delete("/{novel_id}/purge")
async def purge_archived_novel(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> dict:
    """Hard delete — only allowed on already-archived novels. Fires
    CASCADE on chapters/glossary/etc. The novel must be in the Archive
    tab first (409 otherwise) — this is the safety rail."""
    from backend.services.soft_delete import purge_novel  # noqa: PLC0415
    counts = await purge_novel(conn, novel_id)
    return {
        "purged": novel_id,
        "chapters": counts.chapters,
        "glossary_entries": counts.glossary_entries,
        "bookmarks": counts.bookmarks,
        "chapter_observations": counts.chapter_observations,
        "tm_segments": counts.tm_segments,
        "fr_snapshots": counts.fr_snapshots,
        "total_cost_usd": counts.total_cost_usd,
    }


@router.get("/{novel_id}/delete-counts")
async def get_delete_counts(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> dict:
    """Preview the cost of archiving / purging a novel. Used by the
    quantified delete-confirm dialog: lets the UI show counts BEFORE
    the user clicks the destructive button."""
    from backend.services.soft_delete import delete_counts  # noqa: PLC0415
    counts = await delete_counts(conn, novel_id)
    return {
        "novel_id": counts.novel_id,
        "chapters": counts.chapters,
        "glossary_entries": counts.glossary_entries,
        "bookmarks": counts.bookmarks,
        "chapter_observations": counts.chapter_observations,
        "tm_segments": counts.tm_segments,
        "fr_snapshots": counts.fr_snapshots,
        "total_cost_usd": counts.total_cost_usd,
    }


@router.post("/{novel_id}/queue")
async def mass_queue_chapters(
    novel_id: int,
    body: MassQueueRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Queue many chapters for translation in one call.

    Routes pending chapters through queue_translations (just flips the flag
    and spawns a worker, no force_retranslate). Errored chapters take the
    reset path so the worker can claim them again (status='error' isn't
    claimable directly). Already-queued / in-flight / done chapters are
    skipped and counted, so the UI can surface what actually happened.
    """
    cur = await conn.execute("SELECT id FROM novels WHERE id = ?", (novel_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="novel not found")

    if body.mode == "range":
        if body.from_chapter is None or body.to_chapter is None:
            raise HTTPException(
                status_code=400,
                detail="from_chapter and to_chapter are required for range mode",
            )
        if body.from_chapter > body.to_chapter:
            raise HTTPException(
                status_code=400,
                detail="from_chapter must be less than or equal to to_chapter",
            )
        cur = await conn.execute(
            "SELECT id, chapter_num, status, translate_queued FROM chapters "
            "WHERE novel_id = ? AND chapter_num >= ? AND chapter_num <= ? "
            "ORDER BY chapter_num",
            (novel_id, body.from_chapter, body.to_chapter),
        )
    else:
        cur = await conn.execute(
            "SELECT id, chapter_num, status, translate_queued FROM chapters "
            "WHERE novel_id = ? ORDER BY chapter_num",
            (novel_id,),
        )
    rows = await cur.fetchall()

    pending_ids: list[int] = []
    error_ids: list[int] = []
    skipped_done = 0
    skipped_in_flight = 0
    skipped_already_queued = 0
    skipped_errors = 0

    for r in rows:
        status = r["status"]
        if status == "translating":
            skipped_in_flight += 1
            continue
        if status == "done":
            skipped_done += 1
            continue
        if r["translate_queued"]:
            skipped_already_queued += 1
            continue
        if status == "error":
            if body.include_errors:
                error_ids.append(r["id"])
            else:
                skipped_errors += 1
            continue
        pending_ids.append(r["id"])

    queued_count = 0
    if pending_ids:
        await queue_svc.queue_translations(novel_id, pending_ids)
        queued_count += len(pending_ids)
    if error_ids:
        reset_ids = await queue_svc.reset_chapters_for_retranslate(
            conn, novel_id, error_ids
        )
        for cid in reset_ids:
            queue_svc.spawn_translate_worker(novel_id, cid)
        queued_count += len(reset_ids)

    return {
        "queued_count": queued_count,
        "skipped_done": skipped_done,
        "skipped_in_flight": skipped_in_flight,
        "skipped_already_queued": skipped_already_queued,
        "skipped_errors": skipped_errors,
    }


@router.delete("/{novel_id}/queue")
async def cancel_novel_queue(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> dict:
    """Remove every queued chapter from this novel's translate queue."""
    cur = await conn.execute("SELECT id FROM novels WHERE id = ?", (novel_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="novel not found")

    cur = await conn.execute(
        "SELECT "
        " SUM(CASE WHEN translate_queued = 1 AND status != 'translating' THEN 1 ELSE 0 END) AS t_cancel, "
        " SUM(CASE WHEN status = 'translating' THEN 1 ELSE 0 END) AS t_inflight "
        "FROM chapters WHERE novel_id = ?",
        (novel_id,),
    )
    counts = await cur.fetchone()

    await conn.execute(
        "UPDATE chapters SET translate_queued = 0 "
        "WHERE novel_id = ? AND translate_queued = 1 AND status != 'translating'",
        (novel_id,),
    )
    await conn.commit()

    return {
        "cancelled_translate": counts["t_cancel"] or 0,
        "in_flight_translate": counts["t_inflight"] or 0,
    }


@router.get("/{novel_id}/download")
async def download_novel(
    novel_id: int,
    format: str = "txt",
    skip_untranslated: bool = False,
    conn: aiosqlite.Connection = Depends(get_conn),
):
    """Stream the novel in one of three formats:
      - txt: plain UTF-8 with title bars / chapter rules.
      - md: markdown with h1/h2.
      - epub: EPUB 3 archive built via ebooklib, source-of-truth =
        COALESCE(refined_text, translated_text), embeds the uploaded
        cover when set.
    """
    if format not in ("txt", "md", "epub"):
        raise HTTPException(
            status_code=400, detail="unsupported format (use txt, md, or epub)"
        )
    cur = await conn.execute(
        "SELECT id, title, author, synopsis, cover_image_path FROM novels WHERE id = ?",
        (novel_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="novel not found")
    novel_title = row["title"]

    if format == "epub":
        # EPUB requires the full chapter list in memory (ebooklib's writer
        # isn't async + streaming friendly). For the desktop use case this
        # is acceptable — a 600-chapter novel fits comfortably in RAM.
        cur = await conn.execute(
            "SELECT chapter_num, title_en, title_zh, translated_text, "
            "refined_text, refinement_status, status "
            "FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
            (novel_id,),
        )
        chapter_rows = [dict(r) for r in await cur.fetchall()]
        novel_dict = {
            "id": row["id"],
            "title": novel_title,
            "author": row["author"],
            "synopsis": row["synopsis"],
        }
        cover_pair = None
        cover_path = resolve_cover_path(row["cover_image_path"])
        if cover_path is not None:
            try:
                cover_pair = (cover_path.read_bytes(), cover_path.suffix)
            except OSError as e:
                # Missing or unreadable cover — log and proceed without it.
                # Failing the whole export over a stale cover row is worse
                # than emitting a cover-less EPUB.
                import logging
                logging.getLogger(__name__).warning(
                    "could not read cover for novel %d: %s", novel_id, e,
                )
        try:
            data = build_epub(novel_dict, chapter_rows, cover_pair)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        ascii_name, utf8_name = _safe_filename(novel_title, "epub")
        disposition = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(utf8_name, safe='')}"
        )
        return Response(
            content=data,
            media_type="application/epub+zip",
            headers={"Content-Disposition": disposition},
        )

    async def stream():
        async with open_conn() as sconn:
            if format == "md":
                yield f"# {novel_title}\n".encode("utf-8")
            else:
                bar = "=" * max(3, len(novel_title))
                yield f"{novel_title}\n{bar}\n".encode("utf-8")
            cur = await sconn.execute(
                "SELECT chapter_num, title_en, title_zh, translated_text, "
                "refined_text, refinement_status, status "
                "FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
                (novel_id,),
            )
            async for ch in cur:
                body = None
                if ch["status"] == "done":
                    # Prefer refined text when refinement landed. Mirrors
                    # the reader's _displayedEnglish: refined IS the
                    # canonical English once a successful refinement exists.
                    if ch["refinement_status"] == "done" and ch["refined_text"]:
                        body = ch["refined_text"]
                    elif ch["translated_text"]:
                        body = ch["translated_text"]
                if not body and skip_untranslated:
                    continue
                title = (
                    ch["title_en"] or ch["title_zh"] or f"Chapter {ch['chapter_num']}"
                )
                if format == "md":
                    yield f"\n## {title}\n\n".encode("utf-8")
                else:
                    sub_bar = "-" * max(3, len(title))
                    yield f"\n{title}\n{sub_bar}\n\n".encode("utf-8")
                if body:
                    yield body.encode("utf-8")
                else:
                    yield f"[Chapter not translated — status: {ch['status']}]".encode("utf-8")
                yield b"\n"

    ascii_name, utf8_name = _safe_filename(novel_title, format)
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(utf8_name, safe='')}"
    )
    media_type = "text/markdown" if format == "md" else "text/plain"
    return StreamingResponse(
        stream(),
        media_type=f"{media_type}; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


# ----- Per-novel genre tags -------------------------------------------------
#
# The PRIMARY genre lives on novels.genre (existing column; drives the prompt
# overlay via resolve_genre). Secondary tags live in the novel_genres table.
# These endpoints power the genre chips on the novel overview page.

def _genres_response(ng: genres_novel_svc.NovelGenres) -> NovelGenresResponse:
    return NovelGenresResponse(
        primary=ng.primary, secondary=ng.secondary, all_keys=ng.all_keys,
    )


@router.get("/{novel_id}/genres")
async def get_novel_genres(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> NovelGenresResponse:
    return _genres_response(
        await genres_novel_svc.list_novel_genres(conn, novel_id)
    )


@router.post("/{novel_id}/genres")
async def add_novel_genre(
    novel_id: int,
    body: AddNovelGenreRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> NovelGenresResponse:
    if body.is_primary:
        return _genres_response(
            await genres_novel_svc.set_primary_genre(
                conn, novel_id, body.genre_key,
            )
        )
    return _genres_response(
        await genres_novel_svc.add_secondary_genre(
            conn, novel_id, body.genre_key,
        )
    )


@router.delete("/{novel_id}/genres/{genre_key}")
async def remove_novel_genre(
    novel_id: int,
    genre_key: str,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> NovelGenresResponse:
    return _genres_response(
        await genres_novel_svc.remove_secondary_genre(
            conn, novel_id, genre_key,
        )
    )


@router.put("/{novel_id}/genres/{genre_key}/primary")
async def set_novel_primary_genre(
    novel_id: int,
    genre_key: str,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> NovelGenresResponse:
    return _genres_response(
        await genres_novel_svc.set_primary_genre(conn, novel_id, genre_key)
    )
