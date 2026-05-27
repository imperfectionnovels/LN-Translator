"""Routes for the QA dashboard (Initiative 1).

Three endpoints:
  * GET /api/novels/{id}/observations — aggregate counts per chapter, plus
    a novel-wide total. Powers the reader TOC dots and the library badge.
  * GET /api/novels/{id}/chapters/{n}/observations — full list for one
    chapter, ordered by id (stable insertion order).
  * POST /api/observations/{id}/dismiss — soft-dismiss one observation.
    Dismissal does NOT survive a chapter retranslation (the worker's
    DELETE+INSERT in the success-commit transaction wipes all prior rows
    for the chapter).
"""

from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.db import get_conn
from backend.models import Observation, ObservationsSummary
from backend.services.observations import severity_tier_for

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/observations/library-summary")
async def library_observations_summary(
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict[int, int]:
    """Per-novel undismissed-observation counts across all novels.

    One query for the entire library page so the badge render doesn't N+1
    over novels. Returns novel_id → count (omitting novels with zero)."""
    cur = await conn.execute(
        "SELECT c.novel_id, COUNT(o.id) AS n "
        "FROM chapters c "
        "JOIN chapter_observations o ON o.chapter_id = c.id "
        "WHERE o.dismissed_at IS NULL "
        "GROUP BY c.novel_id"
    )
    rows = await cur.fetchall()
    return {r["novel_id"]: r["n"] for r in rows}


@router.get("/novels/{novel_id}/observations")
async def list_novel_observations(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> ObservationsSummary:
    """Aggregate undismissed-observation counts per chapter for one novel.

    Returns chapter_num → count (skipping chapters with zero) plus the
    sum. The reader's TOC paints a dot on chapters present in the map; the
    library's per-novel badge uses `total_undismissed`."""
    cur = await conn.execute(
        "SELECT c.chapter_num, COUNT(o.id) AS n "
        "FROM chapters c "
        "JOIN chapter_observations o ON o.chapter_id = c.id "
        "WHERE c.novel_id = ? AND o.dismissed_at IS NULL "
        "GROUP BY c.chapter_num",
        (novel_id,),
    )
    rows = await cur.fetchall()
    by_chapter: dict[int, int] = {r["chapter_num"]: r["n"] for r in rows}
    return ObservationsSummary(
        total_undismissed=sum(by_chapter.values()),
        by_chapter=by_chapter,
    )


@router.get("/novels/{novel_id}/chapters/{chapter_num}/observations")
async def list_chapter_observations(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
    include_dismissed: bool = False,
) -> list[Observation]:
    """Full observation list for one chapter, ordered by id.

    Includes dismissed rows when `include_dismissed=true` so the reader can
    offer an "undismiss" path (out of v1 scope but the column is exposed
    so the front-end can decide later)."""
    cur = await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    ch = await cur.fetchone()
    if ch is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if include_dismissed:
        cur = await conn.execute(
            "SELECT id, chapter_id, kind, severity, paragraph_index, "
            "excerpt, created_at, dismissed_at "
            "FROM chapter_observations WHERE chapter_id = ? ORDER BY id",
            (ch["id"],),
        )
    else:
        cur = await conn.execute(
            "SELECT id, chapter_id, kind, severity, paragraph_index, "
            "excerpt, created_at, dismissed_at "
            "FROM chapter_observations "
            "WHERE chapter_id = ? AND dismissed_at IS NULL ORDER BY id",
            (ch["id"],),
        )
    rows = await cur.fetchall()
    return [
        Observation(
            id=r["id"],
            chapter_id=r["chapter_id"],
            kind=r["kind"],
            severity=r["severity"],
            severity_tier=severity_tier_for(r["kind"]),
            paragraph_index=r["paragraph_index"],
            excerpt=r["excerpt"],
            created_at=r["created_at"],
            dismissed_at=r["dismissed_at"],
        )
        for r in rows
    ]


# F26 (2026-05-25): bulk-dismiss endpoints. Per-chapter dismisses many
# rows at once; per-novel-by-kind dismisses every undismissed obs of one
# kind across a novel ("dismiss all MT-texture observations across this
# novel"). Both close out the per-row-click tedium.

@router.post("/novels/{novel_id}/chapters/{chapter_num}/observations/bulk-dismiss")
async def bulk_dismiss_chapter_observations(
    novel_id: int,
    chapter_num: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Dismiss every undismissed observation on one chapter. Returns the
    count. Idempotent: a chapter with no undismissed obs returns 0."""
    cur = await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    ch = await cur.fetchone()
    if ch is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    cur = await conn.execute(
        "UPDATE chapter_observations SET dismissed_at = datetime('now') "
        "WHERE chapter_id = ? AND dismissed_at IS NULL",
        (ch["id"],),
    )
    await conn.commit()
    return {"dismissed_count": cur.rowcount or 0}


@router.post("/novels/{novel_id}/observations/bulk-dismiss-by-kind/{kind}")
async def bulk_dismiss_by_kind(
    novel_id: int,
    kind: str,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Dismiss every undismissed observation of one kind across an entire
    novel. Use when a stylistic observer is producing chronic false
    positives for a novel — pairs with the per-novel disabled_observers
    setting to silence future hits too."""
    cur = await conn.execute(
        "UPDATE chapter_observations SET dismissed_at = datetime('now') "
        "WHERE chapter_id IN (SELECT id FROM chapters WHERE novel_id = ?) "
        "AND kind = ? AND dismissed_at IS NULL",
        (novel_id, kind),
    )
    await conn.commit()
    return {"dismissed_count": cur.rowcount or 0, "kind": kind}


@router.get("/diagnostics")
async def get_diagnostics(
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """F44 (2026-05-25): in-app diagnostics surface. Returns runtime
    info the user can copy into a bug report — port, DB path, default
    provider summary, recent log tail (best-effort). Replaces "go look
    in USER_DATA_ROOT/logs/" knowledge gap.

    Extended 2026-05-26 with version/python/platform + size breakdowns
    (db / cache / covers / library total) for the settings About card.
    The Copy-diagnostics button on /settings serialises this payload."""
    import os  # noqa: PLC0415
    import platform  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from backend import __version__  # noqa: PLC0415
    from backend.config import DB_PATH, IS_FROZEN, USER_DATA_ROOT  # noqa: PLC0415
    from backend.services.llm_cache import get_stats as cache_stats  # noqa: PLC0415
    from backend.services.providers import get_default_provider  # noqa: PLC0415

    default = await get_default_provider()
    log_path = USER_DATA_ROOT / "logs" / "startup.log"
    covers_dir = USER_DATA_ROOT / "covers"

    def _gather_disk_info() -> tuple[list[str], int, int]:
        # All filesystem I/O for this endpoint runs in one threadpool
        # hop so the event loop isn't blocked by readlines / os.walk /
        # stat. Returns (log_tail, db_bytes, covers_bytes).
        log_tail: list[str] = []
        if log_path.exists():
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                    log_tail = [ln.rstrip() for ln in lines[-50:]]
            except OSError:
                log_tail = ["(log file present but unreadable)"]

        def _dir_size(p) -> int:
            if not p.exists():
                return 0
            total = 0
            try:
                for root, _dirs, files in os.walk(p):
                    for fname in files:
                        try:
                            total += (p.__class__(root) / fname).stat().st_size
                        except OSError:
                            continue
            except OSError:
                pass
            return total

        db_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        covers_bytes = _dir_size(covers_dir)
        return log_tail, db_bytes, covers_bytes

    log_tail, db_bytes, covers_bytes = await run_in_threadpool(_gather_disk_info)
    cache = cache_stats()
    cache_bytes = int(cache.get("on_disk_bytes") or 0)
    return {
        "version": __version__,
        "frozen": IS_FROZEN,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "db_path": str(DB_PATH),
        "user_data_root": str(USER_DATA_ROOT),
        "data_root": str(USER_DATA_ROOT),
        "log_folder": str(USER_DATA_ROOT / "logs"),
        "covers_dir": str(covers_dir),
        "db_bytes": db_bytes,
        "cache_bytes": cache_bytes,
        "cache_files": int(cache.get("on_disk_files") or 0),
        "covers_bytes": covers_bytes,
        "library_bytes": db_bytes + cache_bytes + covers_bytes,
        "telemetry": "off",
        "default_provider": (
            {
                "id": default.id,
                "name": default.name,
                "type": default.provider_type,
                "model_id": default.model_id,
            } if default else None
        ),
        "cache_stats": cache,
        "log_tail": log_tail,
    }


@router.get("/diagnostics/log-folder")
async def get_diagnostics_log_folder() -> dict:
    """Return the absolute path to the log folder. /settings's About card
    uses this for an "Open log folder" action."""
    from backend.config import USER_DATA_ROOT  # noqa: PLC0415
    return {"path": str(USER_DATA_ROOT / "logs")}


@router.post("/observations/{observation_id}/dismiss")
async def dismiss_observation(
    observation_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Soft-dismiss one observation. Idempotent: already-dismissed rows
    return ok=True without changing the stored timestamp."""
    cur = await conn.execute(
        "UPDATE chapter_observations SET dismissed_at = datetime('now') "
        "WHERE id = ? AND dismissed_at IS NULL",
        (observation_id,),
    )
    await conn.commit()
    if (cur.rowcount or 0) == 0:
        # Either the id doesn't exist or it was already dismissed.
        cur = await conn.execute(
            "SELECT 1 FROM chapter_observations WHERE id = ?",
            (observation_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="observation not found")
    return {"ok": True}
