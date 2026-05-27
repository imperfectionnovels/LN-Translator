"""Find/Replace commit snapshots + restore (Bundle 1.B, F36).

Every successful find/replace commit writes a `find_replace_snapshots` row
containing the pre-substitution body of each changed chapter. Restore
endpoint replays the snapshot back onto the chapters atomically.

Retention: 30-day rolling. Per-row size cap: ~5MB on the JSON payload —
oversized substitutions skip snapshot recording with a logged warning
rather than failing the commit (the commit's the load-bearing thing).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import aiosqlite
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Size cap on the snapshot payload (JSON-encoded chapter bodies). Set to
# leave plenty of headroom for SQLite TEXT columns (no hard limit, but
# very large rows degrade perf). A substitution touching 200 chapters of
# 3KB English each ≈ 600KB — well under cap. Substitutions touching
# entire long novels (1000+ chapters) at full size could exceed this; we
# skip snapshot rather than fail commit in that case.
SNAPSHOT_PAYLOAD_BYTE_CAP = 5 * 1024 * 1024

# Retention in days. Anything older than this is purgeable by a sweep.
RETENTION_DAYS = 30


@dataclass(frozen=True)
class SnapshotSummary:
    """Returned to clients listing a novel's snapshot history."""
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


async def record_snapshot(
    conn: aiosqlite.Connection,
    *,
    novel_id: int,
    commit_token: str,
    find_pattern: str,
    replace_pattern: str,
    target: str,
    scope: str,
    chapters_changed: int,
    payload: dict,
) -> int | None:
    """Write a pre-substitution snapshot. Returns the new row id or None
    if the payload exceeded the cap (the substitution still commits;
    only the restore safety net is unavailable for that particular run).

    Caller is responsible for building `payload` as a dict mapping
    chapter_id (str) → {"translated_before": str | None, "refined_before":
    str | None}. We JSON-encode and store as a TEXT column.

    Does NOT commit; callers wrap in their own transaction so the
    snapshot lives inside the same transaction that did the substitution.
    """
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(payload_json.encode("utf-8")) > SNAPSHOT_PAYLOAD_BYTE_CAP:
        logger.warning(
            "fr_snapshots: payload exceeded %d bytes for novel %d "
            "(%d chapters); skipping snapshot record",
            SNAPSHOT_PAYLOAD_BYTE_CAP, novel_id, chapters_changed,
        )
        return None
    cur = await conn.execute(
        """
        INSERT INTO find_replace_snapshots
            (novel_id, commit_token, find_pattern, replace_pattern,
             target, scope, chapters_changed, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            novel_id, commit_token, find_pattern, replace_pattern,
            target, scope, chapters_changed, payload_json,
        ),
    )
    return cur.lastrowid


async def list_for_novel(
    conn: aiosqlite.Connection, novel_id: int
) -> list[SnapshotSummary]:
    """Return snapshot history for one novel, newest first. The full
    payload_json is NOT returned here — the History tab in the UI only
    needs the summary fields; the actual restore path loads payload on
    demand inside `restore_snapshot`."""
    cur = await conn.execute(
        """
        SELECT id, novel_id, commit_token, find_pattern, replace_pattern,
               target, scope, chapters_changed, committed_at, restored_at
        FROM find_replace_snapshots
        WHERE novel_id = ?
        ORDER BY committed_at DESC
        """,
        (novel_id,),
    )
    rows = await cur.fetchall()
    return [
        SnapshotSummary(
            id=r["id"], novel_id=r["novel_id"],
            commit_token=r["commit_token"],
            find_pattern=r["find_pattern"],
            replace_pattern=r["replace_pattern"],
            target=r["target"], scope=r["scope"],
            chapters_changed=r["chapters_changed"],
            committed_at=r["committed_at"],
            restored_at=r["restored_at"],
        )
        for r in rows
    ]


async def restore_snapshot(
    conn: aiosqlite.Connection, snapshot_id: int
) -> dict:
    """Replay a snapshot back onto chapters. Reverses the substitution
    that produced the snapshot. After restore, the snapshot row is
    marked with restored_at (so the UI can show it as "Restored on
    YYYY-MM-DD" and disable the Restore button).

    Restore is itself an UPDATE — it does NOT write a fresh snapshot
    (otherwise the user could ping-pong forever). Callers that want
    redo can re-run find/replace.

    Returns: {snapshot_id, chapters_restored, target}.
    """
    cur = await conn.execute(
        """
        SELECT novel_id, payload_json, target, restored_at
        FROM find_replace_snapshots WHERE id = ?
        """,
        (snapshot_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    if row["restored_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail="snapshot has already been restored",
        )

    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(
            status_code=500,
            detail=f"snapshot payload is malformed: {e}",
        ) from e

    target = row["target"]
    chapters_restored = 0
    await conn.execute("BEGIN IMMEDIATE")
    try:
        for chapter_id_str, before in payload.items():
            chapter_id = int(chapter_id_str)
            sets: list[str] = []
            params: list = []
            if target in ("translated_text", "both") and "translated_before" in before:
                sets.append("translated_text = ?")
                params.append(before["translated_before"])
            if target in ("refined_text", "both") and "refined_before" in before:
                sets.append("refined_text = ?")
                params.append(before["refined_before"])
            if not sets:
                continue
            params.append(chapter_id)
            cur = await conn.execute(
                f"UPDATE chapters SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if (cur.rowcount or 0) > 0:
                chapters_restored += 1
        await conn.execute(
            "UPDATE find_replace_snapshots SET restored_at = datetime('now') "
            "WHERE id = ?",
            (snapshot_id,),
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    return {
        "snapshot_id": snapshot_id,
        "chapters_restored": chapters_restored,
        "target": target,
    }


async def purge_old_snapshots(conn: aiosqlite.Connection) -> int:
    """Retention sweep. Deletes snapshots older than RETENTION_DAYS.
    Idempotent; intended for periodic invocation (manual or hook)."""
    cur = await conn.execute(
        f"DELETE FROM find_replace_snapshots "
        f"WHERE committed_at < datetime('now', '-{RETENTION_DAYS} days')",
    )
    await conn.commit()
    return cur.rowcount or 0
