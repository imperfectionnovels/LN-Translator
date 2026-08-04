"""Post-pivot gap audit (Item A): USER STYLE PREFERENCES sourced from the
segment ledger.

The CAT pivot severed the style_edits in-app producer (the edit-paragraph
endpoint); the restoration feeds the same prompt arm from chapter_segments
instead. Contracts:

  - segments.recent_edited_pairs: recency ordering (COALESCE(edited_at,
    confirmed_at) DESC, id DESC tiebreak), before-side dedupe, ~400-char
    truncation, current-chapter exclusion, empty-target / no-op-edit /
    NULL-machine skip, limit cap.
  - prompt_inputs.fetch_style_edits: segment pairs outrank legacy
    style_edits rows, one STYLE_EDIT_LIMIT cap and one before-side dedupe
    across both sources, same PROMPT_INCLUDE_STYLE_EDITS gate, same return
    shape (build_prompt / format_style_edits untouched).
  - cache safety: no pairs -> the block is absent and the prompt is
    byte-identical to the pre-change shape (no PROMPT_TEMPLATE_VERSION bump).
  - worker integration: a segment-sourced pair reaches translate_chapter's
    style_edits and prompt_config_snapshot.style_edits_included stays true;
    pairs whose AFTER already rides this chapter's APPROVED TRANSLATIONS
    block are dropped at the queue fetch site (Phase 5 dedupe convention).
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
from backend.services.translators.base import build_prompt

pytestmark = pytest.mark.asyncio


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in (
            "chapter_segments", "chapter_observations", "style_edits",
            "glossary_entries", "chapters", "novels", "providers",
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


@pytest.fixture(autouse=True)
def style_edits_flag_on(monkeypatch):
    # F8: flags single-source through backend.config; one patch reaches
    # every consumer (prompt_inputs gate + queue snapshot alike).
    monkeypatch.setattr(config, "PROMPT_INCLUDE_STYLE_EDITS", True)


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
    machine_text: str | None = None,
    status: str = "edited",
    edited_at: str | None = "2026-08-04 10:00:00",
    confirmed_at: str | None = None,
) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO chapter_segments (novel_id, chapter_id, seg_index, "
            "source_text, source_hash, target_text, machine_text, status, "
            "origin, aligned, edited_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', 1, ?, ?)",
            (novel_id, chapter_id, seg_index, source_text,
             segments_svc.hash16(source_text), target_text, machine_text,
             status, edited_at, confirmed_at),
        )
        await conn.commit()


async def _insert_style_edit(novel_id: int, before: str, after: str) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO style_edits (novel_id, chapter_id, before_text, "
            "after_text) VALUES (?, NULL, ?, ?)",
            (novel_id, before, after),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# segments.recent_edited_pairs
# ---------------------------------------------------------------------------


async def test_recent_pairs_recency_order_and_exclusion():
    novel_id, (ch1, ch2, ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Older fix.",
                          machine_text="Older draft.",
                          edited_at="2026-08-03 09:00:00")
    await _insert_segment(novel_id, ch2, 0, _zh("丙"), "Newer fix.",
                          machine_text="Newer draft.",
                          edited_at="2026-08-04 09:00:00")
    # Confirmed rows qualify via confirmed_at when edited_at is NULL.
    await _insert_segment(novel_id, ch2, 1, _zh("戌"), "Confirmed fix.",
                          machine_text="Confirmed draft.", status="confirmed",
                          edited_at=None, confirmed_at="2026-08-04 12:00:00")
    # Rows on the excluded chapter never appear, however recent.
    await _insert_segment(novel_id, ch3, 0, _zh("丁"), "Own chapter fix.",
                          machine_text="Own chapter draft.",
                          edited_at="2026-08-04 15:00:00")
    async with open_conn() as conn:
        pairs = await segments_svc.recent_edited_pairs(conn, novel_id, ch3, 10)
    assert pairs == [
        ("Confirmed draft.", "Confirmed fix."),
        ("Newer draft.", "Newer fix."),
        ("Older draft.", "Older fix."),
    ]


async def test_recent_pairs_id_desc_tiebreak_and_before_dedupe():
    novel_id, (ch1, ch2, ch3) = await _seed_novel_with_chapters(3)
    stamp = "2026-08-04 09:00:00"
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "First rendering.",
                          machine_text="Same draft.", edited_at=stamp)
    await _insert_segment(novel_id, ch2, 0, _zh("丙"), "Second rendering.",
                          machine_text="Same draft.", edited_at=stamp)
    async with open_conn() as conn:
        pairs = await segments_svc.recent_edited_pairs(conn, novel_id, ch3, 10)
    # Equal timestamps: the higher id wins; the duplicate before-side dedupes.
    assert pairs == [("Same draft.", "Second rendering.")]


async def test_recent_pairs_skip_unqualified_rows():
    novel_id, (ch1, _ch2, ch3) = await _seed_novel_with_chapters(3)
    # machine NULL: nothing to teach from.
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Target only.",
                          machine_text=None)
    # No-op edit: machine == target.
    await _insert_segment(novel_id, ch1, 1, _zh("丙"), "Unchanged.",
                          machine_text="Unchanged.")
    # Empty target.
    await _insert_segment(novel_id, ch1, 2, _zh("丁"), "",
                          machine_text="Draft of empty.")
    # Machine-status row: not a human edit.
    await _insert_segment(novel_id, ch1, 3, _zh("戊"), "Prefilled.",
                          machine_text="Prefill draft.", status="machine")
    # The one qualifying row.
    await _insert_segment(novel_id, ch1, 4, _zh("己"), "Real fix.",
                          machine_text="Real draft.")
    async with open_conn() as conn:
        pairs = await segments_svc.recent_edited_pairs(conn, novel_id, ch3, 10)
    assert pairs == [("Real draft.", "Real fix.")]


async def test_recent_pairs_truncate_and_limit():
    novel_id, (ch1, ch2, ch3) = await _seed_novel_with_chapters(3)
    long_before = "b" * 450
    long_after = "a" * 900
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), long_after,
                          machine_text=long_before,
                          edited_at="2026-08-04 09:00:30")
    for i in range(12):
        await _insert_segment(
            novel_id, ch2, i, _zh(chr(ord("一") + i)), f"Fix {i}.",
            machine_text=f"Draft {i}.",
            edited_at=f"2026-08-04 09:00:{11 - i:02d}",
        )
    async with open_conn() as conn:
        pairs = await segments_svc.recent_edited_pairs(conn, novel_id, ch3, 10)
    assert len(pairs) == 10
    assert pairs[0] == (long_before[:400], long_after[:400])
    assert pairs[1] == ("Draft 0.", "Fix 0.")


# ---------------------------------------------------------------------------
# prompt_inputs.fetch_style_edits merge
# ---------------------------------------------------------------------------


async def test_fetch_merges_segment_pairs_ahead_of_legacy():
    novel_id, (ch1, _ch2, ch3) = await _seed_novel_with_chapters(3)
    await _insert_style_edit(novel_id, "legacy before", "legacy after")
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Segment fix.",
                          machine_text="Segment draft.")
    async with open_conn() as conn:
        result = await prompt_inputs.fetch_style_edits(
            conn, novel_id, exclude_chapter_id=ch3
        )
    assert result == [
        ("Segment draft.", "Segment fix."),
        ("legacy before", "legacy after"),
    ]


async def test_fetch_dedupes_legacy_on_before_text_and_caps():
    novel_id, (ch1, _ch2, ch3) = await _seed_novel_with_chapters(3)
    # A legacy row repeating a segment pair's before side must not re-enter.
    await _insert_style_edit(novel_id, "Segment draft.", "stale legacy after")
    for i in range(12):
        await _insert_style_edit(novel_id, f"legacy b{i}", f"legacy a{i}")
    for i in range(3):
        await _insert_segment(
            novel_id, ch1, i, _zh(chr(ord("一") + i)), f"Segment fix {i}.",
            machine_text="Segment draft." if i == 0 else f"Segment draft {i}.",
            edited_at=f"2026-08-04 09:00:{9 - i:02d}",
        )
    async with open_conn() as conn:
        result = await prompt_inputs.fetch_style_edits(
            conn, novel_id, exclude_chapter_id=ch3
        )
    assert len(result) == prompt_inputs.STYLE_EDIT_LIMIT == 10
    # Segment pairs first (newest first), then legacy fill (newest id first).
    assert result[0] == ("Segment draft.", "Segment fix 0.")
    assert result[1] == ("Segment draft 1.", "Segment fix 1.")
    assert result[2] == ("Segment draft 2.", "Segment fix 2.")
    assert result[3] == ("legacy b11", "legacy a11")
    # The colliding legacy before-text never appears with its stale after.
    assert ("Segment draft.", "stale legacy after") not in result


async def test_fetch_legacy_overfetch_fills_past_window_dupes():
    """Legacy-side over-fetch (limit * 2): duplicate before-texts inside the
    newest window must not under-fill the block when older distinct rows
    exist. 12 rows where the newest repeats an existing before-text: a plain
    LIMIT-10 window would yield 9 pairs; the over-fetch fills all 10."""
    novel_id, (_ch1, _ch2, ch3) = await _seed_novel_with_chapters(3)
    for i in range(11):
        await _insert_style_edit(novel_id, f"legacy b{i}", f"legacy a{i}")
    await _insert_style_edit(novel_id, "legacy b10", "legacy a10 rev2")
    async with open_conn() as conn:
        result = await prompt_inputs.fetch_style_edits(
            conn, novel_id, exclude_chapter_id=ch3
        )
    assert len(result) == 10
    # Newest occurrence of the repeated before-text wins the dedupe...
    assert result[0] == ("legacy b10", "legacy a10 rev2")
    # ...and the slot its older twin freed is back-filled from beyond the
    # plain window.
    assert result[-1] == ("legacy b1", "legacy a1")


async def test_fetch_flag_off_returns_empty(monkeypatch):
    novel_id, (ch1, _ch2, ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Segment fix.",
                          machine_text="Segment draft.")
    await _insert_style_edit(novel_id, "legacy before", "legacy after")
    monkeypatch.setattr(config, "PROMPT_INCLUDE_STYLE_EDITS", False)
    async with open_conn() as conn:
        assert await prompt_inputs.fetch_style_edits(
            conn, novel_id, exclude_chapter_id=ch3
        ) == []


async def test_fetch_empty_sources_and_prompt_byte_identity():
    """Cache safety: with no pairs from either source the fetch returns []
    and build_prompt renders no USER STYLE PREFERENCES block, byte-identical
    to the pre-change prompt (no PROMPT_TEMPLATE_VERSION bump needed)."""
    novel_id, (_ch1, _ch2, ch3) = await _seed_novel_with_chapters(3)
    async with open_conn() as conn:
        assert await prompt_inputs.fetch_style_edits(
            conn, novel_id, exclude_chapter_id=ch3
        ) == []
    base_kwargs = dict(previous_context="Previous tail.", style_note="Voice.")
    legacy = build_prompt("章节正文。", "标题", [], **base_kwargs)
    with_none = build_prompt(
        "章节正文。", "标题", [], style_edits=None, **base_kwargs
    )
    with_empty = build_prompt(
        "章节正文。", "标题", [], style_edits=[], **base_kwargs
    )
    assert legacy == with_none == with_empty
    assert "USER STYLE PREFERENCES" not in legacy


# ---------------------------------------------------------------------------
# Worker integration
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


async def test_worker_passes_segment_pair_and_stamps_snapshot(monkeypatch):
    await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m", is_default=True,
    )
    novel_id, (ch1, _ch2, _ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Human corrected.",
                          machine_text="Machine draft.")
    pending_id = await _seed_pending_chapter(novel_id, 4)

    calls: list = []
    _stub_translate(monkeypatch, calls)
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, pending_id)

    assert calls, "translate_chapter was not called"
    assert calls[0]["style_edits"] == [("Machine draft.", "Human corrected.")]

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT prompt_config_snapshot FROM chapters WHERE id = ?",
            (pending_id,),
        )
        row = await cur.fetchone()
    snap = json.loads(row["prompt_config_snapshot"])
    assert snap["style_edits_included"] is True
    assert snap["flags"]["PROMPT_INCLUDE_STYLE_EDITS"] is True


async def test_worker_drops_pairs_already_in_approved_block(monkeypatch):
    """A segment pair whose AFTER text already rides this chapter's APPROVED
    TRANSLATIONS block (cross-chapter exact confirmed match) is dropped at
    the queue fetch site so the prompt carries the rendering once."""
    await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m", is_default=True,
    )
    novel_id, (ch1, _ch2, _ch3) = await _seed_novel_with_chapters(3)
    # Confirmed rendering of the pending chapter's own source paragraph:
    # lands in approved_pairs (machine == target, so no style pair from it).
    await _insert_segment(novel_id, ch1, 0, _zh("癸"), "Shared rendering.",
                          machine_text="Shared rendering.", status="confirmed",
                          edited_at=None, confirmed_at="2026-08-04 10:00:00")
    # Edited elsewhere to the SAME rendering: its style pair must drop.
    await _insert_segment(novel_id, ch1, 1, _zh("乙"), "Shared rendering.",
                          machine_text="Different draft.",
                          edited_at="2026-08-04 09:30:00")
    # An unrelated correction survives.
    await _insert_segment(novel_id, ch1, 2, _zh("丙"), "Kept correction.",
                          machine_text="Another draft.",
                          edited_at="2026-08-04 09:00:00")
    pending_id = await _seed_pending_chapter(novel_id, 4)

    calls: list = []
    _stub_translate(monkeypatch, calls)
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, pending_id)

    call = calls[0]
    assert [(zh, en) for _i, zh, en in call["approved_pairs"]] == [
        (_zh("癸"), "Shared rendering.")
    ]
    assert call["style_edits"] == [("Another draft.", "Kept correction.")]


async def test_single_config_patch_gates_block_and_snapshot_together(monkeypatch):
    """F8 (bug hunt 2026-08-04): the flags single-source through
    backend.config, so ONE patch site flips the fetch gate, the prompt
    content, and the snapshot's flags dict together. Pre-fix, queue.py and
    prompt_inputs.py held independent module copies: patching one left the
    other live and the snapshot could record a flag state the gates never
    applied."""
    await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m", is_default=True,
    )
    novel_id, (ch1, _ch2, _ch3) = await _seed_novel_with_chapters(3)
    await _insert_segment(novel_id, ch1, 0, _zh("乙"), "Human corrected.",
                          machine_text="Machine draft.")
    pending_id = await _seed_pending_chapter(novel_id, 4)

    monkeypatch.setattr(config, "PROMPT_INCLUDE_STYLE_EDITS", False)
    calls: list = []
    _stub_translate(monkeypatch, calls)
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, pending_id)

    # The gate actually applied: no style pairs reached the translator.
    assert calls[0]["style_edits"] == []
    # And the snapshot records exactly that state, from the same source.
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT prompt_config_snapshot FROM chapters WHERE id = ?",
            (pending_id,),
        )
        row = await cur.fetchone()
    snap = json.loads(row["prompt_config_snapshot"])
    assert snap["flags"]["PROMPT_INCLUDE_STYLE_EDITS"] is False
    assert snap["style_edits_included"] is False
