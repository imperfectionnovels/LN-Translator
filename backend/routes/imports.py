"""Resumable import endpoints.

Three small surfaces the library page uses to drive the
in-progress / paused / resume UX:

- `GET /api/imports/{novel_id}/status` — poll the current import state
  (used by library card badges).
- `POST /api/imports/{novel_id}/cancel` — flip an in-progress import to
  'paused'. Partial novel stays in the library.
- `POST /api/imports/{novel_id}/resume` — re-fire the runner for a
  paused recipe-scrape novel. No-op for bulk/EPUB paused novels
  (their source is gone).
"""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from backend.db import get_conn
from backend.services import import_runner

router = APIRouter()


@router.get("/{novel_id}/status")
async def get_import_status(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Snapshot of a novel's import state.

    Returns:
      - status: NULL | 'in_progress' | 'paused' | 'done' | 'cancelled'.
      - total_chapters: count of chapter rows the novel has.
      - fetched_chapters: count where import_fetched_at IS NOT NULL OR
        original_text != '' (covers both recipe skeleton rows that got
        filled AND bulk-inserted rows that never used the skeleton).
      - resumable: True iff at least one chapter has import_source_url
        set AND import_fetched_at NULL (i.e. recipe-scrape pending).
        False for bulk/EPUB paused novels.
    """
    cur = await conn.execute(
        "SELECT title, import_status FROM novels WHERE id = ?",
        (novel_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="novel not found")
    cur = await conn.execute(
        "SELECT "
        "COUNT(*) AS total, "
        "SUM(CASE WHEN original_text != '' OR import_fetched_at IS NOT NULL "
        "         THEN 1 ELSE 0 END) AS fetched, "
        "SUM(CASE WHEN import_source_url IS NOT NULL "
        "         AND import_fetched_at IS NULL "
        "         THEN 1 ELSE 0 END) AS pending_resumable "
        "FROM chapters WHERE novel_id = ?",
        (novel_id,),
    )
    counts = await cur.fetchone()
    total = int(counts["total"] or 0)
    fetched = int(counts["fetched"] or 0)
    pending_resumable = int(counts["pending_resumable"] or 0)
    return {
        "novel_id": novel_id,
        "title": row["title"],
        "status": row["import_status"],
        "total_chapters": total,
        "fetched_chapters": fetched,
        "resumable": pending_resumable > 0,
    }


@router.post("/{novel_id}/cancel")
async def cancel_import(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Cancel an in-progress import. The runner's fill loop checks
    `import_status` between chapter fetches and exits cleanly when it
    sees 'paused'. Partial novel stays in the library — the user can
    Resume later or delete explicitly.

    Returns:
      - flipped: True if the status was actually changed from
        'in_progress' to 'paused'. False when the novel wasn't
        in_progress (already paused / done / cancelled).
    """
    cur = await conn.execute(
        "SELECT id FROM novels WHERE id = ?", (novel_id,),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="novel not found")
    flipped = await import_runner.cancel_import(novel_id)
    return {"novel_id": novel_id, "flipped": flipped, "status": "paused"}


@router.post("/{novel_id}/resume")
async def resume_import(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Resume a paused recipe-scrape import. Spawns the runner as a
    background task; returns immediately. Library card polls the
    status endpoint to track progress.

    Returns 400 when the novel isn't resumable — either it's not in a
    paused state, or it's a bulk/EPUB import whose source bytes are
    gone (no chapters with import_source_url set).
    """
    cur = await conn.execute(
        "SELECT import_status FROM novels WHERE id = ?", (novel_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="novel not found")
    if row["import_status"] not in ("paused", None):
        raise HTTPException(
            status_code=400,
            detail=(
                f"novel import_status={row['import_status']!r} — only "
                "paused (or NULL) imports can be resumed."
            ),
        )
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM chapters "
        "WHERE novel_id = ? AND import_source_url IS NOT NULL "
        "AND import_fetched_at IS NULL",
        (novel_id,),
    )
    pending = int((await cur.fetchone())["n"] or 0)
    if pending == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Nothing to resume. This novel has no recipe-scrape "
                "chapters awaiting fetch — bulk / EPUB imports can't be "
                "resumed (the source bytes lived in the upload and are "
                "gone). Use the Append flow to add more chapters."
            ),
        )
    # Flip back to in_progress so the fill loop's status check passes.
    await conn.execute(
        "UPDATE novels SET import_status = 'in_progress' WHERE id = ?",
        (novel_id,),
    )
    await conn.commit()
    import_runner.spawn_resume(novel_id)
    return {
        "novel_id": novel_id,
        "status": "in_progress",
        "pending_chapters": pending,
    }
