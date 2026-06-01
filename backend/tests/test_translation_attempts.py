"""Tests for the translation attempts log service."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from backend.db import _ADDITIVE_MIGRATIONS, SCHEMA

DB_PATH = Path(os.environ["DB_PATH"])


def _setup_db() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    for stmt in _ADDITIVE_MIGRATIONS:
        try:
            conn.executescript(stmt)
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()


def _insert_chapter() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
    cur = conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, original_text, status) "
        "VALUES (1, 1, '...', 'done')",
    )
    conn.commit()
    chapter_id = cur.lastrowid
    conn.close()
    return chapter_id


@pytest.mark.asyncio
async def test_record_and_list():
    from backend.db import open_conn
    from backend.services.translation_attempts import (
        list_for_chapter,
        record_attempt,
    )

    _setup_db()
    chapter_id = _insert_chapter()

    async with open_conn() as conn:
        attempt_id = await record_attempt(
            conn,
            chapter_id=chapter_id,
            provider_id=None,
            model_id="claude-opus-4-7",
            status="ok",
            parse_error=None,
            prompt_snapshot="<prompt>",
            retry_count=0,
        )
        await conn.commit()
    assert attempt_id is not None

    async with open_conn() as conn:
        rows = await list_for_chapter(conn, chapter_id)
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].model_id == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_list_newest_first():
    from backend.db import open_conn
    from backend.services.translation_attempts import (
        list_for_chapter,
        record_attempt,
    )

    _setup_db()
    chapter_id = _insert_chapter()

    async with open_conn() as conn:
        for status in ("parse_failed", "ok"):
            await record_attempt(
                conn,
                chapter_id=chapter_id,
                provider_id=None,
                model_id="m",
                status=status,
                parse_error=None,
                prompt_snapshot=None,
                retry_count=0,
            )
        await conn.commit()

    async with open_conn() as conn:
        rows = await list_for_chapter(conn, chapter_id)
    # Newest first → 'ok' inserted last appears first.
    assert rows[0].status == "ok"
    assert rows[1].status == "parse_failed"


@pytest.mark.asyncio
async def test_latest_prompt_returns_most_recent():
    from backend.db import open_conn
    from backend.services.translation_attempts import (
        latest_prompt,
        record_attempt,
    )

    _setup_db()
    chapter_id = _insert_chapter()

    async with open_conn() as conn:
        await record_attempt(
            conn, chapter_id=chapter_id, provider_id=None,
            model_id="m", status="ok",
            parse_error=None, prompt_snapshot="OLD PROMPT", retry_count=0,
        )
        await record_attempt(
            conn, chapter_id=chapter_id, provider_id=None,
            model_id="m", status="ok",
            parse_error=None, prompt_snapshot="NEW PROMPT", retry_count=0,
        )
        await conn.commit()

    async with open_conn() as conn:
        snapshot = await latest_prompt(conn, chapter_id)
    assert snapshot == "NEW PROMPT"


@pytest.mark.asyncio
async def test_latest_prompt_skips_null_prompts():
    """If the most-recent attempt has no prompt_snapshot, return the
    next-most-recent one that does. Some attempts (parse_failed retries)
    may not carry a snapshot."""
    from backend.db import open_conn
    from backend.services.translation_attempts import (
        latest_prompt,
        record_attempt,
    )

    _setup_db()
    chapter_id = _insert_chapter()

    async with open_conn() as conn:
        await record_attempt(
            conn, chapter_id=chapter_id, provider_id=None,
            model_id="m", status="ok",
            parse_error=None, prompt_snapshot="HAS PROMPT", retry_count=0,
        )
        await record_attempt(
            conn, chapter_id=chapter_id, provider_id=None,
            model_id="m", status="error",
            parse_error="x", prompt_snapshot=None, retry_count=0,
        )
        await conn.commit()

    async with open_conn() as conn:
        snapshot = await latest_prompt(conn, chapter_id)
    assert snapshot == "HAS PROMPT"


@pytest.mark.asyncio
async def test_latest_prompt_404_when_none():
    from fastapi import HTTPException

    from backend.db import open_conn
    from backend.services.translation_attempts import latest_prompt

    _setup_db()
    chapter_id = _insert_chapter()

    async with open_conn() as conn:
        with pytest.raises(HTTPException) as exc_info:
            await latest_prompt(conn, chapter_id)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_caps_at_limit():
    from backend.db import open_conn
    from backend.services.translation_attempts import (
        list_for_chapter,
        record_attempt,
    )

    _setup_db()
    chapter_id = _insert_chapter()

    async with open_conn() as conn:
        for _ in range(30):
            await record_attempt(
                conn, chapter_id=chapter_id, provider_id=None,
                model_id="m", status="ok",
                parse_error=None, prompt_snapshot=None, retry_count=0,
            )
        await conn.commit()

    async with open_conn() as conn:
        rows = await list_for_chapter(conn, chapter_id, limit=10)
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_fk_cascade_on_chapter_delete():
    from backend.db import open_conn
    from backend.services.translation_attempts import (
        list_for_chapter,
        record_attempt,
    )

    _setup_db()
    chapter_id = _insert_chapter()

    async with open_conn() as conn:
        await record_attempt(
            conn, chapter_id=chapter_id, provider_id=None,
            model_id="m", status="ok",
            parse_error=None, prompt_snapshot="x", retry_count=0,
        )
        await conn.commit()

    conn_sync = sqlite3.connect(DB_PATH)
    conn_sync.execute("PRAGMA foreign_keys = ON")
    conn_sync.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
    conn_sync.commit()
    conn_sync.close()

    async with open_conn() as conn:
        rows = await list_for_chapter(conn, chapter_id)
    assert rows == []


@pytest.mark.asyncio
async def test_failed_translation_records_error_attempt(monkeypatch):
    """A translation that raises must record an 'error' attempt row in the
    same transaction as the status='error' update, so the provider's
    failure_rate_30d metric reflects real failures. Regression: the error
    path previously only updated the chapter and never logged an attempt,
    pinning failure_rate_30d at 0."""
    from backend.db import init_db, open_conn
    from backend.services import providers as providers_svc
    from backend.services import queue as queue_svc
    from backend.services.translation_attempts import list_for_chapter

    await init_db()
    async with open_conn() as conn:
        for t in ("chapter_translation_attempts", "chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await conn.commit()

    p = await providers_svc.create_provider(
        name="failing", provider_type="gemini", model_id="m", is_default=True,
    )
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "status, translate_queued) VALUES (?, 1, '原文', 'pending', 1)",
            (novel_id,),
        )
        chapter_id = cur.lastrowid
        await conn.commit()

    async def _boom(*a, **kw):
        raise RuntimeError("simulated upstream failure")
    monkeypatch.setattr("backend.services.queue.translate_chapter", _boom)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status FROM chapters WHERE id = ?", (chapter_id,)
        )
        assert (await cur.fetchone())["status"] == "error"
        rows = await list_for_chapter(conn, chapter_id)
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].provider_id == p.id
    assert "simulated upstream failure" in (rows[0].parse_error or "")
