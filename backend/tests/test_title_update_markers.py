"""Author update-count markers (（第四更！）, 求月票) must never reach the
translated title.

Three layers, tested end-to-end through the queue worker:
1. The prompt inputs (CHAPTER TITLE line + heading echoed in the body) are
   stripped before translate_chapter sees them.
2. The stored title_zh / original_text stay verbatim (source fidelity).
3. The zh-gated normalize_title_en backstop drops a trailing parenthetical
   from the model's title even when the model translates the marker anyway.
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


TITLE_ZH = "第392章 惊变！（第四更！）"
SOURCE = "第392章 惊变！（第四更！）\n\n白莲教举教齐至，香火冲天。\n\n正文继续。"


@pytest.mark.asyncio
async def test_update_marker_stripped_from_prompt_and_title(monkeypatch):
    from backend.db import open_conn
    from backend.models import TranslationResult
    from backend.services import providers as providers_svc
    from backend.services import queue as queue_svc

    await _fresh_db()
    await providers_svc.create_provider(
        name="p", provider_type="gemini", model_id="m", is_default=True,
    )
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, "
            "original_text, status, translate_queued) "
            "VALUES (?, 392, ?, ?, 'pending', 1)",
            (novel_id, TITLE_ZH, SOURCE),
        )
        chapter_id = cur.lastrowid
        await conn.commit()

    seen: dict[str, object] = {}

    async def _fake_translate(chapter_zh, title_zh, glossary, **kw):
        seen["chapter_zh"] = chapter_zh
        seen["title_zh"] = title_zh
        # The model translates the marker anyway — the backstop must catch it.
        return TranslationResult(
            title_en="Sudden Turn! (Fourth Update!)",
            translated_text="The body.",
            new_terms=[],
        )

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status, title_zh, title_en, original_text FROM chapters "
            "WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()

    # 1. Prompt inputs were cleaned.
    assert seen["title_zh"] == "第392章 惊变！"
    assert seen["chapter_zh"].split("\n")[0] == "第392章 惊变！"
    assert "正文继续。" in seen["chapter_zh"]
    # 2. Stored source stays verbatim.
    assert row["title_zh"] == TITLE_ZH
    assert row["original_text"] == SOURCE
    # 3. Backstop stripped the translated marker from the committed title.
    assert row["status"] == "done"
    assert row["title_en"] == "Chapter 392: Sudden Turn!"
