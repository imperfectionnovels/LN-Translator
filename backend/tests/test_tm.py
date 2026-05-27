"""Initiative 5 — translation memory tests.

Pins the contract the queue worker relies on:

1. Paragraph alignment: source CRLF and target LF both split correctly;
   leading Chinese heading is dropped so paragraph indices match;
   delta ≤ 2 is accepted, more rejects the chapter.
2. Atomic replace: populating one chapter twice (the retranslate path)
   replaces rows entirely — no orphaned prior-translation rows.
3. Concordance: case-sensitive on Chinese, case-insensitive on English,
   results in reading order, capped at the engine limit.
4. Inconsistency: same source paragraph rendered ≥ 2 ways surfaces with
   per-rendering chapter lists.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
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


async def _seed(payload):
    """payload: list of (chapter_num, source_text, target_text) → novel_id."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, source_url) VALUES (?, ?, NULL)",
            ("TestNovel", "paste"),
        )
        novel_id = cur.lastrowid
        for ch_num, src, tgt in payload:
            await conn.execute(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, original_text, translated_text, status) "
                "VALUES (?, ?, ?, ?, 'done')",
                (novel_id, ch_num, src, tgt),
            )
        await conn.commit()
    return novel_id


# ---- Alignment ----------------------------------------------------------


def test_align_strips_leading_chinese_heading():
    src = "第1章 测试\r\n\r\n源段一。\r\n\r\n源段二。"
    tgt = "Source paragraph one.\n\nSource paragraph two."
    pairs = tm_svc.align_paragraphs(src, tgt)
    assert pairs is not None
    assert len(pairs) == 2
    assert pairs[0].source_text == "源段一。"
    assert pairs[0].target_text == "Source paragraph one."
    assert pairs[1].paragraph_index == 1


def test_align_accepts_off_by_one_delta():
    src = "源段一。\r\n\r\n源段二。\r\n\r\n源段三。"
    tgt = "P1.\n\nP2."  # delta=1, target is shorter
    pairs = tm_svc.align_paragraphs(src, tgt)
    assert pairs is not None
    assert len(pairs) == 2  # truncated to min(src, tgt)


def test_align_rejects_large_delta():
    src = "源段一。"  # 1 paragraph
    tgt = "P1.\n\nP2.\n\nP3.\n\nP4.\n\nP5."  # 5 paragraphs, delta=4
    pairs = tm_svc.align_paragraphs(src, tgt)
    assert pairs is None


def test_align_handles_crlf_source_lf_target():
    """Empirically the most common case: source from upload has CRLF
    blank lines, target from the LLM has LF blank lines."""
    src = "源段一。\r\n\r\n源段二。\r\n\r\n源段三。"
    tgt = "P1.\n\nP2.\n\nP3."
    pairs = tm_svc.align_paragraphs(src, tgt)
    assert pairs is not None
    assert len(pairs) == 3


# ---- Atomic replace -----------------------------------------------------


@pytest.mark.asyncio
async def test_replace_chapter_segments_writes_aligned_rows():
    novel_id = await _seed([
        (1, "源段一。\r\n\r\n源段二。", "Para one.\n\nPara two."),
    ])
    async with open_conn() as conn:
        cur = await conn.execute("SELECT id FROM chapters WHERE chapter_num = 1")
        chapter_id = (await cur.fetchone())["id"]
        n = await tm_svc.replace_chapter_segments(
            conn, novel_id, chapter_id,
            "源段一。\r\n\r\n源段二。",
            "Para one.\n\nPara two.",
        )
        await conn.commit()
    assert n == 2
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT paragraph_index, source_text, target_text FROM tm_segments "
            "WHERE chapter_id = ? ORDER BY paragraph_index", (chapter_id,)
        )
        rows = await cur.fetchall()
    assert len(rows) == 2
    assert rows[0]["source_text"] == "源段一。"
    assert rows[1]["target_text"] == "Para two."


@pytest.mark.asyncio
async def test_replace_clears_prior_rows_atomically():
    """The retranslation invariant: re-running populate against the same
    chapter wipes prior rows. Without this, a chapter that gets shorter
    on retranslate would leave dangling old paragraphs in the TM."""
    novel_id = await _seed([
        (1, "源段一。\r\n\r\n源段二。\r\n\r\n源段三。",
         "P1.\n\nP2.\n\nP3."),
    ])
    async with open_conn() as conn:
        cur = await conn.execute("SELECT id FROM chapters WHERE chapter_num = 1")
        chapter_id = (await cur.fetchone())["id"]
        # First populate: 3 paragraphs.
        await tm_svc.replace_chapter_segments(
            conn, novel_id, chapter_id,
            "源段一。\r\n\r\n源段二。\r\n\r\n源段三。",
            "P1.\n\nP2.\n\nP3.",
        )
        # Retranslate: now 2 paragraphs. Should replace, not append.
        await tm_svc.replace_chapter_segments(
            conn, novel_id, chapter_id,
            "源段一。\r\n\r\n源段二。",
            "First.\n\nSecond.",
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT paragraph_index, target_text FROM tm_segments "
            "WHERE chapter_id = ? ORDER BY paragraph_index", (chapter_id,)
        )
        rows = await cur.fetchall()
    assert len(rows) == 2
    assert rows[0]["target_text"] == "First."
    assert rows[1]["target_text"] == "Second."


@pytest.mark.asyncio
async def test_replace_skips_chapter_when_alignment_fails():
    """A chapter with a large source/target delta gets its prior rows
    wiped (correct — they no longer reflect current text) but NO new
    rows written. Quiet skip + log line; no exception."""
    novel_id = await _seed([(1, "源段一。", "P1.\n\nP2.\n\nP3.\n\nP4.\n\nP5.")])
    async with open_conn() as conn:
        cur = await conn.execute("SELECT id FROM chapters WHERE chapter_num = 1")
        chapter_id = (await cur.fetchone())["id"]
        n = await tm_svc.replace_chapter_segments(
            conn, novel_id, chapter_id,
            "源段一。", "P1.\n\nP2.\n\nP3.\n\nP4.\n\nP5.",
        )
        await conn.commit()
    assert n == 0
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM tm_segments WHERE chapter_id = ?",
            (chapter_id,),
        )
        assert (await cur.fetchone())["c"] == 0


# ---- Concordance --------------------------------------------------------


@pytest.mark.asyncio
async def test_concordance_returns_hits_in_reading_order():
    novel_id = await _seed([
        (1, "白小纯走来。\r\n\r\n他笑了。", "Bai Xiaochun walked.\n\nHe smiled."),
        (3, "白小纯说话。", "Bai Xiaochun spoke."),
    ])
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id FROM chapters ORDER BY chapter_num"
        )
        chapter_ids = [r["id"] for r in await cur.fetchall()]
        for cid, src, tgt in zip(
            chapter_ids,
            ["白小纯走来。\r\n\r\n他笑了。", "白小纯说话。"],
            ["Bai Xiaochun walked.\n\nHe smiled.", "Bai Xiaochun spoke."],
        ):
            await tm_svc.replace_chapter_segments(conn, novel_id, cid, src, tgt)
        await conn.commit()
        hits = await tm_svc.search(conn, novel_id, "白小纯")
    # Two chapters, three matching paragraphs (ch1 has 2, ch3 has 1) —
    # but only the paragraphs that contain 白小纯 count: ch1 para 0, ch3 para 0.
    assert len(hits) == 2
    assert hits[0].chapter_num == 1
    assert hits[1].chapter_num == 3
    assert all(h.matched_side == "source" for h in hits)


@pytest.mark.asyncio
async def test_concordance_target_search_is_case_insensitive():
    novel_id = await _seed([
        (1, "源段。", "BAI XIAOCHUN walked."),
        (2, "源段。", "bai xiaochun walked."),
    ])
    async with open_conn() as conn:
        cur = await conn.execute("SELECT id FROM chapters ORDER BY chapter_num")
        ids = [r["id"] for r in await cur.fetchall()]
        for cid in ids:
            await tm_svc.replace_chapter_segments(
                conn, novel_id, cid, "源段。",
                "BAI XIAOCHUN walked." if cid == ids[0] else "bai xiaochun walked.",
            )
        await conn.commit()
        hits = await tm_svc.search(conn, novel_id, "Bai Xiaochun", search_sides=("target",))
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_concordance_requires_minimum_query_length():
    novel_id = await _seed([(1, "源段。", "Target.")])
    async with open_conn() as conn:
        # 1 char — below minimum; returns []. Prevents matching every
        # paragraph on a stray keystroke.
        hits = await tm_svc.search(conn, novel_id, "x")
    assert hits == []


# ---- Inconsistency detection -------------------------------------------


@pytest.mark.asyncio
async def test_inconsistency_flags_same_source_different_targets():
    """The same source paragraph rendered two ways across chapters
    surfaces as one inconsistency group with both renderings."""
    same_source = "他大笑。"
    novel_id = await _seed([
        (1, same_source, "He laughed loudly."),
        (2, same_source, "He laughed loudly."),
        (3, same_source, "He chuckled."),  # different rendering
    ])
    async with open_conn() as conn:
        cur = await conn.execute("SELECT id FROM chapters ORDER BY chapter_num")
        ids = [r["id"] for r in await cur.fetchall()]
        targets = ["He laughed loudly.", "He laughed loudly.", "He chuckled."]
        for cid, t in zip(ids, targets):
            await tm_svc.replace_chapter_segments(
                conn, novel_id, cid, same_source, t
            )
        await conn.commit()
        groups = await tm_svc.find_inconsistencies(conn, novel_id)
    assert len(groups) == 1
    g = groups[0]
    assert g.source_text == same_source
    assert len(g.renderings) == 2
    assert g.total_occurrences == 3


@pytest.mark.asyncio
async def test_inconsistency_silent_when_all_renderings_match():
    novel_id = await _seed([
        (1, "他大笑。", "He laughed."),
        (2, "他大笑。", "He laughed."),
    ])
    async with open_conn() as conn:
        cur = await conn.execute("SELECT id FROM chapters ORDER BY chapter_num")
        ids = [r["id"] for r in await cur.fetchall()]
        for cid in ids:
            await tm_svc.replace_chapter_segments(
                conn, novel_id, cid, "他大笑。", "He laughed."
            )
        await conn.commit()
        groups = await tm_svc.find_inconsistencies(conn, novel_id)
    assert groups == []
