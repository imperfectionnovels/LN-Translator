"""Cover-image upload / serve routes (Initiative 2).

Covers land on disk at USER_DATA_ROOT/covers/{novel_id}.{ext} so the
SQLite file stays small and backups remain diffable. The DB tracks the
RELATIVE path under USER_DATA_ROOT — that survives a USER_DATA_ROOT move
(dev → frozen-mode user data dir) without rewriting every row.

Image validation: magic-byte sniff on the first 12 bytes. We accept PNG,
JPEG, WebP, and GIF — enough for any cover the user is realistically
going to upload, and a bytewise check stops a renamed .exe from getting
in via extension trickery.
"""

from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from backend.db import get_conn
from backend.services.covers import (
    MAX_COVER_BYTES as _MAX_COVER_BYTES,
)
from backend.services.covers import (
    resolve_cover_path as _resolve_cover_path,
)
from backend.services.covers import (
    sniff_image_ext as _sniff_image_ext,
)
from backend.services.covers import (
    unlink_existing as _unlink_existing,
)
from backend.services.covers import (
    write_cover_for_novel,
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def _ensure_novel_exists(conn: aiosqlite.Connection, novel_id: int) -> None:
    cur = await conn.execute("SELECT 1 FROM novels WHERE id = ?", (novel_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="novel not found")


@router.post("/{novel_id}/cover")
async def upload_cover(
    novel_id: int,
    file: UploadFile = File(...),
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Upload (or replace) a novel cover. Validates magic bytes, caps size,
    writes atomically (temp + rename), updates novels.cover_image_path."""
    await _ensure_novel_exists(conn, novel_id)
    head = await file.read(_MAX_COVER_BYTES + 1)
    if len(head) > _MAX_COVER_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"cover too large; cap is {_MAX_COVER_BYTES // (1024 * 1024)} MB",
        )
    if _sniff_image_ext(head[:16]) is None:
        raise HTTPException(
            status_code=400,
            detail="unsupported image format; upload PNG, JPEG, GIF, or WebP",
        )

    written = await write_cover_for_novel(conn, novel_id, bytes(head), source="upload")
    if written is None:
        # _sniff_image_ext already validated; the only way for the helper to
        # return None now would be empty bytes (already gated above) or a
        # truly malformed magic-byte mismatch with a header that we don't
        # support. Surface as the standard 400.
        raise HTTPException(
            status_code=400,
            detail="unsupported image format; upload PNG, JPEG, GIF, or WebP",
        )
    relative_path, size_bytes = written
    await conn.commit()
    return {
        "ok": True,
        "cover_image_path": relative_path,
        "size_bytes": size_bytes,
    }


@router.get("/{novel_id}/cover")
async def serve_cover(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> FileResponse:
    """Stream the cover image. 404 when none uploaded."""
    cur = await conn.execute(
        "SELECT cover_image_path FROM novels WHERE id = ?", (novel_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="novel not found")
    path = _resolve_cover_path(row["cover_image_path"])
    if path is None:
        raise HTTPException(status_code=404, detail="no cover uploaded")
    # FileResponse handles ETag + If-Modified-Since automatically; covers
    # change rarely so the browser cache is well-behaved.
    return FileResponse(path)


@router.delete("/{novel_id}/cover")
async def delete_cover(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Remove the cover. Idempotent — returns ok=True even when none was
    stored. Clears the column and unlinks the file."""
    cur = await conn.execute(
        "SELECT cover_image_path FROM novels WHERE id = ?", (novel_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="novel not found")
    _unlink_existing(row["cover_image_path"])
    await conn.execute(
        "UPDATE novels SET cover_image_path = NULL WHERE id = ?", (novel_id,)
    )
    await conn.commit()
    return {"ok": True}
