"""Bookmark CRUD routes (Initiative 2).

Three endpoints:
  * GET  /api/novels/{id}/bookmarks — list all bookmarks for one novel,
    ordered by (chapter_num, paragraph_index, id) so the panel can render
    them in reading order.
  * POST /api/novels/{id}/chapters/{n}/bookmarks — create a bookmark at
    an optional paragraph_index with an optional note.
  * DELETE /api/bookmarks/{id} — remove one.

No 'update bookmark' route — bookmarks are intentionally lightweight; a
user who wants to change a note can delete + recreate.
"""

from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from backend.db import get_conn
from backend.models import Bookmark, NewBookmark

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/novels/{novel_id}/bookmarks")
async def list_bookmarks(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> list[Bookmark]:
    """Bookmarks for one novel, in reading order.

    Denormalizes chapter_num via the JOIN so the response carries the
    chapter heading without the panel needing a second fetch."""
    cur = await conn.execute(
        "SELECT b.id, b.novel_id, b.chapter_id, c.chapter_num, "
        "b.paragraph_index, b.note, b.created_at "
        "FROM bookmarks b "
        "JOIN chapters c ON c.id = b.chapter_id "
        "WHERE b.novel_id = ? "
        "ORDER BY c.chapter_num, b.paragraph_index, b.id",
        (novel_id,),
    )
    rows = await cur.fetchall()
    return [
        Bookmark(
            id=r["id"],
            novel_id=r["novel_id"],
            chapter_id=r["chapter_id"],
            chapter_num=r["chapter_num"],
            paragraph_index=r["paragraph_index"],
            note=r["note"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.post("/novels/{novel_id}/chapters/{chapter_num}/bookmarks", status_code=201)
async def create_bookmark(
    novel_id: int,
    chapter_num: int,
    body: NewBookmark,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> Bookmark:
    """Bookmark the given paragraph of one chapter.

    Resolves chapter_id from (novel_id, chapter_num) inside the handler so
    callers don't need to know it. Notes are optional; paragraph_index is
    optional (chapter-level bookmark when omitted)."""
    cur = await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    ch = await cur.fetchone()
    if ch is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    note = (body.note or "").strip() or None
    cur = await conn.execute(
        "INSERT INTO bookmarks (novel_id, chapter_id, paragraph_index, note) "
        "VALUES (?, ?, ?, ?)",
        (novel_id, ch["id"], body.paragraph_index, note),
    )
    await conn.commit()
    new_id = cur.lastrowid
    cur = await conn.execute(
        "SELECT b.id, b.novel_id, b.chapter_id, c.chapter_num, "
        "b.paragraph_index, b.note, b.created_at "
        "FROM bookmarks b JOIN chapters c ON c.id = b.chapter_id "
        "WHERE b.id = ?",
        (new_id,),
    )
    r = await cur.fetchone()
    return Bookmark(
        id=r["id"],
        novel_id=r["novel_id"],
        chapter_id=r["chapter_id"],
        chapter_num=r["chapter_num"],
        paragraph_index=r["paragraph_index"],
        note=r["note"],
        created_at=r["created_at"],
    )


@router.delete("/bookmarks/{bookmark_id}")
async def delete_bookmark(
    bookmark_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Remove a bookmark by id. 404 when it doesn't exist."""
    cur = await conn.execute(
        "DELETE FROM bookmarks WHERE id = ?", (bookmark_id,)
    )
    await conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="bookmark not found")
    return {"ok": True}
