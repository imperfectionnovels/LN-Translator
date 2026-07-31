"""Segment store service (services/segments.py) + tm.full_alignment_path.

Pins the CAT Phase 2 contracts:
  - perfect 1:1 chapters backfill positionally (all aligned=1, state 'ok');
  - non-1:1 chapters go through the full DP path (every source index gets a
    target; unmatched sources get "" aligned=0; state 'partial');
  - below the <50% confidence gate the chapter is 'unaligned' with ZERO rows;
  - segments are always built from the COMMITTED displayed body (self-heal
    rebuilds after an out-of-band edit; the displayed-body rule picks
    refined only when refinement_status == 'done');
  - a SEGMENTATION_VERSION mismatch forces a rebuild;
  - chapter_rev tracks the body.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.services import segments as segments_svc
from backend.services import tm as tm_svc
from backend.services.segmentation import SEGMENTATION_VERSION


@pytest.fixture(autouse=True)
def _reset_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    yield


# Chinese source paragraphs MUST end in terminal punctuation, or
# effective_source_paragraphs joins them (the mid-sentence pre-join).
def _zh_para(ch: str, length: int = 30) -> str:
    return ch * (length - 1) + "。"


async def _seed_chapter(
    original: str,
    translated: str | None,
    *,
    refined: str | None = None,
    refinement_status: str = "none",
    status: str = "done",
) -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, title_zh, title_en, original_text, "
            " translated_text, refined_text, refinement_status, status) "
            "VALUES (?, 1, '第一章', 'Chapter 1', ?, ?, ?, ?, ?)",
            (novel_id, original, translated, refined, refinement_status, status),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _get(novel_id: int, ch: int = 1) -> dict | None:
    async with open_conn() as conn:
        payload = await segments_svc.get_segments(conn, novel_id, ch)
        await conn.commit()
    return payload


def _db_segments(chapter_id: int) -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT id, seg_index, source_text, target_text, machine_text, "
            "status, origin, aligned FROM chapter_segments "
            "WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        ).fetchall()
    finally:
        conn.close()


def _db_chapter_state(chapter_id: int) -> tuple:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT segments_state, segmentation_version FROM chapters "
            "WHERE id = ?",
            (chapter_id,),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# displayed_body / chapter_rev units
# ---------------------------------------------------------------------------


def test_displayed_body_draft_by_default():
    row = {"refinement_status": "none", "refined_text": None,
           "translated_text": "Draft."}
    assert segments_svc.displayed_body(row) == ("draft", "Draft.")


def test_displayed_body_refined_only_when_done():
    base = {"refined_text": "Polished.", "translated_text": "Draft."}
    assert segments_svc.displayed_body({**base, "refinement_status": "done"}) \
        == ("refined", "Polished.")
    for st in ("none", "pending", "in_progress", "error", None):
        assert segments_svc.displayed_body({**base, "refinement_status": st}) \
            == ("draft", "Draft.")
    # done but empty refined text falls back to the draft.
    assert segments_svc.displayed_body(
        {"refinement_status": "done", "refined_text": "",
         "translated_text": "Draft."}
    ) == ("draft", "Draft.")


def test_displayed_body_never_returns_none_text():
    row = {"refinement_status": "none", "refined_text": None,
           "translated_text": None}
    assert segments_svc.displayed_body(row) == ("draft", "")


def test_chapter_rev_is_16_hex_and_tracks_body():
    a = segments_svc.chapter_rev("one body")
    b = segments_svc.chapter_rev("another body")
    assert len(a) == 16 and int(a, 16) >= 0
    assert a != b
    assert a == segments_svc.chapter_rev("one body")


# ---------------------------------------------------------------------------
# tm.full_alignment_path units
# ---------------------------------------------------------------------------


def test_full_path_source_without_target_gets_empty():
    src = [_zh_para("甲"), _zh_para("乙"),
           _zh_para("丙"), _zh_para("丁")]
    tgt = ["A" * 60, "B" * 60, "C" * 60]
    path = tm_svc.full_alignment_path(src, tgt)
    assert path is not None
    assert len(path) == 4
    empties = [(t, a) for t, a in path if not t]
    assert len(empties) == 1 and empties[0][1] is False
    assert sum(1 for _t, a in path if a) == 3
    # Every target paragraph lands somewhere, in order.
    assert "\n\n".join(t for t, _a in path if t) == "\n\n".join(tgt)


def test_full_path_extra_target_attaches_to_neighbor():
    src = [_zh_para("甲"), _zh_para("乙")]
    tgt = ["A" * 45, "B" * 45, "C" * 45]
    path = tm_svc.full_alignment_path(src, tgt)
    assert path is not None
    assert len(path) == 2
    # All three targets survive, one source carries a merged pair.
    assert "\n\n".join(t for t, _a in path if t) == "\n\n".join(tgt)
    merged = [(t, a) for t, a in path if "\n\n" in t]
    assert len(merged) == 1 and merged[0][1] is False


def test_full_path_outlier_anchor_demoted_not_dropped():
    # A tiny beat sitting in a long source's slot: the DP matches them, the
    # outlier rule refuses to trust it. Full path keeps the text, aligned=0.
    src = [_zh_para("甲", 100), _zh_para("乙", 100)]
    tgt = ["A" * 150, "B" * 10]
    path = tm_svc.full_alignment_path(src, tgt)
    assert path is not None
    assert path[0] == ("A" * 150, True)
    assert path[1] == ("B" * 10, False)


def test_full_path_insert_before_first_source_prepends():
    # The DP can open with an 'ins' (a target-only beat before any source
    # consumed). It must prepend onto the FIRST source, flagged unaligned.
    src = [_zh_para("甲", 51)]
    tgt = ["Oops.", "A" * 100]
    path = tm_svc.full_alignment_path(src, tgt)
    assert path is not None
    assert path == [("Oops.\n\n" + "A" * 100, False)]


def test_full_path_below_gate_returns_none():
    src = [_zh_para(c) for c in "甲乙丙丁戊己"]
    tgt = ["One lonely paragraph."]
    assert tm_svc.full_alignment_path(src, tgt) is None


def test_full_path_empty_inputs_return_none():
    assert tm_svc.full_alignment_path([], ["x"]) is None
    assert tm_svc.full_alignment_path(["x"], []) is None


# ---------------------------------------------------------------------------
# build + get: perfect 1:1
# ---------------------------------------------------------------------------

_SRC_3 = "\n\n".join(
    [_zh_para("甲"), _zh_para("乙"), _zh_para("丙")]
)
_TGT_3 = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."


async def test_perfect_1to1_backfill():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    payload = await _get(novel_id)
    assert payload["chapter_status"] == "done"
    assert payload["variant"] == "draft"
    assert payload["segments_state"] == "ok"
    assert payload["aligned"] is True
    assert payload["progress"] == {"confirmed": 0, "total": 3}
    assert payload["next_unconfirmed_index"] == 0
    assert payload["chapter_rev"] == segments_svc.chapter_rev(_TGT_3)
    segs = payload["segments"]
    assert [s["index"] for s in segs] == [0, 1, 2]
    assert [s["target_text"] for s in segs] == [
        "First paragraph.", "Second paragraph.", "Third paragraph.",
    ]
    for s in segs:
        assert s["status"] == "machine"
        assert s["origin"] == "aligned_backfill"
        assert s["aligned"] is True
        assert s["source_hash"] == segments_svc._hash16(s["source_text"])
        assert s["target_hash"] == segments_svc._hash16(s["target_text"])
        assert s["confirmed_at"] is None
    # machine_text mirrors target_text on backfill; chapter state stamped.
    rows = _db_segments(chapter_id)
    assert all(r[3] == r[4] for r in rows)  # target_text == machine_text
    assert _db_chapter_state(chapter_id) == ("ok", SEGMENTATION_VERSION)


async def test_weld_case_partial():
    # 4 source paragraphs, 3 target paragraphs: the full path assigns every
    # source a slot; the unmatched one is flagged, state is 'partial'.
    src = "\n\n".join(
        [_zh_para("甲"), _zh_para("乙"),
         _zh_para("丙"), _zh_para("丁")]
    )
    tgt = "\n\n".join(["A" * 60, "B" * 60, "C" * 60])
    novel_id, chapter_id = await _seed_chapter(src, tgt)
    payload = await _get(novel_id)
    assert payload["segments_state"] == "partial"
    assert payload["aligned"] is False
    segs = payload["segments"]
    assert len(segs) == 4
    flagged = [s for s in segs if not s["aligned"]]
    assert len(flagged) == 1 and flagged[0]["target_text"] == ""
    assert _db_chapter_state(chapter_id)[0] == "partial"


async def test_weld_partial_stable_across_reads():
    # Self-heal must tolerate the ""-target row of a partial chapter (the
    # empty-skip in _segments_match_body): repeated GETs never rebuild.
    src = "\n\n".join(
        [_zh_para("甲"), _zh_para("乙"),
         _zh_para("丙"), _zh_para("丁")]
    )
    tgt = "\n\n".join(["A" * 60, "B" * 60, "C" * 60])
    novel_id, chapter_id = await _seed_chapter(src, tgt)
    first = await _get(novel_id)
    assert first["segments_state"] == "partial"
    ids = [r[0] for r in _db_segments(chapter_id)]
    second = await _get(novel_id)
    assert [r[0] for r in _db_segments(chapter_id)] == ids
    third = await _get(novel_id)
    assert [r[0] for r in _db_segments(chapter_id)] == ids
    assert third == second == first


async def test_merged_target_partial_stable_across_reads():
    # An extra target merged onto one source stores a "\n\n"-joined
    # target_text; the self-heal join must still reproduce the body, so
    # repeated GETs never rebuild.
    src = "\n\n".join([_zh_para("甲"), _zh_para("乙")])
    tgt = "\n\n".join(["A" * 45, "B" * 45, "C" * 45])
    novel_id, chapter_id = await _seed_chapter(src, tgt)
    first = await _get(novel_id)
    assert first["segments_state"] == "partial"
    assert any("\n\n" in s["target_text"] for s in first["segments"])
    ids = [r[0] for r in _db_segments(chapter_id)]
    second = await _get(novel_id)
    assert [r[0] for r in _db_segments(chapter_id)] == ids
    assert second == first


async def test_crlf_body_stable_across_reads():
    # A CRLF-separated body must not force a rebuild on every read: the
    # self-heal check compares against the NORMALIZED paragraph split.
    crlf_body = "First paragraph.\r\n\r\nSecond paragraph.\r\n\r\nThird paragraph."
    novel_id, chapter_id = await _seed_chapter(_SRC_3, crlf_body)
    first = await _get(novel_id)
    assert first["segments_state"] == "ok"
    ids = [r[0] for r in _db_segments(chapter_id)]
    second = await _get(novel_id)
    assert [r[0] for r in _db_segments(chapter_id)] == ids
    assert second == first


async def test_below_gate_unaligned_zero_rows():
    src = "\n\n".join(
        _zh_para(c) for c in "甲乙丙丁戊己"
    )
    novel_id, chapter_id = await _seed_chapter(src, "One lonely paragraph.")
    payload = await _get(novel_id)
    assert payload["segments_state"] == "unaligned"
    assert payload["aligned"] is False
    assert payload["segments"] == []
    assert payload["progress"] == {"confirmed": 0, "total": 0}
    assert payload["next_unconfirmed_index"] is None
    assert _db_segments(chapter_id) == []
    assert _db_chapter_state(chapter_id) == ("unaligned", SEGMENTATION_VERSION)


async def test_unaligned_verdict_cached_until_body_changes(monkeypatch):
    # The persisted 'unaligned' verdict (segments_rev gate) stops reads from
    # re-running the aligner while the body is unchanged, but a retranslate
    # (body change) must still be picked up on the next read.
    src_paras = [_zh_para(c) for c in "甲乙丙丁戊己"]
    novel_id, chapter_id = await _seed_chapter(
        "\n\n".join(src_paras), "One lonely paragraph."
    )
    first = await _get(novel_id)
    assert first["segments_state"] == "unaligned"

    def _must_not_run(_src, _tgt):
        raise AssertionError("aligner re-ran on an unchanged unaligned body")

    monkeypatch.setattr(tm_svc, "full_alignment_path", _must_not_run)
    second = await _get(novel_id)
    assert second["segments_state"] == "unaligned"
    assert second == first

    # Simulate a retranslate under the 1:1 contract: 6 target paragraphs.
    new_body = "\n\n".join(f"Paragraph number {i}." for i in range(6))
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translated_text = ? WHERE id = ?",
        (new_body, chapter_id),
    )
    conn.commit()
    conn.close()

    # Counts now match, so the rebuild maps positionally (the patched
    # aligner stays uncalled) and the chapter comes alive.
    third = await _get(novel_id)
    assert third["segments_state"] == "ok"
    assert len(third["segments"]) == 6
    assert third["chapter_rev"] == segments_svc.chapter_rev(new_body)


async def test_not_done_chapter_is_status_only_no_writes():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, None, status="pending")
    payload = await _get(novel_id)
    assert payload["chapter_status"] == "pending"
    assert payload["segments"] == []
    assert payload["segments_state"] is None
    assert payload["chapter_rev"] is None
    assert _db_segments(chapter_id) == []
    # No stamp: the chapter was never built.
    assert _db_chapter_state(chapter_id) == (None, None)


# ---------------------------------------------------------------------------
# self-heal, displayed-body routing, version bump
# ---------------------------------------------------------------------------


async def test_self_heal_rebuilds_after_out_of_band_edit():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    first = await _get(novel_id)
    assert first["segments_state"] == "ok"

    new_body = "Edited first.\n\nEdited second.\n\nEdited third."
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translated_text = ? WHERE id = ?",
        (new_body, chapter_id),
    )
    conn.commit()
    conn.close()

    second = await _get(novel_id)
    assert [s["target_text"] for s in second["segments"]] == [
        "Edited first.", "Edited second.", "Edited third.",
    ]
    assert second["chapter_rev"] == segments_svc.chapter_rev(new_body)
    assert second["chapter_rev"] != first["chapter_rev"]


async def test_refined_body_drives_segments_when_done():
    refined = "Polished one.\n\nPolished two.\n\nPolished three."
    novel_id, _ = await _seed_chapter(
        _SRC_3, _TGT_3, refined=refined, refinement_status="done",
    )
    payload = await _get(novel_id)
    assert payload["variant"] == "refined"
    assert [s["target_text"] for s in payload["segments"]] == [
        "Polished one.", "Polished two.", "Polished three.",
    ]
    assert payload["chapter_rev"] == segments_svc.chapter_rev(refined)


async def test_refined_text_ignored_unless_status_done():
    refined = "Polished one.\n\nPolished two.\n\nPolished three."
    novel_id, _ = await _seed_chapter(
        _SRC_3, _TGT_3, refined=refined, refinement_status="error",
    )
    payload = await _get(novel_id)
    assert payload["variant"] == "draft"
    assert payload["segments"][0]["target_text"] == "First paragraph."


async def test_segmentation_version_bump_forces_rebuild():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _get(novel_id)
    before_ids = [r[0] for r in _db_segments(chapter_id)]

    # Simulate rows built by an older heuristic: stale stored version, body
    # unchanged (so the self-heal equality alone would NOT trigger).
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET segmentation_version = 0 WHERE id = ?",
        (chapter_id,),
    )
    conn.commit()
    conn.close()

    payload = await _get(novel_id)
    assert payload["segments_state"] == "ok"
    after_ids = [r[0] for r in _db_segments(chapter_id)]
    assert after_ids != before_ids  # rows were rebuilt
    assert _db_chapter_state(chapter_id) == ("ok", SEGMENTATION_VERSION)


async def test_second_get_reuses_stored_rows():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _get(novel_id)
    first_ids = [r[0] for r in _db_segments(chapter_id)]
    payload = await _get(novel_id)
    assert [r[0] for r in _db_segments(chapter_id)] == first_ids
    assert payload["segments_state"] == "ok"


async def test_missing_chapter_returns_none():
    novel_id, _ = await _seed_chapter(_SRC_3, _TGT_3)
    assert await _get(novel_id, ch=99) is None
    assert await _get(novel_id + 1000) is None
