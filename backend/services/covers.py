"""Cover-image storage helper (Initiative 2 + Initiative 7 reuse).

`write_cover_for_novel` is the bytes-in service layer that the HTTP cover-
upload route and the EPUB importer both call. It performs the magic-byte
sniff, atomic temp+rename write, and `novels.cover_image_path` UPDATE in
one shot so EPUB imports can land a cover without duplicating any of the
HTTP-route logic.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import aiosqlite

from backend.config import USER_DATA_ROOT

logger = logging.getLogger(__name__)

# Public so the HTTP route can keep using the same constant for its 413.
MAX_COVER_BYTES = 8 * 1024 * 1024


_COVERS_DIR = USER_DATA_ROOT / "covers"


def sniff_image_ext(head: bytes) -> str | None:
    """Identify image kind from leading magic bytes. PNG / JPEG / GIF / WebP.

    Returns the canonical extension (no dot) or None for unsupported bytes."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def resolve_cover_path(stored: str | None) -> Path | None:
    """Map novels.cover_image_path (relative) to an absolute path under
    USER_DATA_ROOT. Returns None when the column is empty, the file is
    missing, or the relative path would escape USER_DATA_ROOT (defense
    against a tampered row)."""
    if not stored:
        return None
    candidate = (USER_DATA_ROOT / stored).resolve()
    try:
        candidate.relative_to(USER_DATA_ROOT.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def unlink_existing(stored: str | None) -> None:
    """Best-effort delete of a prior cover file. Errors are logged and
    swallowed — the DB column gets overwritten regardless, and a stray
    blob is a cleanup nuisance, not a correctness issue."""
    path = resolve_cover_path(stored)
    if path is None:
        return
    try:
        path.unlink()
    except OSError as e:
        logger.warning("could not unlink prior cover %s: %s", path, e)


async def write_cover_for_novel(
    conn: aiosqlite.Connection,
    novel_id: int,
    image_bytes: bytes,
    *,
    ext_hint: str | None = None,
    source: str | None = None,
) -> tuple[str, int] | None:
    """Write `image_bytes` as the cover for `novel_id` and stamp
    novels.cover_image_path. Returns `(relative_path, size_bytes)` on
    success, or None when the bytes don't look like a supported image
    (PNG / JPEG / GIF / WebP) — callers in the upload routes interpret
    None as "EPUB shipped no usable cover, leave the novel uncovered"
    rather than raising.

    `ext_hint` is consulted only as a fallback when the magic-byte sniff
    fails (some EPUBs ship a JPEG with PNG bytes or other oddities); the
    magic byte sniff is authoritative when it matches.

    `source` stamps novels.cover_source with the provenance label
    ('epub' | 'url' | 'upload'). NULL leaves the column untouched. The
    library card renders a small "scraped" / "epub" pip off this value.

    Atomic write via temp file in the same directory then os.replace —
    the on-disk file appears at its final name in one step. The DB
    UPDATE follows; an os.replace failure (e.g. filesystem full) raises
    before the row changes.

    Does NOT commit — caller controls the transaction. The cover write
    itself touches the filesystem before the UPDATE; a crash between
    them leaves an orphan blob (which the next cover upload overwrites
    or which `unlink_existing` will mop up). This matches the
    routes/covers.py contract.
    """
    if not image_bytes:
        return None
    ext = sniff_image_ext(image_bytes[:16])
    if ext is None and ext_hint:
        hint = ext_hint.lower().lstrip(".")
        if hint in {"png", "jpg", "jpeg", "gif", "webp"}:
            ext = "jpg" if hint == "jpeg" else hint
    if ext is None:
        return None
    if len(image_bytes) > MAX_COVER_BYTES:
        # The HTTP route raises 413 on this; EPUB-extracted covers just
        # get silently skipped — an overly large embedded cover shouldn't
        # block the whole import.
        logger.info(
            "cover bytes for novel %d exceed %d-byte cap; skipping",
            novel_id, MAX_COVER_BYTES,
        )
        return None

    _COVERS_DIR.mkdir(parents=True, exist_ok=True)

    # Snapshot the prior path so the stale file can be cleaned up after
    # the new one commits — if the rename fails, the old one stays.
    cur = await conn.execute(
        "SELECT cover_image_path FROM novels WHERE id = ?", (novel_id,)
    )
    row = await cur.fetchone()
    prior_path = row["cover_image_path"] if row else None

    final_name = f"{novel_id}.{ext}"
    final_path = _COVERS_DIR / final_name
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{novel_id}_", suffix=f".{ext}", dir=_COVERS_DIR,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
        os.replace(tmp_name, final_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    relative_path = str(final_path.relative_to(USER_DATA_ROOT))
    if source is not None:
        await conn.execute(
            "UPDATE novels SET cover_image_path = ?, cover_source = ? WHERE id = ?",
            (relative_path, source, novel_id),
        )
    else:
        await conn.execute(
            "UPDATE novels SET cover_image_path = ? WHERE id = ?",
            (relative_path, novel_id),
        )

    if prior_path and prior_path != relative_path:
        unlink_existing(prior_path)

    return relative_path, len(image_bytes)
