"""Bug hunt 2026-08-04 (B7): 'unaligned' chapters' retained rows stay out
of the cross-surface read feeds.

An unaligned verdict RETAINS rows so human work stays visible in the editor,
but those targets correspond to no current chapter text (zero positional
confidence). The shared `_EXCLUDE_UNALIGNED_SQL` predicate keeps them out of:
prefill_confirmed_exact, approved_prompt_pairs (own-chapter half),
fetch_confirmed_exemplar_pairs, recent_edited_pairs, corpus_for_consistency,
and search_segments. The editor's own GET still returns them for display
(pinned in test_segments_service / test_segments_routes).
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.services import segments as segments_svc
from backend.services.segments import hash16

pytestmark = pytest.mark.asyncio


def _unlink_db_files() -> None:
    # WAL gotcha (docs/decisions.md): a -wal/-shm pair left beside a deleted
    # main file resurrects stale pages into the next connection. Delete the
    # trio, and clean up on teardown too.
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH.parent / (DB_PATH.name + suffix)
        if p.exists():
            p.unlink()


@pytest.fixture(autouse=True)
def _reset_db():
    _unlink_db_files()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    yield
    _unlink_db_files()


_SRC_PARA = "甲" * 29 + "。"


async def _seed() -> tuple[int, int, int, int]:
    """One novel, three chapters:
      - ch1 (aligned, state 'ok'): a confirmed + an edited row for _SRC_PARA;
      - ch2 (state 'unaligned'): retained confirmed + edited rows for the
        SAME source paragraph with distinct targets;
      - ch3: the chapter 'being translated' (feeds exclude it by id).
    Returns (novel_id, ch1_id, ch2_id, ch3_id)."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        ids = []
        for num, state in ((1, "ok"), (2, "unaligned"), (3, "ok")):
            cur = await conn.execute(
                "INSERT INTO chapters (novel_id, chapter_num, original_text, "
                "translated_text, status, segments_state) "
                "VALUES (?, ?, ?, 'Body.', 'done', ?)",
                (novel_id, num, _SRC_PARA, state),
            )
            ids.append(cur.lastrowid)
        ch1, ch2, ch3 = ids
        rows = [
            # (chapter_id, seg_index, status, target, machine)
            (ch1, 0, "confirmed", "Aligned confirmed.", "AI one."),
            (ch1, 1, "edited", "Aligned edited.", "AI two."),
            (ch2, 0, "confirmed", "Detached confirmed.", "AI three."),
            (ch2, 1, "edited", "Detached edited.", "AI four."),
        ]
        for chapter_id, i, status, target, machine in rows:
            await conn.execute(
                "INSERT INTO chapter_segments (novel_id, chapter_id, "
                "seg_index, source_text, source_hash, target_text, "
                "machine_text, status, origin, aligned, edited_at, "
                "confirmed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', 1, "
                "datetime('now'), datetime('now'))",
                (novel_id, chapter_id, i, _SRC_PARA, hash16(_SRC_PARA),
                 target, machine, status),
            )
        await conn.commit()
    return novel_id, ch1, ch2, ch3


async def test_prefill_confirmed_exact_excludes_unaligned():
    novel_id, _ch1, _ch2, ch3 = await _seed()
    async with open_conn() as conn:
        prefill = await segments_svc.prefill_confirmed_exact(
            conn, novel_id, ch3, [_SRC_PARA]
        )
    assert prefill == {0: "Aligned confirmed."}


async def test_approved_prompt_pairs_skip_own_unaligned_rows():
    """When the chapter being translated is itself unaligned, its retained
    rows do not ride the APPROVED TRANSLATIONS block (the merge still
    re-anchors them when it can); cross-chapter confirmed matches from
    aligned chapters still do."""
    novel_id, _ch1, ch2, _ch3 = await _seed()
    async with open_conn() as conn:
        pairs = await segments_svc.approved_prompt_pairs(
            conn, novel_id, ch2, [_SRC_PARA]
        )
    # The only pair is the aligned ch1 confirmed rendering, NOT ch2's own
    # detached rows.
    assert [(i, en) for i, _zh, en in pairs] == [(0, "Aligned confirmed.")]


async def test_confirmed_exemplars_exclude_unaligned():
    novel_id, _ch1, _ch2, ch3 = await _seed()
    async with open_conn() as conn:
        pairs = await segments_svc.fetch_confirmed_exemplar_pairs(
            conn, novel_id, ch3, limit=10
        )
    assert [t for _s, t in pairs] == ["Aligned confirmed."]


async def test_recent_edited_pairs_exclude_unaligned():
    novel_id, _ch1, _ch2, ch3 = await _seed()
    async with open_conn() as conn:
        pairs = await segments_svc.recent_edited_pairs(
            conn, novel_id, ch3, limit=10
        )
    afters = {after for _before, after in pairs}
    assert "Detached confirmed." not in afters
    assert "Detached edited." not in afters
    assert afters == {"Aligned confirmed.", "Aligned edited."}


async def test_corpus_for_consistency_excludes_unaligned():
    novel_id, _ch1, _ch2, ch3 = await _seed()
    async with open_conn() as conn:
        rows = await segments_svc.corpus_for_consistency(conn, novel_id, ch3)
    targets = {r["target_text"] for r in rows}
    assert targets == {"Aligned confirmed.", "Aligned edited."}


async def test_search_segments_excludes_unaligned():
    novel_id, _ch1, _ch2, _ch3 = await _seed()
    async with open_conn() as conn:
        hits = await segments_svc.search_segments(
            conn, novel_id, "Detached", search_sides=("target",)
        )
        aligned_hits = await segments_svc.search_segments(
            conn, novel_id, "Aligned", search_sides=("target",)
        )
    assert hits == []
    assert {h["target_text"] for h in aligned_hits} == {
        "Aligned confirmed.", "Aligned edited.",
    }
