"""Segment store service (services/segments.py) + tm.full_alignment_path.

Pins the CAT Phase 2 contracts:
  - perfect 1:1 chapters backfill positionally (all aligned=1, state 'ok');
  - non-1:1 chapters go through the full DP path (every source index gets a
    target; unmatched sources get "" aligned=0; state 'partial');
  - below the <50% confidence gate the chapter is 'unaligned' with ZERO rows;
  - segments are always built from the COMMITTED displayed body (self-heal
    rebuilds after an out-of-band edit; the displayed-body rule picks
    refined whenever refined_text is non-empty, the presence rule);
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


def test_displayed_body_presence_keyed():
    """2026-07-31 retry-window fix: refined text is canonical whenever it is
    non-empty, REGARDLESS of refinement_status, so retained polish stays
    displayed through a retry window (pending/in_progress) and after a
    failed retry (error). Empty/NULL refined_text (never refined, initial
    refinement in flight, or post-retranslate) displays the draft."""
    base = {"refined_text": "Polished.", "translated_text": "Draft."}
    for st in ("done", "none", "pending", "in_progress", "error", None):
        assert segments_svc.displayed_body({**base, "refinement_status": st}) \
            == ("refined", "Polished.")
    for st in ("done", "none", "pending", "in_progress", "error", None):
        assert segments_svc.displayed_body(
            {"refinement_status": st, "refined_text": "",
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
        assert s["source_hash"] == segments_svc.hash16(s["source_text"])
        assert s["target_hash"] == segments_svc.hash16(s["target_text"])
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


async def test_refined_text_displayed_whenever_present():
    # 2026-07-31 retry-window fix: a retained refined body stays canonical
    # even when refinement_status is 'error' (a failed RETRY of a
    # previously good refinement must not demote the display to draft).
    refined = "Polished one.\n\nPolished two.\n\nPolished three."
    novel_id, _ = await _seed_chapter(
        _SRC_3, _TGT_3, refined=refined, refinement_status="error",
    )
    payload = await _get(novel_id)
    assert payload["variant"] == "refined"
    assert payload["segments"][0]["target_text"] == "Polished one."


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


# ---------------------------------------------------------------------------
# Phase 3: update_segment state machine
# ---------------------------------------------------------------------------


async def _patch(
    novel_id: int,
    seg_index: int,
    action: str,
    after: str | None = None,
    ch: int = 1,
    rev: str | None = None,
    before_hash: str | None = None,
) -> dict:
    """Drive update_segment the way the route does: rev + hash from a fresh
    GET unless the test overrides them to provoke a stale error."""
    payload = await _get(novel_id, ch)
    seg = next(
        (s for s in payload["segments"] if s["index"] == seg_index), None
    )
    async with open_conn() as conn:
        result = await segments_svc.update_segment(
            conn, novel_id, ch, seg_index,
            action=action,
            after_text=after,
            client_rev=rev if rev is not None else (payload["chapter_rev"] or ""),
            before_target_hash=(
                before_hash if before_hash is not None
                else (seg["target_hash"] if seg else "0" * 16)
            ),
        )
        await conn.commit()
    return result


def _assert_i1(chapter_id: int) -> None:
    """Invariant I1: join(non-empty targets) reproduces the displayed body,
    and the stamped segments_rev matches it."""
    from backend.services.segmentation import split_target_paragraphs
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ch = conn.execute(
            "SELECT translated_text, refined_text, refinement_status, "
            "segments_rev FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
        targets = [r[0] for r in conn.execute(
            "SELECT target_text FROM chapter_segments "
            "WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        ).fetchall()]
    finally:
        conn.close()
    if ch["refined_text"]:  # presence keying, matches displayed_body
        body = ch["refined_text"]
    else:
        body = ch["translated_text"] or ""
    joined = "\n\n".join(t for t in targets if t)
    assert joined == "\n\n".join(split_target_paragraphs(body))
    assert ch["segments_rev"] == segments_svc.chapter_rev(body)


def _db_body(chapter_id: int, column: str = "translated_text") -> str:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            f"SELECT {column} FROM chapters WHERE id = ?", (chapter_id,)
        ).fetchone()[0]
    finally:
        conn.close()


async def test_save_machine_row_edits_and_materializes():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    result = await _patch(novel_id, 1, "save", after="A better paragraph.")
    seg = result["segment"]
    assert seg["status"] == "edited"
    assert seg["origin"] == "human"
    assert seg["target_text"] == "A better paragraph."
    assert seg["aligned"] is True
    assert seg["confirmed_at"] is None
    new_body = "First paragraph.\n\nA better paragraph.\n\nThird paragraph."
    assert _db_body(chapter_id) == new_body
    assert result["chapter_rev"] == segments_svc.chapter_rev(new_body)
    assert result["segments_state"] == "ok"
    assert result["progress"] == {"confirmed": 0, "total": 3}
    assert result["next_unconfirmed_index"] == 0
    _assert_i1(chapter_id)
    # machine_text keeps the AI's version (revert anchor); edited_at stamped.
    rows = _db_segments(chapter_id)
    assert rows[1][4] == "Second paragraph."  # machine_text untouched


async def test_save_strips_whitespace_and_rejects_empty():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    result = await _patch(novel_id, 0, "save", after="  Trimmed.  ")
    assert result["segment"]["target_text"] == "Trimmed."
    with pytest.raises(segments_svc.SegmentActionError):
        await _patch(novel_id, 0, "save", after="   ")
    _assert_i1(chapter_id)


async def test_confirm_machine_row_no_text_change():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    before_rev = (await _get(novel_id))["chapter_rev"]
    result = await _patch(novel_id, 0, "confirm")
    seg = result["segment"]
    assert seg["status"] == "confirmed"
    assert seg["confirmed_at"] is not None
    assert seg["target_text"] == "First paragraph."
    assert result["chapter_rev"] == before_rev  # body untouched
    assert result["progress"] == {"confirmed": 1, "total": 3}
    assert result["next_unconfirmed_index"] == 1
    assert _db_body(chapter_id) == _TGT_3
    _assert_i1(chapter_id)


async def test_confirm_is_idempotent():
    novel_id, _ = await _seed_chapter(_SRC_3, _TGT_3)
    first = await _patch(novel_id, 0, "confirm")
    second = await _patch(novel_id, 0, "confirm")
    assert second["segment"]["status"] == "confirmed"
    # The original confirmed_at stands (no re-stamp on an idempotent hit).
    assert second["segment"]["confirmed_at"] == first["segment"]["confirmed_at"]
    assert second["progress"] == {"confirmed": 1, "total": 3}


async def test_save_and_confirm_in_one_write():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    result = await _patch(novel_id, 2, "save_and_confirm", after="Final form.")
    seg = result["segment"]
    assert seg["status"] == "confirmed"
    assert seg["origin"] == "human"
    assert seg["confirmed_at"] is not None
    assert _db_body(chapter_id).endswith("Final form.")
    _assert_i1(chapter_id)


async def test_save_demotes_confirmed_to_edited():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 1, "confirm")
    result = await _patch(novel_id, 1, "save", after="Changed after confirm.")
    seg = result["segment"]
    assert seg["status"] == "edited"
    assert seg["confirmed_at"] is None
    assert result["progress"] == {"confirmed": 0, "total": 3}
    _assert_i1(chapter_id)


async def test_unconfirm_only_from_confirmed():
    novel_id, _ = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 0, "confirm")
    result = await _patch(novel_id, 0, "unconfirm")
    assert result["segment"]["status"] == "edited"
    assert result["segment"]["confirmed_at"] is None
    # machine and edited rows refuse unconfirm.
    with pytest.raises(segments_svc.SegmentActionError):
        await _patch(novel_id, 1, "unconfirm")
    with pytest.raises(segments_svc.SegmentActionError):
        await _patch(novel_id, 0, "unconfirm")  # now 'edited'


async def test_revert_machine_restores_ai_text():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 1, "save", after="Human version.")
    result = await _patch(novel_id, 1, "revert_machine")
    seg = result["segment"]
    assert seg["status"] == "machine"
    assert seg["target_text"] == "Second paragraph."
    assert seg["confirmed_at"] is None
    assert _db_body(chapter_id) == _TGT_3
    _assert_i1(chapter_id)
    # From machine it is a 400-style error.
    with pytest.raises(segments_svc.SegmentActionError):
        await _patch(novel_id, 1, "revert_machine")


async def test_revert_machine_from_confirmed():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 0, "save_and_confirm", after="Confirmed human.")
    result = await _patch(novel_id, 0, "revert_machine")
    assert result["segment"]["status"] == "machine"
    assert result["segment"]["target_text"] == "First paragraph."
    assert result["progress"] == {"confirmed": 0, "total": 3}
    _assert_i1(chapter_id)


async def test_unknown_action_rejected():
    novel_id, _ = await _seed_chapter(_SRC_3, _TGT_3)
    with pytest.raises(segments_svc.SegmentActionError):
        await _patch(novel_id, 0, "obliterate")


async def test_stale_chapter_rev_409():
    novel_id, _ = await _seed_chapter(_SRC_3, _TGT_3)
    with pytest.raises(segments_svc.SegmentStaleError) as exc:
        await _patch(novel_id, 0, "save", after="x", rev="f" * 16)
    assert exc.value.kind == "stale_chapter"


async def test_stale_segment_hash_409():
    novel_id, _ = await _seed_chapter(_SRC_3, _TGT_3)
    with pytest.raises(segments_svc.SegmentStaleError) as exc:
        await _patch(novel_id, 0, "save", after="x", before_hash="f" * 16)
    assert exc.value.kind == "stale_segment"


async def test_not_done_chapter_409_kind_chapter_translating():
    for status in ("pending", "translating", "error"):
        novel_id, chapter_id = await _seed_chapter(
            _SRC_3, _TGT_3, status=status
        )
        async with open_conn() as conn:
            with pytest.raises(segments_svc.SegmentStaleError) as exc:
                await segments_svc.update_segment(
                    conn, novel_id, 1, 0,
                    action="save", after_text="x",
                    client_rev=segments_svc.chapter_rev(_TGT_3),
                    before_target_hash="0" * 16,
                )
        assert exc.value.kind == "chapter_translating"


async def test_unaligned_chapter_refuses_writes():
    src = "\n\n".join(_zh_para(c) for c in "甲乙丙丁戊己")
    novel_id, _ = await _seed_chapter(src, "One lonely paragraph.")
    payload = await _get(novel_id)
    assert payload["segments_state"] == "unaligned"
    async with open_conn() as conn:
        with pytest.raises(segments_svc.SegmentStaleError) as exc:
            await segments_svc.update_segment(
                conn, novel_id, 1, 0,
                action="save", after_text="x",
                client_rev=payload["chapter_rev"],
                before_target_hash="0" * 16,
            )
    assert exc.value.kind == "stale_chapter"


async def test_missing_chapter_and_segment_raise_not_found():
    novel_id, _ = await _seed_chapter(_SRC_3, _TGT_3)
    await _get(novel_id)
    async with open_conn() as conn:
        with pytest.raises(segments_svc.SegmentNotFoundError):
            await segments_svc.update_segment(
                conn, novel_id, 99, 0, action="save", after_text="x",
                client_rev="0" * 16, before_target_hash="0" * 16,
            )
        with pytest.raises(segments_svc.SegmentNotFoundError):
            await segments_svc.update_segment(
                conn, novel_id, 1, 99, action="save", after_text="x",
                client_rev=segments_svc.chapter_rev(_TGT_3),
                before_target_hash="0" * 16,
            )


async def test_refined_variant_save_materializes_refined_column():
    refined = "Polished one.\n\nPolished two.\n\nPolished three."
    novel_id, chapter_id = await _seed_chapter(
        _SRC_3, _TGT_3, refined=refined, refinement_status="done",
    )
    result = await _patch(novel_id, 1, "save", after="Hand polished two.")
    assert result["segment"]["status"] == "edited"
    assert _db_body(chapter_id, "refined_text") == (
        "Polished one.\n\nHand polished two.\n\nPolished three."
    )
    # The archival draft column is untouched.
    assert _db_body(chapter_id) == _TGT_3
    _assert_i1(chapter_id)


async def test_partial_chapter_save_fills_empty_slot_and_flips_ok():
    # 4 source paragraphs, 3 targets: one empty aligned=0 slot. Hand-filling
    # it lands the paragraph at the right position in the body and, with
    # every row now aligned, the chapter graduates to 'ok'.
    src = "\n\n".join(
        [_zh_para("甲"), _zh_para("乙"), _zh_para("丙"), _zh_para("丁")]
    )
    tgt = "\n\n".join(["A" * 60, "B" * 60, "C" * 60])
    novel_id, chapter_id = await _seed_chapter(src, tgt)
    payload = await _get(novel_id)
    assert payload["segments_state"] == "partial"
    empty_idx = next(
        s["index"] for s in payload["segments"] if not s["target_text"]
    )
    result = await _patch(novel_id, empty_idx, "save", after="Filled by hand.")
    assert result["segment"]["status"] == "edited"
    assert result["segment"]["aligned"] is True
    assert result["segments_state"] == "ok"
    body = _db_body(chapter_id)
    assert len(body.split("\n\n")) == 4
    assert body.split("\n\n")[empty_idx] == "Filled by hand."
    _assert_i1(chapter_id)
    # Reverting the hand-filled slot is REFUSED: its machine_text is ""
    # (the aligner had no paragraph for it), so there is nothing to revert
    # TO; a degenerate revert would drop the paragraph from the body again
    # (and on a refined chapter could empty the displayed body under a rev
    # stamped against ""). Empty-string machine_text rejects like NULL.
    with pytest.raises(segments_svc.SegmentActionError):
        await _patch(novel_id, empty_idx, "revert_machine")
    assert _db_body(chapter_id) == body  # untouched
    rows = _db_segments(chapter_id)
    assert rows[empty_idx][5] == "edited"  # status untouched
    _assert_i1(chapter_id)


# ---------------------------------------------------------------------------
# Phase 3: confirm_all
# ---------------------------------------------------------------------------


async def _confirm_all(
    novel_id: int,
    ch: int = 1,
    rev: str | None = None,
    statuses: list[str] | None = None,
) -> dict:
    payload = await _get(novel_id, ch)
    async with open_conn() as conn:
        result = await segments_svc.confirm_all(
            conn, novel_id, ch,
            client_rev=rev if rev is not None else (payload["chapter_rev"] or ""),
            statuses=statuses,
        )
        await conn.commit()
    return result


async def test_confirm_all_default_confirms_machine_and_edited():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 1, "save", after="Edited two.")
    result = await _confirm_all(novel_id)
    assert result["confirmed"] == 3
    assert result["progress"] == {"confirmed": 3, "total": 3}
    assert result["next_unconfirmed_index"] is None
    rows = _db_segments(chapter_id)
    assert all(r[5] == "confirmed" for r in rows)
    _assert_i1(chapter_id)


async def test_confirm_all_statuses_filter_and_validation():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 1, "save", after="Edited two.")
    result = await _confirm_all(novel_id, statuses=["edited"])
    assert result["confirmed"] == 1
    assert result["progress"] == {"confirmed": 1, "total": 3}
    rows = _db_segments(chapter_id)
    assert rows[1][5] == "confirmed"
    assert rows[0][5] == "machine" and rows[2][5] == "machine"
    with pytest.raises(segments_svc.SegmentActionError):
        await _confirm_all(novel_id, statuses=["confirmed"])
    with pytest.raises(segments_svc.SegmentActionError):
        await _confirm_all(novel_id, statuses=[])


async def test_confirm_all_rev_guard_and_empty_target_skip():
    src = "\n\n".join(
        [_zh_para("甲"), _zh_para("乙"), _zh_para("丙"), _zh_para("丁")]
    )
    tgt = "\n\n".join(["A" * 60, "B" * 60, "C" * 60])
    novel_id, chapter_id = await _seed_chapter(src, tgt)
    with pytest.raises(segments_svc.SegmentStaleError) as exc:
        await _confirm_all(novel_id, rev="f" * 16)
    assert exc.value.kind == "stale_chapter"
    result = await _confirm_all(novel_id)
    # The empty-target slot cannot be confirmed; the three real rows can.
    assert result["confirmed"] == 3
    assert result["progress"] == {"confirmed": 3, "total": 4}
    assert result["next_unconfirmed_index"] is not None
    _assert_i1(chapter_id)


# ---------------------------------------------------------------------------
# Phase 3: human-row preservation through rebuilds
# ---------------------------------------------------------------------------


async def test_self_heal_preserves_human_rows_positionally():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 1, "save", after="Human middle.")
    await _patch(novel_id, 2, "save_and_confirm", after="Human end.")
    row_ids_before = {r[1]: r[0] for r in _db_segments(chapter_id)}

    # Out-of-band body edit (same paragraph count): the self-heal rebuild
    # must keep the human rows' STATUS while the new body wins on text.
    new_body = "OOB first.\n\nOOB second.\n\nOOB third."
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translated_text = ? WHERE id = ?",
        (new_body, chapter_id),
    )
    conn.commit()
    conn.close()

    payload = await _get(novel_id)
    assert payload["segments_state"] == "ok"
    segs = payload["segments"]
    assert [s["target_text"] for s in segs] == [
        "OOB first.", "OOB second.", "OOB third.",
    ]
    assert [s["status"] for s in segs] == ["machine", "edited", "confirmed"]
    assert segs[1]["origin"] == "human"
    # The human DB rows survived (same primary keys); machine row rebuilt.
    row_ids_after = {r[1]: r[0] for r in _db_segments(chapter_id)}
    assert row_ids_after[1] == row_ids_before[1]
    assert row_ids_after[2] == row_ids_before[2]
    _assert_i1(chapter_id)


async def test_rebuild_reanchors_human_rows_by_source_hash():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 1, "save_and_confirm", after="Human for yi.")
    human_id = _db_segments(chapter_id)[1][0]

    # Swap the first two SOURCE paragraphs (and the body to match) and force
    # a rebuild: the human row must follow its source paragraph to index 0.
    paras = [_zh_para("乙"), _zh_para("甲"), _zh_para("丙")]
    swapped_src = "\n\n".join(paras)
    swapped_tgt = "Second paragraph.\n\nFirst paragraph.\n\nThird paragraph."
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET original_text = ?, translated_text = ?, "
        "segmentation_version = 0 WHERE id = ?",
        (swapped_src, swapped_tgt, chapter_id),
    )
    conn.commit()
    conn.close()

    payload = await _get(novel_id)
    assert payload["segments_state"] == "ok"
    seg0 = payload["segments"][0]
    assert seg0["status"] == "confirmed"
    assert seg0["source_text"] == _zh_para("乙")
    assert seg0["target_text"] == "Second paragraph."  # body-canonical text
    rows = _db_segments(chapter_id)
    assert rows[0][0] == human_id  # same DB row, moved to index 0
    assert [r[5] for r in rows] == ["confirmed", "machine", "machine"]
    _assert_i1(chapter_id)


async def test_alignment_failure_with_human_rows_retains_rows():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 0, "save_and_confirm", after="Kept work.")
    ids_before = [r[0] for r in _db_segments(chapter_id)]

    # Body collapses to one unalignable paragraph. Zero-row behavior would
    # delete everything; with human rows present ALL rows must be retained.
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translated_text = 'One lonely paragraph.' "
        "WHERE id = ?",
        (chapter_id,),
    )
    conn.commit()
    conn.close()

    payload = await _get(novel_id)
    assert payload["segments_state"] == "unaligned"
    assert len(payload["segments"]) == 3  # nothing vanished from the editor
    kept = payload["segments"][0]
    assert kept["status"] == "confirmed"
    assert kept["target_text"] == "Kept work."
    assert [r[0] for r in _db_segments(chapter_id)] == ids_before

    # The retained verdict is rev-gated: repeated reads are stable, no
    # rebuild churn.
    second = await _get(novel_id)
    assert second == payload
    assert [r[0] for r in _db_segments(chapter_id)] == ids_before


async def test_vanished_source_with_human_rows_retains_rows():
    novel_id, chapter_id = await _seed_chapter(_SRC_3, _TGT_3)
    await _patch(novel_id, 1, "save", after="Human work.")
    ids_before = [r[0] for r in _db_segments(chapter_id)]

    # The whole source is rewritten (every hash changes) and a rebuild is
    # forced: no anchor exists for the human row, so everything is retained
    # under an 'unaligned' verdict instead of rebuilding over it.
    new_src = "\n\n".join([_zh_para("戊"), _zh_para("己"), _zh_para("庚")])
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET original_text = ?, segmentation_version = 0 "
        "WHERE id = ?",
        (new_src, chapter_id),
    )
    conn.commit()
    conn.close()

    payload = await _get(novel_id)
    assert payload["segments_state"] == "unaligned"
    assert len(payload["segments"]) == 3
    assert payload["segments"][1]["status"] == "edited"
    assert [r[0] for r in _db_segments(chapter_id)] == ids_before


# ---------------------------------------------------------------------------
# Bug hunt 2026-08-04 (B13): UPDATE-0-rows window surfaces as stale_segment
# ---------------------------------------------------------------------------


class _RemintBeforeWriteConn:
    """Connection proxy that simulates a concurrent worker merge: right
    before update_segment's row UPDATE executes, the target row is deleted
    and re-inserted with a NEW id (same seg_index/content), so the
    UPDATE-by-stale-id writes 0 rows."""

    def __init__(self, conn, chapter_id: int, seg_index: int) -> None:
        self._conn = conn
        self._chapter_id = chapter_id
        self._seg_index = seg_index
        self._fired = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql, params=()):
        stripped = sql.lstrip()
        if (
            not self._fired
            and stripped.startswith("UPDATE chapter_segments")
            and "WHERE id = ?" in sql
        ):
            self._fired = True
            cur = await self._conn.execute(
                "SELECT novel_id, chapter_id, seg_index, source_text, "
                "source_hash, target_text, machine_text, status, origin, "
                "aligned FROM chapter_segments "
                "WHERE chapter_id = ? AND seg_index = ?",
                (self._chapter_id, self._seg_index),
            )
            row = await cur.fetchone()
            await self._conn.execute(
                "DELETE FROM chapter_segments "
                "WHERE chapter_id = ? AND seg_index = ?",
                (self._chapter_id, self._seg_index),
            )
            await self._conn.execute(
                "INSERT INTO chapter_segments (novel_id, chapter_id, "
                "seg_index, source_text, source_hash, target_text, "
                "machine_text, status, origin, aligned) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["novel_id"], row["chapter_id"], row["seg_index"],
                    row["source_text"], row["source_hash"],
                    row["target_text"], row["machine_text"], row["status"],
                    row["origin"], row["aligned"],
                ),
            )
        return await self._conn.execute(sql, params)


@pytest.mark.asyncio
@pytest.mark.parametrize("action,after", [
    ("save", "New text."),
    ("confirm", None),
    ("revert_machine", None),
])
async def test_reminted_row_write_raises_stale_segment(action, after):
    original = "\n\n".join([_zh_para("甲"), _zh_para("乙")])
    translated = "First paragraph here.\n\nSecond paragraph here."
    novel_id, chapter_id = await _seed_chapter(original, translated)
    payload = await _get(novel_id)
    seg = payload["segments"][0]
    if action == "revert_machine":
        # A machine row whose target diverged from machine_text (the only
        # revertable machine shape). The body is updated in lockstep so the
        # self-heal check (join(targets) == body) does not rebuild the store.
        conn_sync = sqlite3.connect(DB_PATH)
        conn_sync.execute(
            "UPDATE chapter_segments SET target_text = 'Prefilled text.' "
            "WHERE chapter_id = ? AND seg_index = 0",
            (chapter_id,),
        )
        conn_sync.execute(
            "UPDATE chapters SET translated_text = ? WHERE id = ?",
            ("Prefilled text.\n\nSecond paragraph here.", chapter_id),
        )
        conn_sync.commit()
        conn_sync.close()
        payload = await _get(novel_id)
        seg = payload["segments"][0]
        assert seg["machine_differs"] is True

    async with open_conn() as conn:
        proxied = _RemintBeforeWriteConn(conn, chapter_id, 0)
        with pytest.raises(segments_svc.SegmentStaleError) as exc_info:
            await segments_svc.update_segment(
                proxied, novel_id, 1, 0,
                action=action,
                after_text=after,
                client_rev=payload["chapter_rev"],
                before_target_hash=seg["target_hash"],
            )
    assert exc_info.value.kind == "stale_segment"


# ---------------------------------------------------------------------------
# Bug hunt 2026-08-04 (B8): chapter_id anti-renumber guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_carries_chapter_id():
    original = "\n\n".join([_zh_para("甲"), _zh_para("乙")])
    translated = "First paragraph here.\n\nSecond paragraph here."
    novel_id, chapter_id = await _seed_chapter(original, translated)
    payload = await _get(novel_id)
    assert payload["chapter_id"] == chapter_id


@pytest.mark.asyncio
async def test_chapter_id_mismatch_raises_stale_chapter_on_duplicate_content():
    """The duplicate-content shape: a mid-novel insert renumbers, and the
    chapter now at the loaded chapter_num has IDENTICAL content, so the
    rev + target-hash guards both pass on the WRONG row. The chapter_id
    echo is the guard that still catches it."""
    original = "\n\n".join([_zh_para("甲"), _zh_para("乙")])
    translated = "First paragraph here.\n\nSecond paragraph here."
    novel_id, chapter_id = await _seed_chapter(original, translated)
    payload = await _get(novel_id)  # loaded as chapter_num 1, id chapter_id
    seg = payload["segments"][0]

    # Renumber: the loaded chapter moves to num 2; a NEW chapter with the
    # same content takes num 1 (id differs).
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET chapter_num = 2 WHERE id = ?", (chapter_id,)
        )
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, "
            "title_en, original_text, translated_text, status) "
            "VALUES (?, 1, '第一章', 'Chapter 1', ?, ?, 'done')",
            (novel_id, original, translated),
        )
        imposter_id = cur.lastrowid
        await conn.commit()
    # Build the imposter's store; identical content means identical rev and
    # target hashes.
    imposter_payload = await _get(novel_id, 1)
    assert imposter_payload["chapter_id"] == imposter_id
    assert imposter_payload["chapter_rev"] == payload["chapter_rev"]

    # With the chapter_id echo: 409 stale_chapter, imposter untouched.
    async with open_conn() as conn:
        with pytest.raises(segments_svc.SegmentStaleError) as exc_info:
            await segments_svc.update_segment(
                conn, novel_id, 1, 0,
                action="save", after_text="Edited text.",
                client_rev=payload["chapter_rev"],
                before_target_hash=seg["target_hash"],
                chapter_id=chapter_id,
            )
    assert exc_info.value.kind == "stale_chapter"
    conn_sync = sqlite3.connect(DB_PATH)
    n_edited = conn_sync.execute(
        "SELECT COUNT(*) FROM chapter_segments WHERE chapter_id = ? "
        "AND status != 'machine'",
        (imposter_id,),
    ).fetchone()[0]
    conn_sync.close()
    assert n_edited == 0

    # Matching chapter_id (a fresh load of the renumbered list) succeeds.
    async with open_conn() as conn:
        result = await segments_svc.update_segment(
            conn, novel_id, 1, 0,
            action="save", after_text="Edited text.",
            client_rev=imposter_payload["chapter_rev"],
            before_target_hash=imposter_payload["segments"][0]["target_hash"],
            chapter_id=imposter_id,
        )
        await conn.commit()
    assert result["segment"]["status"] == "edited"


@pytest.mark.asyncio
async def test_confirm_all_chapter_id_mismatch_raises_stale_chapter():
    original = "\n\n".join([_zh_para("甲"), _zh_para("乙")])
    translated = "First paragraph here.\n\nSecond paragraph here."
    novel_id, chapter_id = await _seed_chapter(original, translated)
    payload = await _get(novel_id)
    async with open_conn() as conn:
        with pytest.raises(segments_svc.SegmentStaleError) as exc_info:
            await segments_svc.confirm_all(
                conn, novel_id, 1,
                client_rev=payload["chapter_rev"],
                chapter_id=chapter_id + 999,
            )
    assert exc_info.value.kind == "stale_chapter"

    # Omitting chapter_id keeps the pre-B8 behavior (additive contract).
    async with open_conn() as conn:
        result = await segments_svc.confirm_all(
            conn, novel_id, 1, client_rev=payload["chapter_rev"],
        )
        await conn.commit()
    assert result["confirmed"] == 2
