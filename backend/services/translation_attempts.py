"""Translation attempts log (Bundle 2, F22).

One row per `_translate_chapter_in_db` invocation. Records the prompt
that was sent, the parse status, the error message (if any), and how
many retries the worker had to do. Powers two UI surfaces:

- The "View translation attempts" panel per chapter in the reader (edit
  mode only) — shows the history including any failed-then-recovered
  parse retries.
- The "Show prompt" diagnostic — returns the most recent prompt_snapshot
  so the user can see exactly what the LLM received.

Written inside the same transaction as the chapter UPDATE so a partial
attempt (started but not finished) won't appear as a phantom row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import aiosqlite
from fastapi import HTTPException

AttemptStatus = Literal["ok", "parse_failed", "fallback_plaintext", "error"]


@dataclass(frozen=True)
class AttemptRow:
    id: int
    chapter_id: int
    provider_id: int | None
    model_id: str | None
    started_at: str
    finished_at: str | None
    status: str
    parse_error: str | None
    retry_count: int
    # prompt_snapshot is omitted from the list endpoint (it can be very
    # large); fetched separately via latest_prompt for the diagnostic.


async def record_attempt(
    conn: aiosqlite.Connection,
    *,
    chapter_id: int,
    provider_id: int | None,
    model_id: str | None,
    status: AttemptStatus,
    parse_error: str | None,
    prompt_snapshot: str | None,
    retry_count: int,
) -> int:
    """INSERT one attempt row. Returns the new id. Does NOT commit;
    callers wrap inside their own transaction so the attempt row's
    lifetime matches the chapter commit's."""
    cur = await conn.execute(
        """
        INSERT INTO chapter_translation_attempts
            (chapter_id, provider_id, model_id, finished_at,
             status, parse_error, prompt_snapshot, retry_count)
        VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?)
        """,
        (
            chapter_id, provider_id, model_id, status,
            parse_error, prompt_snapshot, retry_count,
        ),
    )
    return cur.lastrowid


async def list_for_chapter(
    conn: aiosqlite.Connection, chapter_id: int, limit: int = 20
) -> list[AttemptRow]:
    """Return the attempt history for one chapter, newest first.
    Caps at `limit` to keep the panel scannable. Excludes the prompt
    snapshot — that's heavy and only the most-recent one matters for
    diagnostic purposes (see latest_prompt)."""
    cur = await conn.execute(
        """
        SELECT id, chapter_id, provider_id, model_id, started_at, finished_at,
               status, parse_error, retry_count
        FROM chapter_translation_attempts
        WHERE chapter_id = ?
        ORDER BY started_at DESC, id DESC
        LIMIT ?
        """,
        (chapter_id, limit),
    )
    rows = await cur.fetchall()
    return [
        AttemptRow(
            id=r["id"], chapter_id=r["chapter_id"],
            provider_id=r["provider_id"], model_id=r["model_id"],
            started_at=r["started_at"], finished_at=r["finished_at"],
            status=r["status"], parse_error=r["parse_error"],
            retry_count=r["retry_count"],
        )
        for r in rows
    ]


async def latest_prompt(
    conn: aiosqlite.Connection, chapter_id: int
) -> str:
    """Return the most-recent attempt's prompt_snapshot for the
    'Show prompt' diagnostic. 404 if no attempts have been recorded
    for this chapter yet."""
    cur = await conn.execute(
        """
        SELECT prompt_snapshot FROM chapter_translation_attempts
        WHERE chapter_id = ? AND prompt_snapshot IS NOT NULL
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (chapter_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="no prompt snapshot recorded for this chapter yet",
        )
    return row["prompt_snapshot"]
