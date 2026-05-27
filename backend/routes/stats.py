"""Stats dashboard routes (Initiative 6).

Two endpoints:
  * GET /api/stats/global         — library-wide aggregate.
  * GET /api/stats/novel/{id}     — per-novel rollup.

Routes return raw dicts from the service layer; FastAPI serializes them
straight to JSON. No Pydantic round-trip — the service is the schema.
"""

from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from backend.db import get_conn
from backend.services import stats as stats_svc

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/stats/global")
async def global_dashboard(
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    return await stats_svc.global_stats(conn)


@router.get("/stats/novel/{novel_id}")
async def novel_dashboard(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    result = await stats_svc.novel_stats(conn, novel_id)
    if result is None:
        raise HTTPException(status_code=404, detail="novel not found")
    return result
