from __future__ import annotations

import csv
import io
import re

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.db import get_conn
from backend.models import ChapterSummary, GlossaryEntry, GlossaryUpdate, NewGlossaryEntry
from backend.services import find_replace as fr
from backend.services import glossary as glossary_svc
from backend.services import queue as queue_svc

router = APIRouter()


@router.get("/novels/{novel_id}/glossary")
async def list_glossary(
    novel_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> list[GlossaryEntry]:
    return await glossary_svc.list_for_novel(conn, novel_id)


def _safe_filename(s: str) -> str:
    """Strip filesystem-unfriendly chars and keep the name short. Used in the
    Content-Disposition header; the file name is purely cosmetic and the user
    can rename on save."""
    cleaned = re.sub(r"[^\w\-]+", "_", (s or "glossary").strip())
    return cleaned[:60] or "glossary"


@router.get("/novels/{novel_id}/glossary/export")
async def export_glossary(
    novel_id: int,
    format: str = Query("csv", pattern="^(csv|md)$"),
    conn: aiosqlite.Connection = Depends(get_conn),
) -> StreamingResponse:
    """Stream the novel's glossary as CSV or Markdown. Mirrors the chapter
    download endpoint's pattern (StreamingResponse with a filename) so the
    browser triggers a save dialog."""
    cur = await conn.execute("SELECT title FROM novels WHERE id = ?", (novel_id,))
    novel = await cur.fetchone()
    if novel is None:
        raise HTTPException(status_code=404, detail="novel not found")
    entries = await glossary_svc.list_for_novel(conn, novel_id)
    base = _safe_filename(novel["title"])

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["term_zh", "term_en", "category", "locked", "auto_detected", "notes"])
        for e in entries:
            writer.writerow([
                e.term_zh, e.term_en, e.category,
                "1" if e.locked else "0",
                "1" if e.auto_detected else "0",
                e.notes or "",
            ])
        body = buf.getvalue()
        media_type = "text/csv; charset=utf-8"
        filename = f"{base}_glossary.csv"
    else:
        # Markdown: a single table per category. Sorted within category by
        # term_zh so the output is stable and diffable.
        lines: list[str] = [f"# Glossary — {novel['title']}", ""]
        by_cat: dict[str, list[GlossaryEntry]] = {}
        for e in entries:
            by_cat.setdefault(e.category, []).append(e)
        for cat in sorted(by_cat):
            lines.append(f"## {cat}")
            lines.append("")
            lines.append("| 中文 | English | Locked | Notes |")
            lines.append("| --- | --- | --- | --- |")
            for e in sorted(by_cat[cat], key=lambda x: x.term_zh):
                # Escape pipes so they don't break the table.
                notes = (e.notes or "").replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| {e.term_zh} | {e.term_en} | "
                    f"{'✓' if e.locked else ''} | {notes} |"
                )
            lines.append("")
        body = "\n".join(lines)
        media_type = "text/markdown; charset=utf-8"
        filename = f"{base}_glossary.md"

    return StreamingResponse(
        iter([body]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/novels/{novel_id}/glossary", status_code=201)
async def create_glossary_entry(
    novel_id: int,
    payload: NewGlossaryEntry,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> GlossaryEntry:
    try:
        return await glossary_svc.create_or_overwrite_entry(
            conn,
            novel_id=novel_id,
            term_zh=payload.term_zh,
            term_en=payload.term_en,
            category=payload.category,
            notes=payload.notes,
            usage_note=payload.usage_note,
        )
    except glossary_svc.LockedEntryConflict:
        raise HTTPException(
            status_code=409,
            detail=(
                f'Term "{payload.term_zh}" already exists and is locked. '
                "Edit the existing entry instead."
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/glossary/{entry_id}")
async def update_entry(
    entry_id: int,
    payload: GlossaryUpdate,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> GlossaryEntry:
    # `update_entry` raises ValueError on a whitespace-only term_en — surface
    # it as a 400 so the user sees the actual reason, not an opaque 500.
    # Pydantic already rejects outright-empty term_en upstream as a 422.
    try:
        updated = await glossary_svc.update_entry(
            conn,
            entry_id=entry_id,
            term_en=payload.term_en,
            category=payload.category,
            notes=payload.notes,
            usage_note=payload.usage_note,
            locked=payload.locked,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="entry not found")
    return updated


@router.delete("/glossary/{entry_id}")
async def delete_entry(
    entry_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> dict:
    deleted = await glossary_svc.delete_entry(conn, entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="entry not found")
    return {"deleted": entry_id}


# ----- Initiative 4: in-place term substitution -----


class ApplyInPlaceRequest(BaseModel):
    """Body for /api/glossary/{id}/apply-in-place.

    Carries BOTH the old and new English so the route doesn't have to
    guess from server-side state — the client just rewrote `term_en`
    via PATCH, so it knows both halves. Word-boundary substitution
    runs across the entry's novel only (cross-novel rendering for a
    per-novel term doesn't make sense)."""

    old_en: str = Field(min_length=1, max_length=200)
    new_en: str = Field(min_length=1, max_length=200)


class ApplyInPlaceResponse(BaseModel):
    chapters_updated: int
    rows_updated_translated: int
    rows_updated_refined: int
    rows_updated_titles: int = 0


@router.post("/glossary/{entry_id}/apply-in-place")
async def apply_in_place(
    entry_id: int,
    body: ApplyInPlaceRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> ApplyInPlaceResponse:
    """Substitute `old_en` with `new_en` across every chapter of the
    novel that owns this glossary entry. Word-boundary, case-sensitive,
    NO preview gate — the glossary edit dialog has already shown the
    user what they're doing.

    The UI copy must make clear: this updates EXACT matching English text
    only. Chapters where the term was translated inconsistently won't all
    match — for full consistency, use Retranslate instead."""
    entry = await glossary_svc.get_one(conn, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    if entry.novel_id is None:
        raise HTTPException(
            status_code=400,
            detail="this entry has no novel — use the global glossary's apply-in-place",
        )
    result = await fr.apply_in_place_for_glossary_term(
        conn,
        old_en=body.old_en,
        new_en=body.new_en,
        novel_id=entry.novel_id,
    )
    return ApplyInPlaceResponse(
        chapters_updated=result.chapters_updated,
        rows_updated_translated=result.rows_updated_translated,
        rows_updated_refined=result.rows_updated_refined,
        rows_updated_titles=result.rows_updated_titles,
    )


class BulkDeleteRequest(BaseModel):
    """Body for /glossary/bulk-delete. `novel_id` is required so a
    frontend bug can't accidentally delete entries belonging to a
    different novel."""
    novel_id: int
    ids: list[int] = Field(default_factory=list)


@router.post("/glossary/bulk-delete")
async def bulk_delete(
    body: BulkDeleteRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Delete many glossary entries in one request, scoped to one novel.

    The frontend's bulk action otherwise fires N DELETE calls in parallel,
    which the SQLite write lock serializes anyway — folding them into one
    statement is faster and keeps the audit trail clean."""
    if not body.ids:
        return {"deleted": 0}
    placeholders = ",".join("?" * len(body.ids))
    cur = await conn.execute(
        f"DELETE FROM glossary_entries WHERE id IN ({placeholders}) AND novel_id = ?",
        [*body.ids, body.novel_id],
    )
    await conn.commit()
    return {"deleted": cur.rowcount or 0}


@router.get("/novels/{novel_id}/glossary/health")
async def glossary_health(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Glossary health report. Surfaces three classes of issue the user
    should review:

    - duplicate_en: same English term used for multiple Chinese terms (one
      English maps to many sources; reader can't tell which Chinese term a
      given English mention came from)
    - duplicate_zh: shouldn't happen (UNIQUE constraint) but check anyway
    - unused: glossary entry whose `term_zh` doesn't appear in any chapter's
      original_text — usually a stale auto-extracted term from a deleted
      chapter, or a manually-added term for content that hasn't been
      uploaded yet

    Each entry returned includes the full GlossaryEntry payload so the UI
    can render edit links inline."""
    # Single SELECT for the full glossary so we don't fan out per entry.
    entries = await glossary_svc.list_for_novel(conn, novel_id)

    # Duplicate English: group by lower-cased en, keep groups with >1 entry.
    by_en: dict[str, list[GlossaryEntry]] = {}
    for e in entries:
        key = (e.term_en or "").strip().lower()
        if not key:
            continue
        by_en.setdefault(key, []).append(e)
    duplicate_en = [
        {"term_en": v[0].term_en, "entries": [e.model_dump() for e in v]}
        for v in by_en.values() if len(v) > 1
    ]

    # Duplicate Chinese: also lower-cased + stripped for normalization, though
    # the UNIQUE constraint on (novel_id, term_zh) makes this normally empty.
    by_zh: dict[str, list[GlossaryEntry]] = {}
    for e in entries:
        key = (e.term_zh or "").strip()
        if not key:
            continue
        by_zh.setdefault(key, []).append(e)
    duplicate_zh = [
        {"term_zh": v[0].term_zh, "entries": [e.model_dump() for e in v]}
        for v in by_zh.values() if len(v) > 1
    ]

    # Unused: term_zh not contained in any chapter's original_text. One
    # NOT EXISTS subquery per row replaces what was N round-trips (formerly
    # blocked the request for ~500ms on a 500-term glossary). The chapter
    # scan inside the subquery is cheap because INSTR returns at the first
    # hit and there's no LIMIT-induced cursor overhead per row.
    cur = await conn.execute(
        """
        SELECT id, novel_id, term_zh, term_en, category, notes,
               auto_detected, locked
        FROM glossary_entries g
        WHERE g.novel_id = ?
          AND NOT EXISTS (
            SELECT 1 FROM chapters c
            WHERE c.novel_id = g.novel_id
              AND INSTR(c.original_text, g.term_zh) > 0
          )
        """,
        (novel_id,),
    )
    unused_rows = await cur.fetchall()
    unused: list[dict] = [
        {
            "id": r["id"],
            "novel_id": r["novel_id"],
            "term_zh": r["term_zh"],
            "term_en": r["term_en"],
            "category": r["category"],
            "notes": r["notes"],
            "auto_detected": bool(r["auto_detected"]),
            "locked": bool(r["locked"]),
        }
        for r in unused_rows
    ]

    return {
        "duplicate_en": duplicate_en,
        "duplicate_zh": duplicate_zh,
        "unused": unused,
        "total_entries": len(entries),
    }


class BulkLockRequest(BaseModel):
    """Body for /glossary/bulk-lock. novel_id is required so a frontend bug
    can't toggle locks on a different novel."""
    novel_id: int
    ids: list[int] = Field(default_factory=list)
    locked: bool


@router.post("/glossary/bulk-lock")
async def bulk_lock(
    body: BulkLockRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Set `locked` on many glossary entries in one request, scoped to one
    novel."""
    if not body.ids:
        return {"updated": 0}
    placeholders = ",".join("?" * len(body.ids))
    cur = await conn.execute(
        f"UPDATE glossary_entries SET locked = ? WHERE id IN ({placeholders}) AND novel_id = ?",
        [1 if body.locked else 0, *body.ids, body.novel_id],
    )
    await conn.commit()
    return {"updated": cur.rowcount or 0}


@router.get("/glossary/{entry_id}/affected-chapters")
async def affected_chapters(
    entry_id: int, conn: aiosqlite.Connection = Depends(get_conn)
) -> list[ChapterSummary]:
    """Chapters whose original_text contains this term — i.e., chapters that
    would benefit from re-translation if the term's English/category changed."""
    entry = await glossary_svc.get_one(conn, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    rows = await glossary_svc.find_chapters_using_term(
        conn, entry.novel_id, entry.term_zh
    )
    # Populate translate_queued so the UI accurately reflects whether a
    # chapter is already in the queue. Without this, the affected-chapters
    # list reads every row as "not queued" and the user re-queues duplicates.
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


@router.post("/glossary/{entry_id}/retranslate-affected")
async def retranslate_affected(
    entry_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Queue every chapter whose original_text contains this glossary term.

    Chapters that are currently being processed by a worker (status='translating')
    are skipped — overwriting their status
    mid-flight would clobber the worker's success UPDATE and silently drop the
    translation. Skipped chapters are reported back so the UI can surface them.

    The chapters land in the durable queue (translate_queued=1) and the worker
    pool processes them one at a time in chapter_num order. The user sees the
    queue depth grow immediately.

    Reset + flag-set + in-flight-skip happen in a single atomic UPDATE per
    chunk inside reset_chapters_for_retranslate — see that helper's docstring
    for the rationale (closes the worker-race window and the crash-window
    the old two-write design carried, plus chunks for very large affected
    sets so SQLite's parameter cap can't bite on a long xianxia)."""
    entry = await glossary_svc.get_one(conn, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    rows = await glossary_svc.find_chapters_using_term(
        conn, entry.novel_id, entry.term_zh
    )
    if not rows:
        return {"queued_count": 0, "chapter_nums": [], "skipped_in_flight": []}
    all_ids = [r["id"] for r in rows]
    reset_ids = await queue_svc.reset_chapters_for_retranslate(
        conn, entry.novel_id, all_ids
    )
    reset_set = set(reset_ids)
    chapter_nums = [r["chapter_num"] for r in rows if r["id"] in reset_set]
    skipped_in_flight = [r["chapter_num"] for r in rows if r["id"] not in reset_set]
    # The helper already set translate_queued=1 atomically with the reset;
    # spawn the workers directly (do not go through queue_translation, which
    # would issue a redundant second UPDATE+commit and reopen the
    # crash-window the helper closes).
    for cid in reset_ids:
        queue_svc.spawn_translate_worker(entry.novel_id, cid)
    return {
        "queued_count": len(reset_ids),
        "chapter_nums": chapter_nums,
        "skipped_in_flight": skipped_in_flight,
    }


class BulkRetranslateBody(BaseModel):
    """Body for POST /glossary/bulk-retranslate-affected. The entry_ids must
    all belong to the same novel — cross-novel batches are rejected because
    the worker dispatch is per-novel and silently splitting wouldn't
    surface that to the user."""
    entry_ids: list[int] = Field(min_length=1, max_length=200)


@router.post("/glossary/bulk-retranslate-affected")
async def bulk_retranslate_affected(
    body: BulkRetranslateBody,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Design v2 Phase D: batch the per-entry /retranslate-affected so the
    Ledger view's bulk action ("Retranslate N affected") can ship as a
    single user gesture.

    Reuses /glossary/{entry_id}/retranslate-affected's mechanics
    per-entry, but consolidates the affected-chapter set across all
    requested terms before kicking the workers — chapters that use more
    than one of the selected terms are queued exactly once.

    Returns a per-entry breakdown so the UI can surface "term X queued
    47, term Y queued 12 (3 shared)" if it wants. Skipped in-flight
    chapters are aggregated since the user only cares that some couldn't
    be touched, not which entry triggered the skip."""
    # Resolve all entries, gate same-novel.
    entries = []
    for eid in body.entry_ids:
        e = await glossary_svc.get_one(conn, eid)
        if e is None:
            raise HTTPException(status_code=404, detail=f"entry {eid} not found")
        entries.append(e)
    novel_ids = {e.novel_id for e in entries}
    if len(novel_ids) != 1:
        raise HTTPException(
            status_code=400,
            detail="bulk retranslate requires all entries to belong to the same novel",
        )
    novel_id = next(iter(novel_ids))
    # Build the union of affected chapters across all entries. Per-entry
    # affected counts feed the UI breakdown; the dedup-set drives the
    # actual queue mutation.
    per_entry: list[dict] = []
    all_affected_ids: set[int] = set()
    per_entry_chapter_ids: dict[int, set[int]] = {}
    for e in entries:
        rows = await glossary_svc.find_chapters_using_term(conn, novel_id, e.term_zh)
        ids = {r["id"] for r in rows}
        per_entry_chapter_ids[e.id] = ids
        per_entry.append({"entry_id": e.id, "term_zh": e.term_zh, "affected_count": len(ids)})
        all_affected_ids |= ids
    if not all_affected_ids:
        return {
            "queued_count": 0,
            "skipped_in_flight": [],
            "per_entry": per_entry,
        }
    reset_ids = await queue_svc.reset_chapters_for_retranslate(
        conn, novel_id, list(all_affected_ids)
    )
    reset_set = set(reset_ids)
    # Look up chapter_num for skipped-in-flight reporting.
    cur = await conn.execute(
        f"SELECT id, chapter_num FROM chapters WHERE id IN ({','.join('?' * len(all_affected_ids))})",
        tuple(all_affected_ids),
    )
    rows = await cur.fetchall()
    skipped_in_flight = sorted(r["chapter_num"] for r in rows if r["id"] not in reset_set)
    for cid in reset_ids:
        queue_svc.spawn_translate_worker(novel_id, cid)
    return {
        "queued_count": len(reset_ids),
        "skipped_in_flight": skipped_in_flight,
        "per_entry": per_entry,
    }
