"""Edit-mode consistency aid (services/consistency.py).

Read-only, deterministic cross-chapter drift detection that powers the
reader's edit-mode consistency rail:

1. Fuzzy tier: a current source paragraph whose near-duplicate elsewhere in
   the novel was translated DIFFERENTLY surfaces; an identical rendering does
   not. Exact (script-folded) source matches are marked `exact`.
2. Current rendering is read LIVE from the displayed body (translated_text, or
   refined_text when refinement is done), never from the chapter's own
   tm_segments rows (which go stale after a manual edit or refinement).
3. Glossary tier: locked glossary terms present in the source but absent from
   the translation flag; unlocked entries never flag.
4. Status contract: missing chapter -> None (route 404s); not-yet-translated
   -> "not_translated"; otherwise "ok".
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.services import consistency as cons
from backend.services import tm as tm_svc


@pytest.fixture(autouse=True)
def _reset_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    yield


async def _seed_novel(chapters, glossary=None):
    """chapters: list of dicts with keys
         chapter_num, original_text, translated_text,
         optional refined_text / refinement_status / status,
         optional tm: list of (paragraph_index, source_text, target_text)
                      -> explicit tm_segments rows for THIS chapter.
       When `tm` is omitted, rows are auto-derived by aligning
       original_text<->translated_text (mirrors the queue worker).
       glossary: list of dicts (term_zh, term_en, locked, category).
       Returns novel_id.
    """
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, source_url) VALUES (?, ?, NULL)",
            ("TestNovel", "paste"),
        )
        novel_id = cur.lastrowid
        for ch in chapters:
            cur = await conn.execute(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, original_text, translated_text, "
                " refined_text, refinement_status, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    novel_id,
                    ch["chapter_num"],
                    ch["original_text"],
                    ch.get("translated_text"),
                    ch.get("refined_text"),
                    ch.get("refinement_status", "none"),
                    ch.get("status", "done"),
                ),
            )
            chapter_id = cur.lastrowid
            rows = ch.get("tm")
            if rows is None and ch.get("translated_text"):
                pairs = tm_svc.align_paragraphs(
                    ch["original_text"], ch["translated_text"]
                )
                rows = (
                    [(p.paragraph_index, p.source_text, p.target_text) for p in pairs]
                    if pairs
                    else []
                )
            for pidx, src, tgt in rows or []:
                await conn.execute(
                    "INSERT INTO tm_segments "
                    "(novel_id, chapter_id, paragraph_index, source_text, "
                    " target_text, source_hash) VALUES (?, ?, ?, ?, ?, ?)",
                    (novel_id, chapter_id, pidx, src, tgt, tm_svc._hash_source(src)),
                )
        for g in glossary or []:
            await conn.execute(
                "INSERT INTO glossary_entries "
                "(novel_id, term_zh, term_en, category, locked, auto_detected) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    novel_id,
                    g["term_zh"],
                    g["term_en"],
                    g.get("category", "other"),
                    1 if g.get("locked") else 0,
                    0 if g.get("locked") else 1,
                ),
            )
        await conn.commit()
    return novel_id


async def _run(novel_id, chapter_num):
    async with open_conn() as conn:
        return await cons.consistency_for_chapter(conn, novel_id, chapter_num)


# ---- Fuzzy tier ---------------------------------------------------------


async def test_exact_source_divergent_rendering_surfaces():
    """An identical source paragraph rendered two ways across chapters is
    drift: it surfaces on the later chapter, pointing at the earlier one."""
    novel = await _seed_novel([
        {"chapter_num": 1, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spirit Treasure."},
        {"chapter_num": 2, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spiritual Treasure."},
    ])
    res = await _run(novel, 2)
    assert res.status == "ok"
    assert len(res.matches) == 1
    m = res.matches[0]
    assert m.paragraph_index == 0
    assert m.current_rendering == "He took out a Spiritual Treasure."
    assert len(m.others) == 1
    o = m.others[0]
    assert o.chapter_num == 1
    assert o.target_text == "He took out a Spirit Treasure."
    assert o.exact is True
    assert o.similarity >= 0.99


async def test_identical_rendering_not_flagged():
    """Same source, SAME rendering across chapters is already consistent;
    nothing surfaces."""
    novel = await _seed_novel([
        {"chapter_num": 1, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spirit Treasure."},
        {"chapter_num": 2, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spirit Treasure."},
    ])
    res = await _run(novel, 2)
    assert res.status == "ok"
    assert res.matches == []


async def test_fuzzy_near_duplicate_divergent_surfaces():
    """A near-identical source (one differing char) translated differently
    surfaces as a non-exact match above threshold."""
    novel = await _seed_novel([
        {"chapter_num": 1,
         "original_text": "他缓缓地取出了一件古老的灵宝准备应敌。",
         "translated_text": "He slowly took out an ancient Spirit Treasure to face the enemy."},
        {"chapter_num": 2,
         "original_text": "他缓缓地取出了一件古旧的灵宝准备应敌。",
         "translated_text": "He slowly drew an old Spiritual Treasure to meet the foe."},
    ])
    res = await _run(novel, 2)
    assert res.status == "ok"
    assert len(res.matches) == 1
    o = res.matches[0].others[0]
    assert o.chapter_num == 1
    assert o.exact is False
    assert 0.90 <= o.similarity < 1.0


async def test_below_threshold_not_surfaced():
    """Genuinely different source paragraphs do not fuzzy-match."""
    novel = await _seed_novel([
        {"chapter_num": 1, "original_text": "清晨的阳光洒满了整座山谷。",
         "translated_text": "Morning light filled the whole valley."},
        {"chapter_num": 2, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spiritual Treasure."},
    ])
    res = await _run(novel, 2)
    assert res.status == "ok"
    assert res.matches == []


async def test_current_rendering_read_live_not_from_stale_tm():
    """The divergence check uses the LIVE chapter body, not the chapter's own
    tm_segments rows. A stale TM target must not cause a false positive when
    the live rendering already matches the other chapter."""
    novel = await _seed_novel([
        {"chapter_num": 1, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spirit Treasure."},
        # Live body now agrees with ch1, but the stale TM row for ch2 still
        # holds the old divergent rendering.
        {"chapter_num": 2, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spirit Treasure.",
         "tm": [(0, "他取出了一件灵宝。", "He took out a Spiritual Treasure.")]},
    ])
    res = await _run(novel, 2)
    assert res.matches == [], "stale ch2 TM target must not be used as the current rendering"


async def test_refined_variant_is_the_current_rendering():
    """When refinement is done, the displayed (refined) body is the current
    rendering used for divergence."""
    novel = await _seed_novel([
        {"chapter_num": 1, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spirit Treasure."},
        {"chapter_num": 2, "original_text": "他取出了一件灵宝。",
         "translated_text": "He took out a Spirit Treasure.",  # draft agrees with ch1
         "refined_text": "He produced a Spiritual Treasure.",   # refined diverges
         "refinement_status": "done"},
    ])
    res = await _run(novel, 2)
    assert len(res.matches) == 1
    assert res.matches[0].current_rendering == "He produced a Spiritual Treasure."
    assert res.matches[0].others[0].target_text == "He took out a Spirit Treasure."


# ---- Glossary tier ------------------------------------------------------


async def test_locked_term_missing_from_translation_flags():
    novel = await _seed_novel(
        [
            {"chapter_num": 1, "original_text": "他突破到了金丹境界。",
             "translated_text": "He broke through to a new realm."},
        ],
        glossary=[{"term_zh": "金丹", "term_en": "Golden Core", "locked": True}],
    )
    res = await _run(novel, 1)
    flags = res.glossary_flags
    assert any(f.term_zh == "金丹" and f.expected_en == "Golden Core" for f in flags)
    assert flags[0].paragraph_index == 0


async def test_unlocked_term_does_not_flag():
    novel = await _seed_novel(
        [
            {"chapter_num": 1, "original_text": "他突破到了金丹境界。",
             "translated_text": "He broke through to a new realm."},
        ],
        glossary=[{"term_zh": "金丹", "term_en": "Golden Core", "locked": False}],
    )
    res = await _run(novel, 1)
    assert res.glossary_flags == []


async def test_present_locked_term_does_not_flag():
    novel = await _seed_novel(
        [
            {"chapter_num": 1, "original_text": "他突破到了金丹境界。",
             "translated_text": "He broke through to the Golden Core realm."},
        ],
        glossary=[{"term_zh": "金丹", "term_en": "Golden Core", "locked": True}],
    )
    res = await _run(novel, 1)
    assert res.glossary_flags == []


# ---- Status contract ----------------------------------------------------


async def test_missing_chapter_returns_none():
    novel = await _seed_novel([
        {"chapter_num": 1, "original_text": "x", "translated_text": "y"},
    ])
    assert await _run(novel, 999) is None


async def test_not_translated_status():
    novel = await _seed_novel([
        {"chapter_num": 1, "original_text": "他取出了一件灵宝。",
         "translated_text": None, "status": "pending"},
    ])
    res = await _run(novel, 1)
    assert res.status == "not_translated"
    assert res.matches == []
    assert res.glossary_flags == []
