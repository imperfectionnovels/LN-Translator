"""Soft-delete + Archive for novels (Bundle 1.B, F11).

Replaces the previous hard-delete behavior. `DELETE /api/novels/{id}` now
flips `novels.deleted_at` (set to NOW); the novel disappears from the
default library list but its chapters / glossary / bookmarks / TM /
observations are preserved untouched. A second action ("Purge") is the
only path that fires CASCADE on those tables.

Default list filtering happens in `routes/novels.py`'s SELECTs by adding
`WHERE n.deleted_at IS NULL` to the cross-novel queries. Archive view
hits the same routes with `?archived=1` and inverts that predicate.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite
from fastapi import HTTPException


@dataclass(frozen=True)
class DeleteCounts:
    """Quantified-confirm dialog inputs: tell the user exactly what they're
    archiving (or purging) before they confirm. Computed once at delete time
    so the dialog text matches what the action will actually affect."""
    novel_id: int
    chapters: int
    glossary_entries: int
    bookmarks: int
    chapter_observations: int
    tm_segments: int
    fr_snapshots: int


async def _novel_row(conn: aiosqlite.Connection, novel_id: int) -> aiosqlite.Row:
    cur = await conn.execute(
        "SELECT id, title, deleted_at FROM novels WHERE id = ?", (novel_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="novel not found")
    return row


async def delete_counts(
    conn: aiosqlite.Connection, novel_id: int
) -> DeleteCounts:
    """Aggregate everything that would be lost on hard purge. Used by the
    quantified-confirm dialog whether the user is archiving (soft) or
    purging (hard) — same counts, different action semantics."""
    await _novel_row(conn, novel_id)  # 404 if missing
    cur = await conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM chapters WHERE novel_id = ?) AS chapters,
            (SELECT COUNT(*) FROM glossary_entries WHERE novel_id = ?) AS glossary,
            (SELECT COUNT(*) FROM bookmarks WHERE novel_id = ?) AS bookmarks,
            (SELECT COUNT(*) FROM chapter_observations co
                JOIN chapters c ON c.id = co.chapter_id
                WHERE c.novel_id = ?) AS observations,
            (SELECT COUNT(*) FROM tm_segments WHERE novel_id = ?) AS tm,
            (SELECT COUNT(*) FROM find_replace_snapshots
                WHERE novel_id = ?) AS fr
        """,
        (novel_id, novel_id, novel_id, novel_id, novel_id, novel_id),
    )
    row = await cur.fetchone()
    return DeleteCounts(
        novel_id=novel_id,
        chapters=int(row["chapters"] or 0),
        glossary_entries=int(row["glossary"] or 0),
        bookmarks=int(row["bookmarks"] or 0),
        chapter_observations=int(row["observations"] or 0),
        tm_segments=int(row["tm"] or 0),
        fr_snapshots=int(row["fr"] or 0),
    )


async def archive_novel(
    conn: aiosqlite.Connection, novel_id: int
) -> DeleteCounts:
    """Soft-delete. Sets deleted_at = datetime('now'). Idempotent — calling
    archive on an already-archived novel is a no-op (no error)."""
    counts = await delete_counts(conn, novel_id)
    await conn.execute(
        "UPDATE novels SET deleted_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NULL",
        (novel_id,),
    )
    await conn.commit()
    return counts


async def restore_novel(
    conn: aiosqlite.Connection, novel_id: int
) -> None:
    """Restore from Archive. Clears deleted_at. Errors with 404 if novel
    doesn't exist; 409 if the novel isn't archived."""
    row = await _novel_row(conn, novel_id)
    if row["deleted_at"] is None:
        raise HTTPException(
            status_code=409,
            detail="novel is not archived; nothing to restore",
        )
    await conn.execute(
        "UPDATE novels SET deleted_at = NULL WHERE id = ?",
        (novel_id,),
    )
    await conn.commit()


async def list_archived(
    conn: aiosqlite.Connection,
) -> list[dict]:
    """Return id+title+deleted_at for every archived novel. The full novel
    list endpoint already accepts ?archived=1 for the rich response; this
    helper is for service-layer callers (e.g. retention sweeps) that need
    a lightweight rowset."""
    cur = await conn.execute(
        "SELECT id, title, deleted_at FROM novels "
        "WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC",
    )
    return [dict(r) for r in await cur.fetchall()]


async def purge_novel(
    conn: aiosqlite.Connection, novel_id: int
) -> DeleteCounts:
    """Hard delete. Fires CASCADE on chapters / glossary / etc. Only allowed
    on already-archived novels (409 if novel is still active) — this is the
    safety rail: "you have to archive first, then explicitly purge."
    """
    row = await _novel_row(conn, novel_id)
    if row["deleted_at"] is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "novel must be archived before it can be purged; "
                "archive it first"
            ),
        )
    counts = await delete_counts(conn, novel_id)
    await conn.execute("DELETE FROM novels WHERE id = ?", (novel_id,))
    await conn.commit()
    return counts
