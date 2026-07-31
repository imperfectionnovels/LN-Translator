"""CAT editor segment feed (Phase 2, read-only).

One GET per chapter open. The service lazily backfills the segment store on
this read (build on first open, self-heal after out-of-band edits, rebuild on
a SEGMENTATION_VERSION bump), so the route commits before returning; that is
deliberate for a local single-user app where the read path is the natural
backfill trigger. Phase 3 adds the PATCH write surface.
"""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from backend.db import get_conn
from backend.models import SegmentListResponse
from backend.services import segments as segments_svc

router = APIRouter()


@router.get("/novels/{novel_id}/chapters/{chapter_num}/segments")
async def get_chapter_segments(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> SegmentListResponse:
    """Segment payload for one chapter. 404 only when the chapter row is
    missing; untranslated chapters return a status-only payload the editor
    turns into its pending / translating / error cards."""
    payload = await segments_svc.get_segments(conn, novel_id, chapter_num)
    if payload is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    # Persist any lazy backfill the read performed (no-op otherwise).
    await conn.commit()
    return SegmentListResponse(**payload)
