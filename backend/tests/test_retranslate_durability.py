"""CAT Phase 4: retranslation durability + pre-fill + approved-block tests.

The payoff contracts:
  - a confirmed/edited segment survives a retranslate VERBATIM (the worker
    merge is segments-authoritative for human rows); machine rows regenerate;
    human rows get machine_text refreshed with the new AI rendering;
  - observers run on the FINAL merged body (what the reader sees);
  - cross-chapter exact confirmed matches pre-fill as origin='tm_exact'
    (full-text verified against hash collisions, most-recently-confirmed
    wins);
  - a chapter with no store gets a fresh build at translate commit;
  - the count-drift path (welded machine output) still keeps human rows;
  - a failed translation leaves the store untouched;
  - prompt_config_snapshot records the APPROVED TRANSLATIONS flag + emit
    state, and the block's pairs reach the translator call (absent when the
    flag is off or no approved rows exist);
  - the refiner merge keeps human rows and materializes refined_text from
    the merged set; retry-refinement no longer nulls refined_text; stale
    refinement-stage drift observations are replaced on a clean
    re-refinement;
  - editor writes are refused for the duration of the refine window (the
    same mid-transition window get_segments refuses to rebuild in), while
    work confirmed before it still rides the merge verbatim.
"""

from __future__ import annotations

import json

import pytest

from backend import config
from backend.db import init_db, open_conn
from backend.models import TranslationResult
from backend.services import providers as providers_svc
from backend.services import queue as queue_svc
from backend.services import segments as segments_svc
from backend.services.segments import hash16
from backend.services.translators.base import (
    build_prompt,
    format_approved_translations,
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


async def _seed_translator_provider() -> int:
    p = await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m",
        is_default=True,
    )
    return p.id


async def _seed_refiner_provider() -> int:
    p = await providers_svc.create_provider(
        name="refiner", provider_type="gemini", model_id="r",
    )
    return p.id


# Chinese paragraphs must end in terminal punctuation or the mid-sentence
# pre-join welds them (services/segmentation.py).
def _zh(ch: str, length: int = 29) -> str:
    return ch * length + "。"


_SRC_PARAS = [_zh("甲"), _zh("乙"), _zh("丙")]
_SRC = "\n\n".join(_SRC_PARAS)


async def _seed_chapter(
    novel_id: int, chapter_num: int, original: str
) -> int:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, "
            "original_text, status, translate_queued) "
            "VALUES (?, ?, '第一章', ?, 'pending', 1)",
            (novel_id, chapter_num, original),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return chapter_id


async def _seed_novel(
    originals: list[str], refinement_provider_id: int | None = None
) -> tuple[int, list[int]]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, refinement_provider_id) "
            "VALUES ('N', 'paste', ?)",
            (refinement_provider_id,),
        )
        novel_id = cur.lastrowid
        await conn.commit()
    ids = []
    for i, original in enumerate(originals, start=1):
        ids.append(await _seed_chapter(novel_id, i, original))
    return novel_id, ids


def _machine_para(tag: str, i: int) -> str:
    return (
        f"Machine {tag} rendering number {i + 1} of this paragraph, padded "
        f"well beyond the refiner minimum draft length threshold."
    )


def _stub_translate(monkeypatch, tag: str = "one", calls: list | None = None,
                    body: str | None = None):
    """Stub queue.translate_chapter: one output paragraph per prompt-source
    paragraph (the 1:1 contract), tagged so retranslates are tellable."""
    async def _fake(chapter_zh, title_zh, glossary, **kwargs):
        if calls is not None:
            calls.append({"chapter_zh": chapter_zh, **kwargs})
        if body is not None:
            text = body
        else:
            n = len(chapter_zh.split("\n\n"))
            text = "\n\n".join(_machine_para(tag, i) for i in range(n))
        return TranslationResult(
            title_en="T", translated_text=text, new_terms=[],
        )
    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake)


async def _translate(novel_id: int, chapter_id: int) -> None:
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)


async def _requeue(novel_id: int, chapter_id: int) -> None:
    async with open_conn() as conn:
        reset = await queue_svc.reset_chapters_for_retranslate(
            conn, novel_id, [chapter_id]
        )
    assert reset == [chapter_id]


async def _segment_action(
    novel_id: int, chapter_num: int, idx: int, action: str,
    text: str | None = None,
) -> None:
    async with open_conn() as conn:
        payload = await segments_svc.get_segments(conn, novel_id, chapter_num)
        await conn.commit()
        seg = next(s for s in payload["segments"] if s["index"] == idx)
        await segments_svc.update_segment(
            conn, novel_id, chapter_num, idx,
            action=action, after_text=text,
            client_rev=payload["chapter_rev"],
            before_target_hash=seg["target_hash"],
        )
        await conn.commit()


async def _rows(chapter_id: int) -> list[dict]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT seg_index, source_text, source_hash, target_text, "
            "machine_text, status, origin, aligned "
            "FROM chapter_segments WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def _chapter(chapter_id: int) -> dict:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT * FROM chapters WHERE id = ?", (chapter_id,)
        )
        return dict(await cur.fetchone())


def _paras(text: str) -> list[str]:
    return [p for p in text.split("\n\n") if p.strip()]


# ---------------------------------------------------------------------------
# Fresh build at translate commit
# ---------------------------------------------------------------------------


async def test_translate_builds_fresh_store_with_llm_origin(monkeypatch):
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    assert ch["status"] == "done"
    rows = await _rows(chapter_id)
    assert len(rows) == 3
    assert all(r["status"] == "machine" and r["origin"] == "llm" for r in rows)
    assert all(r["aligned"] == 1 for r in rows)
    assert ch["segments_state"] == "ok"
    # I1: join(targets) == displayed body, and the rev stamp matches it.
    joined = "\n\n".join(r["target_text"] for r in rows if r["target_text"])
    assert joined == ch["translated_text"]
    assert ch["segments_rev"] == segments_svc.chapter_rev(ch["translated_text"])


# ---------------------------------------------------------------------------
# The payoff: human rows survive a retranslate verbatim
# ---------------------------------------------------------------------------


async def test_confirmed_and_edited_rows_survive_retranslate(monkeypatch):
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)

    confirmed_text = "MY CONFIRMED LINE STAYS EXACTLY AS WRITTEN."
    edited_text = "My edited line also survives the retranslate."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", confirmed_text)
    await _segment_action(novel_id, 1, 2, "save", edited_text)

    await _requeue(novel_id, chapter_id)
    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    assert ch["status"] == "done"
    paras = _paras(ch["translated_text"])
    # Machine paragraph regenerated; human paragraphs verbatim.
    assert paras == [_machine_para("two", 0), confirmed_text, edited_text]

    rows = await _rows(chapter_id)
    assert rows[0]["status"] == "machine" and rows[0]["origin"] == "llm"
    assert rows[0]["target_text"] == _machine_para("two", 0)
    assert rows[1]["status"] == "confirmed"
    assert rows[1]["target_text"] == confirmed_text
    # machine_text refreshed with the NEW AI rendering for that index.
    assert rows[1]["machine_text"] == _machine_para("two", 1)
    assert rows[2]["status"] == "edited"
    assert rows[2]["target_text"] == edited_text
    assert rows[2]["machine_text"] == _machine_para("two", 2)
    # I1 still holds after the merge.
    joined = "\n\n".join(r["target_text"] for r in rows if r["target_text"])
    assert joined == ch["translated_text"]


async def test_observers_run_on_merged_body(monkeypatch):
    """The confirmed human line drops a locked glossary term the machine
    output carries. If observers saw the raw machine body there would be no
    hit; on the MERGED body the missing-term observation must fire."""
    await _seed_translator_provider()
    src = "\n\n".join(["甲甲甲灵石甲甲甲甲甲。", _zh("乙"), _zh("丙")])
    novel_id, (chapter_id,) = await _seed_novel([src])
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO glossary_entries (novel_id, term_zh, term_en, "
            "category, locked, auto_detected) "
            "VALUES (?, '灵石', 'Spirit Stone', 'item', 1, 0)",
            (novel_id,),
        )
        await conn.commit()

    def _body_with_term(n: int) -> str:
        paras = ["The Spirit Stone paragraph number one is rendered here."]
        paras += [_machine_para("one", i) for i in range(1, n)]
        return "\n\n".join(paras)

    _stub_translate(monkeypatch, body=_body_with_term(3))
    await _translate(novel_id, chapter_id)
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT kind FROM chapter_observations WHERE chapter_id = ?",
            (chapter_id,),
        )
        kinds = [r["kind"] for r in await cur.fetchall()]
    assert "missing_glossary_term" not in kinds

    await _segment_action(
        novel_id, 1, 0, "save_and_confirm",
        "A human paragraph that dropped the gem name entirely.",
    )
    await _requeue(novel_id, chapter_id)
    _stub_translate(monkeypatch, body=_body_with_term(3))
    await _translate(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    assert "Spirit Stone" not in _paras(ch["translated_text"])[0]
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT kind FROM chapter_observations WHERE chapter_id = ?",
            (chapter_id,),
        )
        kinds = [r["kind"] for r in await cur.fetchall()]
    assert "missing_glossary_term" in kinds


# ---------------------------------------------------------------------------
# Cross-chapter exact-hash prefill (origin 'tm_exact')
# ---------------------------------------------------------------------------

_SHARED = "共" * 24 + "享享享享。"


async def test_prefill_lands_with_tm_exact_origin(monkeypatch):
    await _seed_translator_provider()
    src_a = "\n\n".join([_SHARED, _zh("乙")])
    src_b = "\n\n".join([_zh("丁"), _SHARED, _zh("戊")])
    novel_id, (ch_a, ch_b) = await _seed_novel([src_a, src_b])

    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, ch_a)
    canon = "THE CANONICAL CONFIRMED RENDERING OF THE SHARED LINE."
    await _segment_action(novel_id, 1, 0, "save_and_confirm", canon)

    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, ch_b)

    ch = await _chapter(ch_b)
    rows = await _rows(ch_b)
    assert rows[1]["origin"] == "tm_exact"
    assert rows[1]["status"] == "machine"  # status stays machine (TM chip)
    assert rows[1]["target_text"] == canon
    # The fresh AI rendering is preserved for revert-to-AI.
    assert rows[1]["machine_text"] == _machine_para("two", 1)
    assert _paras(ch["translated_text"])[1] == canon
    # Non-shared rows are plain machine output.
    assert rows[0]["origin"] == "llm"
    assert rows[2]["origin"] == "llm"


async def test_prefill_most_recently_confirmed_wins(monkeypatch):
    await _seed_translator_provider()
    src_a = "\n\n".join([_SHARED, _zh("乙")])
    src_c = "\n\n".join([_SHARED, _zh("丙")])
    src_b = "\n\n".join([_SHARED, _zh("丁")])
    novel_id, (ch_a, ch_c, ch_b) = await _seed_novel([src_a, src_c, src_b])

    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, ch_a)
    await _translate(novel_id, ch_c)
    await _segment_action(novel_id, 1, 0, "save_and_confirm", "OLD RENDERING.")
    await _segment_action(novel_id, 2, 0, "save_and_confirm", "NEW RENDERING.")
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapter_segments SET confirmed_at = '2026-01-01 00:00:00' "
            "WHERE chapter_id = ? AND seg_index = 0", (ch_a,),
        )
        await conn.execute(
            "UPDATE chapter_segments SET confirmed_at = '2026-02-01 00:00:00' "
            "WHERE chapter_id = ? AND seg_index = 0", (ch_c,),
        )
        await conn.commit()

    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, ch_b)
    rows = await _rows(ch_b)
    assert rows[0]["origin"] == "tm_exact"
    assert rows[0]["target_text"] == "NEW RENDERING."


async def test_prefill_rejects_hash_collision(monkeypatch):
    """A confirmed row with the same 16-hex source_hash but DIFFERENT full
    source text must not pre-fill (full-text equality check)."""
    await _seed_translator_provider()
    src_a = "\n\n".join([_zh("甲"), _zh("乙")])
    src_b = "\n\n".join([_SHARED, _zh("戊")])
    novel_id, (ch_a, ch_b) = await _seed_novel([src_a, src_b])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, ch_a)
    # Forge a confirmed row in chapter A carrying B's shared-paragraph hash
    # but a different source text (a simulated 16-hex collision).
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO chapter_segments (novel_id, chapter_id, seg_index, "
            "source_text, source_hash, target_text, machine_text, status, "
            "origin, aligned, confirmed_at) "
            "VALUES (?, ?, 99, '完全不同的原文。', ?, 'WRONG RENDERING.', "
            "'WRONG RENDERING.', 'confirmed', 'human', 1, '2026-03-01')",
            (novel_id, ch_a, hash16(_SHARED)),
        )
        await conn.commit()

    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, ch_b)
    rows = await _rows(ch_b)
    assert rows[0]["origin"] == "llm"
    assert rows[0]["target_text"] == _machine_para("two", 0)


# ---------------------------------------------------------------------------
# Count drift (welded machine output) keeps human rows
# ---------------------------------------------------------------------------


async def test_count_drift_path_keeps_human_rows(monkeypatch):
    await _seed_translator_provider()
    src = "\n\n".join([_zh("甲"), _zh("乙"), _zh("丙"), "丁丁丁。"])
    novel_id, (chapter_id,) = await _seed_novel([src])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)

    kept = "THE KEPT CONFIRMED LINE STAYS EXACTLY."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", kept)

    await _requeue(novel_id, chapter_id)
    # 3 machine paragraphs for a 4-paragraph source: the recorded drift seam
    # (a fixup weld). The tiny 4th source paragraph is the cheapest delete,
    # so the full alignment path pins it as the empty slot.
    welded = "\n\n".join(
        f"Welded machine two paragraph {i + 1} with plenty of length in it."
        for i in range(3)
    )
    _stub_translate(monkeypatch, body=welded)
    await _translate(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    rows = await _rows(chapter_id)
    assert rows[1]["status"] == "confirmed"
    assert rows[1]["target_text"] == kept
    assert kept in ch["translated_text"]
    assert ch["segments_state"] == "partial"
    assert any(r["aligned"] == 0 for r in rows)
    # I1 (empty-skip join) still holds.
    joined = "\n\n".join(r["target_text"] for r in rows if r["target_text"])
    assert joined == ch["translated_text"]
    # The drift observation describes the final merged body.
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT excerpt FROM chapter_observations "
            "WHERE chapter_id = ? AND kind = 'paragraph_count_drift'",
            (chapter_id,),
        )
        excerpts = [r["excerpt"] for r in await cur.fetchall()]
    assert any("(translation)" in e for e in excerpts)


# ---------------------------------------------------------------------------
# Failed translation leaves the store untouched
# ---------------------------------------------------------------------------


async def test_failed_translation_leaves_store_untouched(monkeypatch):
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)
    await _segment_action(novel_id, 1, 1, "save_and_confirm", "SAFE LINE.")
    before_rows = await _rows(chapter_id)
    before_body = (await _chapter(chapter_id))["translated_text"]

    await _requeue(novel_id, chapter_id)

    async def _boom(*a, **kw):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr("backend.services.queue.translate_chapter", _boom)
    await _translate(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    assert ch["status"] == "error"
    assert ch["translated_text"] == before_body
    assert await _rows(chapter_id) == before_rows


# ---------------------------------------------------------------------------
# APPROVED TRANSLATIONS block + snapshot provenance
# ---------------------------------------------------------------------------


async def test_snapshot_records_approved_flag_and_included_key(monkeypatch):
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)
    snap = json.loads((await _chapter(chapter_id))["prompt_config_snapshot"])
    assert snap["approved_translations_included"] is False
    assert snap["flags"]["PROMPT_INCLUDE_APPROVED_TRANSLATIONS"] is True

    await _segment_action(novel_id, 1, 1, "save_and_confirm", "APPROVED LINE.")
    await _requeue(novel_id, chapter_id)
    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, chapter_id)
    snap = json.loads((await _chapter(chapter_id))["prompt_config_snapshot"])
    assert snap["approved_translations_included"] is True
    assert snap["flags"]["PROMPT_INCLUDE_APPROVED_TRANSLATIONS"] is True


async def test_approved_pairs_reach_the_translator_call(monkeypatch):
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    calls: list = []
    _stub_translate(monkeypatch, "one", calls=calls)
    await _translate(novel_id, chapter_id)
    assert calls[-1]["approved_pairs"] is None

    await _segment_action(novel_id, 1, 1, "save_and_confirm", "APPROVED LINE.")
    await _requeue(novel_id, chapter_id)
    _stub_translate(monkeypatch, "two", calls=calls)
    await _translate(novel_id, chapter_id)
    pairs = calls[-1]["approved_pairs"]
    assert pairs == [(1, _SRC_PARAS[1], "APPROVED LINE.")]


async def test_approved_pairs_absent_when_flag_off(monkeypatch):
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)
    await _segment_action(novel_id, 1, 1, "save_and_confirm", "APPROVED LINE.")
    await _requeue(novel_id, chapter_id)

    monkeypatch.setattr(
        config, "PROMPT_INCLUDE_APPROVED_TRANSLATIONS", False
    )
    calls: list = []
    _stub_translate(monkeypatch, "two", calls=calls)
    await _translate(novel_id, chapter_id)
    assert calls[-1]["approved_pairs"] is None
    snap = json.loads((await _chapter(chapter_id))["prompt_config_snapshot"])
    assert snap["approved_translations_included"] is False
    assert snap["flags"]["PROMPT_INCLUDE_APPROVED_TRANSLATIONS"] is False
    # The deterministic merge still enforces durability with the block off.
    rows = await _rows(chapter_id)
    assert rows[1]["target_text"] == "APPROVED LINE."


async def test_build_prompt_carries_approved_block():
    prompt = build_prompt(
        "原文。", None, [],
        approved_pairs=[(2, "共享原文。", "The approved English line.")],
    )
    assert "APPROVED TRANSLATIONS" in prompt
    assert "Paragraph 3:" in prompt  # 1-based ordinal, matches the editor
    assert "The approved English line." in prompt
    # Absent entirely when there are no pairs.
    bare = build_prompt("原文。", None, [])
    assert "APPROVED TRANSLATIONS" not in bare
    assert format_approved_translations(None) == ""
    assert format_approved_translations([]) == ""


async def test_approved_block_caps():
    pairs = [(i, "源" * 30, "target text " * 5) for i in range(60)]
    block = format_approved_translations(pairs)
    # format renders whatever it is given; the cap lives in
    # approved_prompt_pairs. Sanity: every listed pair renders.
    assert block.count("Paragraph ") == 60


async def test_approved_prompt_pairs_caps_and_orders():
    await _seed_translator_provider()
    src_paras = [_zh(c) for c in "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥中英日韩德法俄意西葡希廷伯尼泰越印马菲"]
    src = "\n\n".join(src_paras)
    novel_id, (chapter_id,) = await _seed_novel([src])
    async with open_conn() as conn:
        for i, p in enumerate(src_paras):
            await conn.execute(
                "INSERT INTO chapter_segments (novel_id, chapter_id, "
                "seg_index, source_text, source_hash, target_text, "
                "machine_text, status, origin, aligned) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'edited', 'human', 1)",
                (novel_id, chapter_id, i, p, hash16(p),
                 f"Edited line {i}.", "machine"),
            )
        await conn.commit()
        pairs = await segments_svc.approved_prompt_pairs(
            conn, novel_id, chapter_id, src_paras
        )
    assert len(pairs) == 30  # max_pairs cap
    assert [p[0] for p in pairs] == list(range(30))  # index-ordered


# ---------------------------------------------------------------------------
# Refiner merge
# ---------------------------------------------------------------------------


def _stub_refine(monkeypatch, tag: str = "r1"):
    async def _fake(draft, provider, glossary=None,
                    expected_paragraph_count=None, **kw):
        n = len([p for p in draft.split("\n\n") if p.strip()])
        return "\n\n".join(
            f"Refined {tag} paragraph number {i + 1} of the polished body."
            for i in range(n)
        )
    monkeypatch.setattr("backend.services.queue.refine_chapter", _fake)


async def _refine(novel_id: int, chapter_id: int) -> None:
    async with open_conn() as conn:
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)


async def test_refiner_merge_keeps_human_rows(monkeypatch):
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, (chapter_id,) = await _seed_novel(
        [_SRC], refinement_provider_id=refiner_id
    )
    _stub_translate(monkeypatch, "one")
    _stub_refine(monkeypatch, "r1")
    await _translate(novel_id, chapter_id)
    await _refine(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    assert ch["refinement_status"] == "done"
    # Store mirrors the DISPLAYED (refined) body after the refine merge.
    rows = await _rows(chapter_id)
    assert all(r["origin"] == "llm_refined" for r in rows)
    joined = "\n\n".join(r["target_text"] for r in rows if r["target_text"])
    assert joined == ch["refined_text"]

    # Confirm a segment on the refined variant, then re-refine.
    human = "HUMAN LINE ON THE REFINED VARIANT."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", human)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'pending' WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
    _stub_refine(monkeypatch, "r2")
    await _refine(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    assert ch["refinement_status"] == "done"
    paras = _paras(ch["refined_text"])
    assert paras[1] == human  # human row untouched by the refine merge
    assert paras[0] == "Refined r2 paragraph number 1 of the polished body."
    rows = await _rows(chapter_id)
    assert rows[1]["status"] == "confirmed"
    assert rows[1]["target_text"] == human
    # The refiner's rewrite of the protected row is discarded from the body
    # but recorded as the AI suggestion.
    assert rows[1]["machine_text"] == (
        "Refined r2 paragraph number 2 of the polished body."
    )
    joined = "\n\n".join(r["target_text"] for r in rows if r["target_text"])
    assert joined == ch["refined_text"]


async def test_retry_window_editor_get_leaves_confirmed_rows(monkeypatch):
    """Mid-retry editor GET must not rebuild the store against a
    mid-transition body: refined stays displayed (presence keying) and
    confirmed rows keep their text (the pre-fix chain flipped the display
    to draft and text-authoritatively clobbered confirmed targets)."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, (chapter_id,) = await _seed_novel(
        [_SRC], refinement_provider_id=refiner_id
    )
    _stub_translate(monkeypatch, "one")
    _stub_refine(monkeypatch, "r1")
    await _translate(novel_id, chapter_id)
    await _refine(novel_id, chapter_id)
    human = "CONFIRMED ON THE REFINED VARIANT."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", human)
    before_rows = await _rows(chapter_id)
    refined_before = (await _chapter(chapter_id))["refined_text"]

    # Enter the retry window (retry_refinement semantics: status flips to
    # pending, refined_text retained).
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'pending', "
            "refined_at = NULL WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
        payload = await segments_svc.get_segments(conn, novel_id, 1)
        await conn.commit()
    # Displayed body stays refined; the GET served stored rows untouched.
    assert payload["variant"] == "refined"
    assert payload["chapter_rev"] == segments_svc.chapter_rev(refined_before)
    assert await _rows(chapter_id) == before_rows
    seg = next(s for s in payload["segments"] if s["index"] == 1)
    assert seg["status"] == "confirmed"
    assert seg["target_text"] == human


async def test_failed_retry_keeps_refined_display_and_store(monkeypatch):
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, (chapter_id,) = await _seed_novel(
        [_SRC], refinement_provider_id=refiner_id
    )
    _stub_translate(monkeypatch, "one")
    _stub_refine(monkeypatch, "r1")
    await _translate(novel_id, chapter_id)
    await _refine(novel_id, chapter_id)
    human = "CONFIRMED SURVIVES A FAILED RETRY."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", human)
    before_rows = await _rows(chapter_id)
    refined_before = (await _chapter(chapter_id))["refined_text"]

    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'pending', "
            "refined_at = NULL WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()

    async def _boom(*a, **kw):
        raise RuntimeError("refiner exploded")
    monkeypatch.setattr("backend.services.queue.refine_chapter", _boom)
    await _refine(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    assert ch["refinement_status"] == "error"  # failure surfaced (retry UI)
    assert ch["refinement_error"]
    assert ch["refined_text"] == refined_before  # polish not vanished
    assert segments_svc.displayed_body(ch) == ("refined", refined_before)
    assert await _rows(chapter_id) == before_rows
    # And the store still mirrors the displayed body (stable state).
    joined = "\n\n".join(
        r["target_text"] for r in before_rows if r["target_text"]
    )
    assert joined == refined_before


async def test_revert_machine_reaches_ai_text_behind_tm_prefill(monkeypatch):
    await _seed_translator_provider()
    src_a = "\n\n".join([_SHARED, _zh("乙")])
    src_b = "\n\n".join([_zh("丁"), _SHARED, _zh("戊")])
    novel_id, (ch_a, ch_b) = await _seed_novel([src_a, src_b])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, ch_a)
    await _segment_action(novel_id, 1, 0, "save_and_confirm", "CANON LINE.")
    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, ch_b)
    rows = await _rows(ch_b)
    assert rows[1]["origin"] == "tm_exact"

    # Revert the tm_exact machine row: target swaps to the fresh AI text.
    await _segment_action(novel_id, 2, 1, "revert_machine")
    rows = await _rows(ch_b)
    assert rows[1]["status"] == "machine"
    assert rows[1]["origin"] == "llm"
    assert rows[1]["target_text"] == _machine_para("two", 1)
    ch = await _chapter(ch_b)
    assert _paras(ch["translated_text"])[1] == _machine_para("two", 1)

    # Second revert: target already equals machine_text -> 400.
    with pytest.raises(segments_svc.SegmentActionError):
        await _segment_action(novel_id, 2, 1, "revert_machine")


async def test_anchor_rejects_source_hash_collision(monkeypatch):
    """A human row whose source_hash matches a source paragraph but whose
    FULL source_text differs (forged 16-hex collision) must not silently
    anchor: the merge retains every row and stamps 'unaligned'."""
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)
    human = "CONFIRMED LINE ON A FORGED ROW."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", human)
    # Forge the collision: same hash, different full source text.
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapter_segments SET source_text = '完全不同的原文。' "
            "WHERE chapter_id = ? AND seg_index = 1",
            (chapter_id,),
        )
        await conn.commit()

    await _requeue(novel_id, chapter_id)
    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, chapter_id)

    ch = await _chapter(chapter_id)
    assert ch["segments_state"] == "unaligned"
    rows = await _rows(chapter_id)
    kept = next(r for r in rows if r["status"] == "confirmed")
    assert kept["target_text"] == human  # retained, not clobbered
    assert all(r["aligned"] == 0 for r in rows)
    # The machine output is the displayed body (retention fallback).
    assert _paras(ch["translated_text"]) == [
        _machine_para("two", i) for i in range(3)
    ]


async def test_source_recipe_unified_for_marker_headings(monkeypatch):
    """SEGMENTATION_VERSION 2: every chapter_segments writer derives source
    paragraphs via chapter_source_paragraphs (marker strip composed in).
    Before the unification, a first line matching parser's broad
    _TITLE_PREFIX_RE (spaced 第 N 章) but not segmentation's tight
    _HEADING_RE, while carrying an update marker, produced DIFFERENT seg-0
    sources per writer and sent the chapter retain-all unaligned."""
    from backend.services.segmentation import (
        chapter_source_paragraphs,
        effective_source_paragraphs,
    )
    heading = "第 12 章 序幕（第四更！）"
    src = "\n\n".join([heading, _zh("甲"), _zh("乙"), _zh("丙")])
    canonical = chapter_source_paragraphs(src)
    # The bare recipe diverges on this class; the canonical one strips the
    # marker for every writer.
    assert canonical != effective_source_paragraphs(src)
    assert all("第四更" not in p for p in canonical)

    # End to end: worker-built rows and the lazy rebuild now agree, so a
    # version-stamp rebuild re-anchors the human row instead of retaining.
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([src])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)
    rows = await _rows(chapter_id)
    assert [r["source_text"] for r in rows] == canonical
    human = "CONFIRMED THROUGH THE RECIPE SEAM."
    await _segment_action(novel_id, 1, 0, "save_and_confirm", human)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET segmentation_version = 1 WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
        payload = await segments_svc.get_segments(conn, novel_id, 1)
        await conn.commit()
    assert payload["segments_state"] == "ok"
    seg0 = next(s for s in payload["segments"] if s["index"] == 0)
    assert seg0["status"] == "confirmed"
    assert seg0["target_text"] == human
    assert seg0["source_text"] == canonical[0]


async def test_version_bump_rebuild_preserves_human_rows(monkeypatch):
    """A SEGMENTATION_VERSION bump forces a lazy rebuild on next open; for
    chapters the recipe change does not affect, the hash+text re-anchor
    must carry human rows through untouched."""
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)
    human = "SURVIVES THE VERSION BUMP."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", human)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET segmentation_version = 1 WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
        payload = await segments_svc.get_segments(conn, novel_id, 1)
        await conn.commit()
    assert payload["segments_state"] == "ok"
    seg = next(s for s in payload["segments"] if s["index"] == 1)
    assert seg["status"] == "confirmed"
    assert seg["target_text"] == human


async def test_save_normalizes_pasted_blank_lines(monkeypatch):
    """A pasted blank-line run collapses to a single newline on save (a
    segment is ONE paragraph), so the body's paragraph count is preserved
    and the next retranslate fires no drift observation."""
    await _seed_translator_provider()
    novel_id, (chapter_id,) = await _seed_novel([_SRC])
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)
    pasted = "Pasted line one.\r\n\r\n\r\nPasted line two."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", pasted)
    rows = await _rows(chapter_id)
    assert rows[1]["target_text"] == "Pasted line one.\nPasted line two."
    ch = await _chapter(chapter_id)
    assert len(_paras(ch["translated_text"])) == 3  # count preserved

    await _requeue(novel_id, chapter_id)
    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, chapter_id)
    rows = await _rows(chapter_id)
    assert rows[1]["target_text"] == "Pasted line one.\nPasted line two."
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM chapter_observations "
            "WHERE chapter_id = ? AND kind = 'paragraph_count_drift'",
            (chapter_id,),
        )
        assert (await cur.fetchone())["n"] == 0


async def test_prefill_survives_refinement(monkeypatch):
    """A tm_exact rendering on a machine row must ride through the refiner
    pass: the refine merge re-applies the prefill, keeping the confirmed
    cross-chapter rendering (the consistency point of prefill) while the
    refiner's own wording lands in machine_text."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    src_a = "\n\n".join([_SHARED, _zh("乙")])
    src_b = "\n\n".join([_zh("丁"), _SHARED, _zh("戊")])
    novel_id, (ch_a, ch_b) = await _seed_novel(
        [src_a, src_b], refinement_provider_id=refiner_id
    )
    _stub_translate(monkeypatch, "one")
    _stub_refine(monkeypatch, "ra")
    await _translate(novel_id, ch_a)
    await _refine(novel_id, ch_a)
    canon = "CANON LINE SURVIVES REFINEMENT."
    await _segment_action(novel_id, 1, 0, "save_and_confirm", canon)

    _stub_translate(monkeypatch, "two")
    await _translate(novel_id, ch_b)
    rows = await _rows(ch_b)
    assert rows[1]["origin"] == "tm_exact"
    _stub_refine(monkeypatch, "rb")
    await _refine(novel_id, ch_b)

    ch = await _chapter(ch_b)
    assert ch["refinement_status"] == "done"
    rows = await _rows(ch_b)
    assert rows[1]["origin"] == "tm_exact"
    assert rows[1]["status"] == "machine"
    assert rows[1]["target_text"] == canon
    assert rows[1]["machine_text"] == (
        "Refined rb paragraph number 2 of the polished body."
    )
    assert _paras(ch["refined_text"])[1] == canon


async def test_refine_window_refuses_writes_and_keeps_confirmed_work(
    monkeypatch,
):
    """An editor write attempted WHILE the refiner call is in flight is
    REFUSED (409 'chapter_translating'), the same window get_segments
    refuses to rebuild in. The worker holds no write transaction across the
    LLM await, so a save whose read straddles the refine commit would
    rematerialize the body from a stale variant and then be discarded by the
    next text-authoritative rebuild: a 200 the paragraph does not survive.
    Refusing it keeps the failure honest and visible.

    Work confirmed BEFORE the window still rides the merge verbatim (the
    merge re-reads human rows inside its commit transaction), which is the
    durability payoff the window guard must not cost."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, (chapter_id,) = await _seed_novel(
        [_SRC], refinement_provider_id=refiner_id
    )
    _stub_translate(monkeypatch, "one")
    _stub_refine(monkeypatch, "r1")
    await _translate(novel_id, chapter_id)
    await _refine(novel_id, chapter_id)

    confirmed = "CONFIRMED BEFORE THE REFINE WINDOW."
    await _segment_action(novel_id, 1, 1, "save_and_confirm", confirmed)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'pending' WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()

    refused: list[str] = []

    async def _refine_with_interleave(draft, provider, glossary=None,
                                      expected_paragraph_count=None, **kw):
        with pytest.raises(segments_svc.SegmentStaleError) as excinfo:
            await _segment_action(
                novel_id, 1, 1, "save", "MID WINDOW EDITOR WRITE."
            )
        refused.append(excinfo.value.kind)
        n = len([p for p in draft.split("\n\n") if p.strip()])
        return "\n\n".join(
            f"Refined ix paragraph number {i + 1} of the polished body."
            for i in range(n)
        )
    monkeypatch.setattr(
        "backend.services.queue.refine_chapter", _refine_with_interleave
    )
    await _refine(novel_id, chapter_id)

    assert refused == ["chapter_translating"]
    ch = await _chapter(chapter_id)
    assert ch["refinement_status"] == "done"
    # The refused write left no trace; the pre-window confirmation rode the
    # merge verbatim.
    assert _paras(ch["refined_text"])[1] == confirmed
    rows = await _rows(chapter_id)
    assert rows[1]["status"] == "confirmed"
    assert rows[1]["target_text"] == confirmed
    joined = "\n\n".join(r["target_text"] for r in rows if r["target_text"])
    assert joined == ch["refined_text"]


async def test_stale_refinement_drift_rows_replaced(monkeypatch):
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, (chapter_id,) = await _seed_novel(
        [_SRC], refinement_provider_id=refiner_id
    )
    _stub_translate(monkeypatch, "one")
    await _translate(novel_id, chapter_id)
    async with open_conn() as conn:
        await conn.executemany(
            "INSERT INTO chapter_observations "
            "(chapter_id, kind, severity, paragraph_index, excerpt) "
            "VALUES (?, 'paragraph_count_drift', 'info', NULL, ?)",
            [
                (chapter_id,
                 "Paragraph count drift (refinement): stale old row."),
                (chapter_id,
                 "Paragraph count drift (translation): keep this one."),
            ],
        )
        await conn.commit()
    _stub_refine(monkeypatch, "r1")
    await _refine(novel_id, chapter_id)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT excerpt FROM chapter_observations "
            "WHERE chapter_id = ? AND kind = 'paragraph_count_drift'",
            (chapter_id,),
        )
        excerpts = [r["excerpt"] for r in await cur.fetchall()]
    # The clean re-refinement removed the stale refinement-stage row and,
    # being clean, inserted no replacement; the translation-stage row stays.
    assert excerpts == ["Paragraph count drift (translation): keep this one."]


@pytest.fixture
def quiet_app(monkeypatch):
    async def _no_probe(default_provider):
        return None
    async def _no_drain():
        return None
    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)
    from backend.main import app
    return app


async def test_retry_refinement_keeps_refined_text(quiet_app, monkeypatch):
    """The retry window must not vanish confirmed work from the displayed
    body: refined_text is KEPT (only refinement_error / refined_at clear)."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, (chapter_id,) = await _seed_novel(
        [_SRC], refinement_provider_id=refiner_id
    )
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = 'd', "
            "translate_queued = 0, refinement_status = 'error', "
            "refinement_error = 'old error', refined_text = 'KEEP ME', "
            "refined_at = '2026-01-01' WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
    monkeypatch.setattr(queue_svc, "spawn_refine_worker", lambda *a: None)

    from fastapi.testclient import TestClient
    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/retry-refinement"
        )
    assert resp.status_code == 200, resp.text

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT refinement_status, refinement_error, refined_text, "
            "refined_at FROM chapters WHERE id = ?", (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "pending"
    assert row["refinement_error"] is None
    assert row["refined_text"] == "KEEP ME"
    assert row["refined_at"] is None
