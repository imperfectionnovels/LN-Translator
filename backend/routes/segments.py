"""CAT editor segment surface.

GET: one fetch per chapter open. The service lazily backfills the segment
store on this read (build on first open, self-heal after out-of-band edits,
rebuild on a SEGMENTATION_VERSION bump), so the route commits before
returning; that is deliberate for a local single-user app where the read
path is the natural backfill trigger.

PATCH / confirm-all: the Phase 3 editing loop. Thin routes: parse, call the
service, commit, return. Stale writes surface as 409 with a structured
detail {message, error_kind} (error_kind one of 'stale_segment',
'stale_chapter', 'chapter_translating'), the same shape the scrape route
uses, so api.js exposes err.error_kind without re-parsing. Editor writes
take no lock (invariant I5); the guards are the rev + hash echoes.
"""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from backend.db import get_conn
from backend.models import (
    EditorNext,
    SegmentAssist,
    SegmentConfirmAll,
    SegmentConfirmAllResult,
    SegmentListResponse,
    SegmentPatch,
    SegmentPatchResult,
)
from backend.services import segments as segments_svc

router = APIRouter()


@router.get("/novels/{novel_id}/editor-next")
async def editor_next(
    novel_id: int,
    after: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> EditorNext:
    """The editor's continue card (CAT Phase 5): the next chapter that still
    needs work (untranslated, or its segment store is missing or carries any
    non-confirmed row), searching forward from `after` then wrapping.
    next_chapter_num is None when every chapter is fully confirmed. 404 when
    the novel does not exist."""
    cur = await conn.execute(
        "SELECT 1 FROM novels WHERE id = ?", (novel_id,)
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="novel not found")
    nxt = await segments_svc.next_chapter_to_edit(conn, novel_id, after)
    return EditorNext(next_chapter_num=nxt)


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


@router.get(
    "/novels/{novel_id}/chapters/{chapter_num}/segments/{seg_index}/assist"
)
async def get_segment_assist(
    novel_id: int,
    chapter_num: int,
    seg_index: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> SegmentAssist:
    """Assist-rail feed for one segment (Phase 4): exact TM matches (same
    source_hash in other chapters, full-text verified, confirmed first),
    fuzzy TM matches (rapidfuzz over chapter_segments sources, threshold
    0.80, cap 5), and the row's stored machine rendering. Read-only."""
    payload = await segments_svc.segment_assist(
        conn, novel_id, chapter_num, seg_index
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="segment not found")
    return SegmentAssist(**payload)


@router.patch("/novels/{novel_id}/chapters/{chapter_num}/segments/{seg_index}")
async def patch_chapter_segment(
    novel_id: int,
    chapter_num: int,
    seg_index: int,
    payload: SegmentPatch,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> SegmentPatchResult:
    """One segment write (save / confirm / save_and_confirm / unconfirm /
    revert_machine). 400 on a bad action or empty save text, 404 on a
    missing chapter or segment, 409 (structured) on stale state."""
    try:
        result = await segments_svc.update_segment(
            conn,
            novel_id,
            chapter_num,
            seg_index,
            action=payload.action,
            after_text=payload.after_text,
            client_rev=payload.chapter_rev,
            before_target_hash=payload.before_target_hash,
            chapter_id=payload.chapter_id,
        )
    except segments_svc.SegmentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except segments_svc.SegmentActionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except segments_svc.SegmentStaleError as e:
        raise HTTPException(
            status_code=409,
            detail={"message": str(e), "error_kind": e.kind},
        ) from e
    await conn.commit()
    return SegmentPatchResult(**result)


@router.post("/novels/{novel_id}/chapters/{chapter_num}/segments/confirm-all")
async def confirm_all_segments(
    novel_id: int,
    chapter_num: int,
    payload: SegmentConfirmAll,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> SegmentConfirmAllResult:
    """Confirm every remaining machine/edited segment (rev-guarded; the UI
    gates this behind a danger confirm dialog)."""
    try:
        result = await segments_svc.confirm_all(
            conn,
            novel_id,
            chapter_num,
            client_rev=payload.chapter_rev,
            statuses=payload.statuses,
            chapter_id=payload.chapter_id,
        )
    except segments_svc.SegmentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except segments_svc.SegmentActionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except segments_svc.SegmentStaleError as e:
        raise HTTPException(
            status_code=409,
            detail={"message": str(e), "error_kind": e.kind},
        ) from e
    await conn.commit()
    return SegmentConfirmAllResult(**result)
