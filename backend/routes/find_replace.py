"""Project-wide find/replace routes (Initiative 4).

Two endpoints:
  * POST /api/find    — preview a substitution; returns hit counts +
                        sample lines + a frozen-preview token.
  * POST /api/replace — commit a previously-generated preview by token.
                        Refuses with 409 when chapter content has drifted
                        between preview and commit.
"""

from __future__ import annotations

import logging
from typing import Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.db import get_conn
from backend.services import find_replace as fr

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class FindRequest(BaseModel):
    find: str = Field(min_length=1, max_length=2000)
    replacement: str = Field(max_length=8000)
    scope_kind: Literal["chapter", "novel", "novels", "all"]
    scope_ids: list[int] = Field(default_factory=list)
    target_cols: list[Literal["translated_text", "refined_text"]] = Field(
        default_factory=lambda: ["translated_text", "refined_text"]
    )
    use_regex: bool = False
    case_sensitive: bool = True
    word_boundary: bool = False


class ChapterPreviewResponse(BaseModel):
    chapter_id: int
    novel_id: int
    novel_title: str
    chapter_num: int
    chapter_title_en: str | None
    hits_translated: int
    hits_refined: int
    sample_lines: list[str]


class PreviewResponse(BaseModel):
    token: str
    expires_at: float
    total_chapters: int
    total_hits_translated: int
    total_hits_refined: int
    truncated: bool
    rows: list[ChapterPreviewResponse]


class CommitRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class CommitResponse(BaseModel):
    chapters_updated: int
    rows_updated_translated: int
    rows_updated_refined: int
    # Snapshot row ids written during this commit, one per touched novel.
    # Empty when there were no matches or when a novel's snapshot was skipped
    # due to payload size. Clients can use these ids to offer cross-novel undo.
    snapshot_ids: list[int] = []


class FrSnapshot(BaseModel):
    """One find/replace commit-log entry (mirrors services.fr_snapshots
    SnapshotSummary). Rendered by the History tab; carries enough to show
    the commit and a Restore button."""
    id: int
    novel_id: int
    commit_token: str
    find_pattern: str
    replace_pattern: str
    target: str
    scope: str
    chapters_changed: int
    committed_at: str
    restored_at: str | None


class RestoreSnapshotResponse(BaseModel):
    """Result of replaying a snapshot (mirrors services.fr_snapshots
    RestoreResult). Matches the typed contract of /find and /replace in this
    router instead of a bare dict passthrough."""
    snapshot_id: int
    chapters_restored: int
    target: str


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@router.post("/find")
async def find_preview(
    body: FindRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> PreviewResponse:
    """Compute hit counts and a sample preview, return a frozen-snapshot
    token the caller must POST to /api/replace to commit."""
    if not body.target_cols:
        raise HTTPException(
            status_code=400,
            detail="target_cols must list at least one of 'translated_text', 'refined_text'",
        )
    query = fr.FindReplaceQuery(
        find=body.find,
        replacement=body.replacement,
        scope_kind=body.scope_kind,
        scope_ids=body.scope_ids,
        target_cols=body.target_cols,
        use_regex=body.use_regex,
        case_sensitive=body.case_sensitive,
        word_boundary=body.word_boundary,
    )
    try:
        result = await fr.build_preview(conn, query)
    except fr.InvalidPatternError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return PreviewResponse(
        token=result.token,
        expires_at=result.expires_at,
        total_chapters=result.total_chapters,
        total_hits_translated=result.total_hits_translated,
        total_hits_refined=result.total_hits_refined,
        truncated=result.truncated,
        rows=[ChapterPreviewResponse(**row.__dict__) for row in result.rows],
    )


@router.post("/replace")
async def find_replace_commit(
    body: CommitRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> CommitResponse:
    """Apply a previously-previewed substitution. Returns 410 when the
    token is unknown / expired and 409 when chapter content has drifted
    since the preview was generated."""
    try:
        result = await fr.commit_preview(conn, body.token)
    except fr.TokenExpiredError as e:
        raise HTTPException(
            status_code=410,
            detail="preview token is unknown or expired; re-preview before committing",
        ) from e
    except fr.PreviewDriftError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "drifted_chapter_ids": e.drifted_chapter_ids,
            },
        ) from e
    except fr.InvalidPatternError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return CommitResponse(
        chapters_updated=result.chapters_updated,
        rows_updated_translated=result.rows_updated_translated,
        rows_updated_refined=result.rows_updated_refined,
        snapshot_ids=result.snapshot_ids,
    )


# ---- F36 (2026-05-25): snapshot history + restore ------------------------

@router.get("/novels/{novel_id}/fr-snapshots")
async def list_fr_snapshots(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> list[FrSnapshot]:
    """Per-novel find/replace commit log. Returns snapshots newest first;
    each row carries enough metadata for the History tab to render +
    a Restore button. Payload is fetched on demand by the restore route."""
    from backend.services.fr_snapshots import list_for_novel  # noqa: PLC0415
    snapshots = await list_for_novel(conn, novel_id)
    return [FrSnapshot(**vars(s)) for s in snapshots]


@router.post("/fr-snapshots/{snapshot_id}/restore")
async def restore_fr_snapshot(
    snapshot_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> RestoreSnapshotResponse:
    """Replay a snapshot back onto the affected chapters. Single-shot:
    once restored, the snapshot is marked restored_at and can't be
    replayed again (the History row's button disables)."""
    from backend.services.fr_snapshots import restore_snapshot  # noqa: PLC0415
    return RestoreSnapshotResponse(**await restore_snapshot(conn, snapshot_id))
