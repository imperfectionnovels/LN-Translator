"""The translate worker must refuse a chapter whose source text is empty.

Import skeletons (resumable scrapes) insert chapter rows with
original_text='' and status='pending' before the fill phase runs. Without
a guard, translate-all on a paused import sends an empty CHAPTER block to
the LLM, which improvises a chapter that then commits as status='done'
(silent fabricated content). The worker must error the row instead and
never call the translator.
"""

from __future__ import annotations

import pytest


async def _fresh_db():
    from backend.db import init_db, open_conn

    await init_db()
    async with open_conn() as conn:
        for t in ("chapter_translation_attempts", "chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await conn.commit()


async def _insert_novel_and_chapter(
    original_text: str, import_source_url: str | None = None
) -> tuple[int, int]:
    from backend.db import open_conn

    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "status, translate_queued, import_source_url) "
            "VALUES (?, 1, ?, 'pending', 1, ?)",
            (novel_id, original_text, import_source_url),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


@pytest.mark.asyncio
async def test_empty_source_chapter_errors_without_llm_call(monkeypatch):
    from backend.db import open_conn
    from backend.services import providers as providers_svc
    from backend.services import queue as queue_svc

    await _fresh_db()
    await providers_svc.create_provider(
        name="p", provider_type="gemini", model_id="m", is_default=True,
    )
    novel_id, chapter_id = await _insert_novel_and_chapter("   \n  ")

    calls: list[object] = []

    async def _fake_translate(*a, **kw):
        calls.append(a)
        raise AssertionError("translator must not be called for empty source")

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status, translate_queued, error_msg FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert calls == []
    assert row["status"] == "error"
    assert row["translate_queued"] == 0
    assert "empty" in (row["error_msg"] or "").lower()


@pytest.mark.asyncio
async def test_unfetched_import_skeleton_errors_with_fetch_message(monkeypatch):
    """A skeleton row (empty source + import_source_url set) gets a message
    that points at the unfinished import, not a generic empty-text error."""
    from backend.db import open_conn
    from backend.services import providers as providers_svc
    from backend.services import queue as queue_svc

    await _fresh_db()
    await providers_svc.create_provider(
        name="p", provider_type="gemini", model_id="m", is_default=True,
    )
    novel_id, chapter_id = await _insert_novel_and_chapter(
        "", import_source_url="https://example.com/ch1"
    )

    async def _fake_translate(*a, **kw):
        raise AssertionError("translator must not be called for a skeleton row")

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status, error_msg FROM chapters WHERE id = ?", (chapter_id,)
        )
        row = await cur.fetchone()
    assert row["status"] == "error"
    assert "fetched" in (row["error_msg"] or "").lower()
