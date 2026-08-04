"""CAT Phase 5: APPROVED TRANSLATION EXAMPLES (confirmed-exemplar) block.

Contracts:
  - segments.fetch_confirmed_exemplar_pairs: recency ordering, source
    dedupe, ~400-char truncation, current-chapter exclusion, empty-target
    skip, limit cap.
  - prompt_inputs.fetch_confirmed_exemplars: flag gate at the fetch site
    (PROMPT_INCLUDE_CONFIRMED_EXEMPLARS), CONFIRMED_EXEMPLAR_LIMIT cap.
  - format_confirmed_exemplars / build_prompt: block renders next to the
    USER STYLE PREFERENCES block; ABSENT when no exemplars exist, leaving
    the prompt byte-identical to the pre-Phase-5 shape (cache safety: no
    PROMPT_TEMPLATE_VERSION bump needed).
  - worker integration: the exemplars reach translate_chapter, and
    prompt_config_snapshot records confirmed_exemplars_included + the flag.
"""

from __future__ import annotations

import json

import pytest

from backend import config
from backend.db import init_db, open_conn
from backend.models import TranslationResult
from backend.services import prompt_inputs
from backend.services import providers as providers_svc
from backend.services import queue as queue_svc
from backend.services import segments as segments_svc
from backend.services.translators.base import (
    build_prompt,
    format_confirmed_exemplars,
)

pytestmark = pytest.mark.asyncio


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in (
            "chapter_segments", "chapter_observations", "glossary_entries",
            "chapters", "novels", "providers",
        ):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    yield
    await _reset_db()


def _zh(ch: str, length: int = 29) -> str:
    return ch * length + "。"


async def _seed_novel_with_chapters(n_chapters: int = 3) -> tuple[int, list[int]]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        ids = []
        for i in range(1, n_chapters + 1):
            cur = await conn.execute(
                "INSERT INTO chapters (novel_id, chapter_num, title_zh, "
                "original_text, status) VALUES (?, ?, '第一章', ?, 'done')",
                (novel_id, i, _zh("甲")),
            )
            ids.append(cur.lastrowid)
        await conn.commit()
    return novel_id, ids


async def _insert_segment(
    novel_id: int, chapter_id: int, seg_index: int,
    source_text: str, target_text: str, *,
    status: str = "confirmed", confirmed_at: str | None = "2026-07-31 10:00:00",
) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO chapter_segments (novel_id, chapter_id, seg_index, "
            "source_text, source_hash, target_text, machine_text, status, "
            "origin, aligned, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', 1, ?)",
            (novel_id, chapter_id, seg_index, source_text,
             segments_svc.hash16(source_text), target_text, target_text,
             status, confirmed_at),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Service query
# ---------------------------------------------------------------------------


async def test_exemplars_recency_order_and_exclusion():
    novel_id, (ch1, ch2, ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Older confirmed.",
                          confirmed_at="2026-07-30 09:00:00")
    await _insert_segment(novel_id, ch2, 0, _zh("丙"), "Newer confirmed.",
                          confirmed_at="2026-07-31 09:00:00")
    # Rows on the excluded chapter never appear, however recent.
    await _insert_segment(novel_id, ch3, 0, _zh("丁"), "Own chapter.",
                          confirmed_at="2026-07-31 12:00:00")
    async with open_conn() as conn:
        pairs = await segments_svc.fetch_confirmed_exemplar_pairs(
            conn, novel_id, ch3, 5
        )
    assert pairs == [
        (_zh("丙"), "Newer confirmed."),
        (_zh("乙"), "Older confirmed."),
    ]


async def test_exemplars_dedupe_by_source_newest_wins():
    novel_id, (ch1, ch2, ch3) = await _seed_novel_with_chapters(3)
    src = _zh("戊")
    await _insert_segment(novel_id, ch1, 0, src, "First rendering.",
                          confirmed_at="2026-07-30 09:00:00")
    await _insert_segment(novel_id, ch2, 0, src, "Second rendering.",
                          confirmed_at="2026-07-31 09:00:00")
    async with open_conn() as conn:
        pairs = await segments_svc.fetch_confirmed_exemplar_pairs(
            conn, novel_id, ch3, 5
        )
    assert pairs == [(src, "Second rendering.")]


async def test_exemplars_skip_non_confirmed_and_empty_targets():
    novel_id, (ch1, ch2, ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("己"), "Edited only.",
                          status="edited", confirmed_at=None)
    await _insert_segment(novel_id, ch1, 1, _zh("庚"), "",
                          status="confirmed")
    await _insert_segment(novel_id, ch2, 0, _zh("辛"), "Real confirmed.",
                          status="confirmed")
    async with open_conn() as conn:
        pairs = await segments_svc.fetch_confirmed_exemplar_pairs(
            conn, novel_id, ch3, 5
        )
    assert pairs == [(_zh("辛"), "Real confirmed.")]


async def test_exemplars_truncate_and_limit():
    novel_id, (ch1, ch2, ch3) = await _seed_novel_with_chapters(3)
    long_src = "长" * 450 + "。"
    long_tgt = "x" * 900
    await _insert_segment(novel_id, ch1, 0, long_src, long_tgt,
                          confirmed_at="2026-07-31 09:00:06")
    for i in range(6):
        await _insert_segment(
            novel_id, ch2, i, _zh(chr(ord("一") + i)), f"Rendering {i}.",
            confirmed_at=f"2026-07-31 09:00:0{5 - i}",
        )
    async with open_conn() as conn:
        pairs = await segments_svc.fetch_confirmed_exemplar_pairs(
            conn, novel_id, ch3, 5
        )
    assert len(pairs) == 5
    # The newest row is the long one; both sides truncated to 400 chars.
    assert pairs[0] == (long_src[:400], long_tgt[:400])
    assert pairs[1] == (_zh("一"), "Rendering 0.")


# ---------------------------------------------------------------------------
# prompt_inputs flag gate
# ---------------------------------------------------------------------------


async def test_fetch_confirmed_exemplars_flag_gate(monkeypatch):
    novel_id, (ch1, _ch2, ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("壬"), "Confirmed pair.")
    async with open_conn() as conn:
        monkeypatch.setattr(
            config, "PROMPT_INCLUDE_CONFIRMED_EXEMPLARS", True
        )
        on = await prompt_inputs.fetch_confirmed_exemplars(conn, novel_id, ch3)
        assert on == [(_zh("壬"), "Confirmed pair.")]

        monkeypatch.setattr(
            config, "PROMPT_INCLUDE_CONFIRMED_EXEMPLARS", False
        )
        off = await prompt_inputs.fetch_confirmed_exemplars(conn, novel_id, ch3)
        assert off == []


# ---------------------------------------------------------------------------
# Prompt block rendering + cache safety
# ---------------------------------------------------------------------------


async def test_format_confirmed_exemplars_block_shape():
    block = format_confirmed_exemplars([("你好。", "Hello there.")])
    assert block.startswith("APPROVED TRANSLATION EXAMPLES (")
    assert "the user confirmed these renderings in earlier chapters" in block
    assert "glossary and structural rules still win" in block
    assert "SOURCE:    你好。" in block
    assert "CONFIRMED: Hello there." in block
    # Dash-free project rule for model-facing strings.
    assert "—" not in block and "–" not in block


async def test_format_confirmed_exemplars_empty_cases():
    assert format_confirmed_exemplars(None) == ""
    assert format_confirmed_exemplars([]) == ""
    assert format_confirmed_exemplars([("", "x"), ("y", "")]) == ""


async def test_build_prompt_block_position_near_style_edits():
    prompt = build_prompt(
        "章节正文。", "标题", [],
        style_edits=[("before text", "after text")],
        confirmed_exemplars=[("你好。", "Hello there.")],
    )
    style_at = prompt.index("USER STYLE PREFERENCES")
    exemplar_at = prompt.index("APPROVED TRANSLATION EXAMPLES")
    chapter_at = prompt.index("CHAPTER (Chinese):")
    # Adjacent voice-precedent blocks, both ahead of the chapter body.
    assert style_at < exemplar_at < chapter_at


async def test_build_prompt_byte_identical_without_exemplars():
    """Cache safety: the block is absent when no exemplars exist, so the
    prompt matches the pre-Phase-5 shape exactly and no
    PROMPT_TEMPLATE_VERSION bump is needed."""
    base_kwargs = dict(
        previous_context="Previous tail.",
        style_edits=[("a", "b")],
        style_note="Voice note.",
    )
    legacy = build_prompt("章节正文。", "标题", [], **base_kwargs)
    with_none = build_prompt(
        "章节正文。", "标题", [], confirmed_exemplars=None, **base_kwargs
    )
    with_empty = build_prompt(
        "章节正文。", "标题", [], confirmed_exemplars=[], **base_kwargs
    )
    assert legacy == with_none == with_empty
    assert "APPROVED TRANSLATION EXAMPLES" not in legacy


# ---------------------------------------------------------------------------
# Worker integration: exemplars reach the call; snapshot records the state
# ---------------------------------------------------------------------------


def _stub_translate(monkeypatch, calls: list):
    async def _fake(chapter_zh, title_zh, glossary, **kwargs):
        calls.append({"chapter_zh": chapter_zh, **kwargs})
        n = len(chapter_zh.split("\n\n"))
        text = "\n\n".join(
            f"Machine rendering number {i + 1} of this paragraph." for i in range(n)
        )
        return TranslationResult(title_en="T", translated_text=text, new_terms=[])
    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake)


async def _seed_pending_chapter(novel_id: int, chapter_num: int) -> int:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, "
            "original_text, status, translate_queued) "
            "VALUES (?, ?, '第二章', ?, 'pending', 1)",
            (novel_id, chapter_num, _zh("癸")),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return chapter_id


async def test_worker_passes_exemplars_and_stamps_snapshot(monkeypatch):
    await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m", is_default=True,
    )
    novel_id, (ch1, _ch2, _ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Confirmed exemplar.")
    pending_id = await _seed_pending_chapter(novel_id, 4)

    calls: list = []
    _stub_translate(monkeypatch, calls)
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, pending_id)

    assert calls, "translate_chapter was not called"
    assert calls[0]["confirmed_exemplars"] == [(_zh("乙"), "Confirmed exemplar.")]

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT prompt_config_snapshot FROM chapters WHERE id = ?",
            (pending_id,),
        )
        row = await cur.fetchone()
    snap = json.loads(row["prompt_config_snapshot"])
    assert snap["confirmed_exemplars_included"] is True
    assert snap["flags"]["PROMPT_INCLUDE_CONFIRMED_EXEMPLARS"] is True


async def test_worker_drops_exemplars_already_in_approved_block(monkeypatch):
    """A confirmed source that also recurs in the chapter being translated
    rides the APPROVED TRANSLATIONS block (cross-chapter exact match); the
    fetch site drops its exemplar copy so the prompt carries it once."""
    await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m", is_default=True,
    )
    novel_id, (ch1, _ch2, _ch3) = await _seed_novel_with_chapters(3)
    # _zh("癸") is the pending chapter's own source paragraph: its confirmed
    # rendering lands in approved_pairs, so the exemplar copy must drop.
    await _insert_segment(novel_id, ch1, 0, _zh("癸"), "Shared rendering.",
                          confirmed_at="2026-08-03 10:00:00")
    await _insert_segment(novel_id, ch1, 1, _zh("乙"), "Voice exemplar.",
                          confirmed_at="2026-08-03 09:00:00")
    pending_id = await _seed_pending_chapter(novel_id, 4)

    calls: list = []
    _stub_translate(monkeypatch, calls)
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, pending_id)

    call = calls[0]
    assert [(zh, en) for _i, zh, en in call["approved_pairs"]] == [
        (_zh("癸"), "Shared rendering.")
    ]
    assert call["confirmed_exemplars"] == [(_zh("乙"), "Voice exemplar.")]


async def test_worker_flag_off_drops_exemplars(monkeypatch):
    await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m", is_default=True,
    )
    novel_id, (ch1, _ch2, _ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Confirmed exemplar.")
    pending_id = await _seed_pending_chapter(novel_id, 4)

    calls: list = []
    _stub_translate(monkeypatch, calls)
    # F8: one patch site controls the fetch gate AND the snapshot.
    monkeypatch.setattr(
        config, "PROMPT_INCLUDE_CONFIRMED_EXEMPLARS", False
    )
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, pending_id)

    assert calls[0]["confirmed_exemplars"] is None

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT prompt_config_snapshot FROM chapters WHERE id = ?",
            (pending_id,),
        )
        row = await cur.fetchone()
    snap = json.loads(row["prompt_config_snapshot"])
    assert snap["confirmed_exemplars_included"] is False
    assert snap["flags"]["PROMPT_INCLUDE_CONFIRMED_EXEMPLARS"] is False
