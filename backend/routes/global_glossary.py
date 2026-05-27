"""Global glossary CRUD routes (Initiative 3).

Mirrors the per-novel glossary surface but with no novel_id in the path:
  * GET    /api/glossary/global              — list all
  * POST   /api/glossary/global              — create (409 on term_zh collision)
  * PATCH  /api/glossary/global/{id}         — partial update
  * DELETE /api/glossary/global/{id}         — remove
  * GET    /api/glossary/global/{id}/usage   — per-novel chapter counts where
                                               this term's Chinese appears.
                                               Used by the scope-warning dialog.

Plus one transition route for moving per-novel rows into the global table:
  * POST   /api/glossary/{id}/promote-to-global
"""

from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.db import get_conn
from backend.models import (
    GlobalGlossaryEntry,
    GlobalGlossaryUpdate,
    GlobalGlossaryUsage,
    NewGlobalGlossaryEntry,
)
from backend.services import find_replace as fr
from backend.services import global_glossary as global_svc
from backend.services import glossary as glossary_svc

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/glossary/global")
async def list_global(
    conn: aiosqlite.Connection = Depends(get_conn),
) -> list[GlobalGlossaryEntry]:
    return await global_svc.list_all(conn)


@router.post("/glossary/global")
async def create_global(
    body: NewGlobalGlossaryEntry,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> GlobalGlossaryEntry:
    try:
        return await global_svc.create_entry(
            conn,
            term_zh=body.term_zh,
            term_en=body.term_en,
            category=body.category,
            notes=body.notes,
            usage_note=body.usage_note,
        )
    except global_svc.GlobalGlossaryConflict as conflict:
        # 409 carries the existing entry so the UI can offer "edit the
        # existing global instead" rather than just failing.
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(conflict),
                "existing": conflict.existing.model_dump(),
            },
        ) from conflict
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.patch("/glossary/global/{entry_id}")
async def update_global(
    entry_id: int,
    body: GlobalGlossaryUpdate,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> GlobalGlossaryEntry:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        result = await global_svc.update_entry(
            conn,
            entry_id,
            term_en=updates.get("term_en"),
            category=updates.get("category"),
            notes=updates.get("notes"),
            usage_note=updates.get("usage_note"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail="global entry not found")
    return result


@router.delete("/glossary/global/{entry_id}")
async def delete_global(
    entry_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    deleted = await global_svc.delete_entry(conn, entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="global entry not found")
    return {"ok": True}


@router.get("/glossary/global/{entry_id}/usage")
async def usage_global(
    entry_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> list[GlobalGlossaryUsage]:
    entry = await global_svc.get_one(conn, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="global entry not found")
    rows = await global_svc.usage_per_novel(conn, entry.term_zh)
    return [GlobalGlossaryUsage(**r) for r in rows]


class GlobalApplyInPlaceRequest(BaseModel):
    """Body for /api/glossary/global/{id}/apply-in-place. See the
    per-novel ApplyInPlaceRequest — same shape, different scope."""

    old_en: str = Field(min_length=1, max_length=200)
    new_en: str = Field(min_length=1, max_length=200)


class GlobalApplyInPlaceResponse(BaseModel):
    chapters_updated: int
    rows_updated_translated: int
    rows_updated_refined: int


@router.post("/glossary/global/{entry_id}/apply-in-place")
async def apply_in_place_global(
    entry_id: int,
    body: GlobalApplyInPlaceRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> GlobalApplyInPlaceResponse:
    """Substitute `old_en` with `new_en` across EVERY novel's chapters.

    Same word-boundary, case-sensitive contract as the per-novel
    apply-in-place. The blast radius is the full library, so the UI
    must show the per-novel impact count first (the existing
    /usage endpoint does this)."""
    entry = await global_svc.get_one(conn, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="global entry not found")
    result = await fr.apply_in_place_for_glossary_term(
        conn,
        old_en=body.old_en,
        new_en=body.new_en,
        novel_id=None,  # all-novels scope
    )
    return GlobalApplyInPlaceResponse(
        chapters_updated=result.chapters_updated,
        rows_updated_translated=result.rows_updated_translated,
        rows_updated_refined=result.rows_updated_refined,
    )


@router.post("/glossary/{entry_id}/promote-to-global")
async def promote_to_global(
    entry_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> GlobalGlossaryEntry:
    """Move a per-novel glossary entry into the global table.

    Refuses (409) when a global entry with the same `term_zh` already
    exists — the UI prompts the user to merge or edit the global instead.
    On success, the per-novel row is deleted so the global wins everywhere
    (per-novel rows that shadow this term in OTHER novels stay intact;
    only this one novel's row gets removed). Atomic: both writes happen
    in one transaction.
    """
    src = await glossary_svc.get_one(conn, entry_id)
    if src is None:
        raise HTTPException(status_code=404, detail="entry not found")
    existing = await global_svc.get_by_term_zh(conn, src.term_zh)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"global glossary already has term {src.term_zh!r}; "
                    f"edit the existing global entry instead, or delete it first."
                ),
                "existing": existing.model_dump(),
            },
        )
    # Single transaction so promote can't half-succeed (insert global but
    # leave the per-novel row behind, or vice versa).
    cur = await conn.execute(
        "INSERT INTO global_glossary_entries "
        "(term_zh, term_en, category, notes, usage_note) "
        "VALUES (?, ?, ?, ?, ?)",
        (src.term_zh, src.term_en, src.category, src.notes, src.usage_note),
    )
    new_id = cur.lastrowid
    await conn.execute("DELETE FROM glossary_entries WHERE id = ?", (entry_id,))
    await conn.commit()
    result = await global_svc.get_one(conn, new_id)
    assert result is not None
    return result
