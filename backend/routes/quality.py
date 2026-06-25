"""Quality + consistency cockpit routes (read-only).

Surfaces the measurement IP that previously only ran as CLI scripts:
  * GET /api/novels/{id}/quality?chapters=LO-HI   per-category scorecard + worst chapters
  * GET /api/novels/{id}/consistency               TCR overall/per-category + worst terms
  * GET /api/novels/{id}/chapters/{n}/quality      single-chapter badge data

Routes stay thin: parse, existence-check, delegate to services/quality_dashboard,
return the raw dict (the service is the schema, matching routes/stats.py).
"""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.db import get_conn
from backend.services import quality_dashboard as qd

router = APIRouter()


def _parse_range(chapters: str | None) -> tuple[int, int]:
    if not chapters:
        return 1, 10**9
    try:
        lo_s, hi_s = chapters.split("-", 1)
        return int(lo_s), int(hi_s)
    except ValueError as exc:  # noqa: B904 - message is the point
        raise HTTPException(
            status_code=400, detail="chapters must be LO-HI, e.g. 800-880"
        ) from exc


async def _novel_exists(conn: aiosqlite.Connection, novel_id: int) -> bool:
    cur = await conn.execute("SELECT 1 FROM novels WHERE id = ?", (novel_id,))
    return await cur.fetchone() is not None


@router.get("/novels/{novel_id}/quality")
async def novel_quality(
    novel_id: int,
    chapters: str | None = Query(default=None),
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    if not await _novel_exists(conn, novel_id):
        raise HTTPException(status_code=404, detail="novel not found")
    lo, hi = _parse_range(chapters)
    card = await qd.scorecard(novel_id, lo, hi)
    if card is None:
        raise HTTPException(
            status_code=404,
            detail=f"no done chapters in novel {novel_id} range {lo}-{hi}",
        )
    return card


@router.get("/novels/{novel_id}/consistency")
async def novel_consistency(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    if not await _novel_exists(conn, novel_id):
        raise HTTPException(status_code=404, detail="novel not found")
    return await qd.consistency(novel_id)


@router.get("/novels/{novel_id}/chapters/{chapter_num}/quality")
async def chapter_quality(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    result = await qd.chapter_quality(conn, novel_id, chapter_num)
    if result is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return result
