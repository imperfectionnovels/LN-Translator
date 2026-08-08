"""Empty-target destruction guard + the mid-refinement write guard.

Two verified defects in services/segments.py:

  - `tm.full_alignment_path` legitimately emits ("", False) slots (a merge2
    follow-on row, a bare del). The preservation rebuild used to write that
    "" straight into an anchored human row, erasing the user's paragraph.
    The loss was SILENT (join_paragraphs skips empties, so the body still
    reproduced) but permanent: apply_machine_translation kept the empty
    target verbatim, so every later retranslate dropped the paragraph too.
    The rebuild now refuses instead, retaining every row under the
    'unaligned' verdict, and the merge HEALS an already-empty human target
    from the fresh machine text (origin='reprojected') so invariant I1 holds
    and live stores repair themselves.
  - `_load_editable_chapter` (the shared PATCH / confirm-all guard) had no
    mid-refinement check, so a save could land inside the window where the
    refine commit rematerializes the body: a 200 the paragraph did not
    survive. It now raises the same 'chapter_translating' 409 that
    get_segments' rebuild refusal mirrors.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.services import segments as segments_svc
from backend.services.segmentation import SEGMENTATION_VERSION


def _unlink_db_files() -> None:
    # WAL gotcha (docs/decisions.md): delete the -wal/-shm trio too.
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


# Chinese source paragraphs MUST end in terminal punctuation, or
# effective_source_paragraphs joins them (the mid-sentence pre-join).
def _zh_para(ch: str, length: int = 30) -> str:
    return ch * (length - 1) + "。"


_SRC_PARAS = [_zh_para("甲"), _zh_para("乙"), _zh_para("丙")]
_SRC = "\n\n".join(_SRC_PARAS)
_TGT_PARAS = [
    "Bai Xiaochun walked forward through the gate.",
    "The sect elders watched him in silence for a long time.",
    "Snow fell over the mountain as night arrived.",
]
_TGT = "\n\n".join(_TGT_PARAS)
_HUMAN = "The sect elders watched him, saying nothing at all."


async def _seed_chapter() -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, translated_text, status) "
            "VALUES (?, 1, ?, ?, 'done')",
            (novel_id, _SRC, _TGT),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _build_store(novel_id: int) -> dict:
    async with open_conn() as conn:
        payload = await segments_svc.get_segments(conn, novel_id, 1)
        await conn.commit()
    return payload


async def _act(
    novel_id: int, seg_index: int, action: str, after_text: str | None = None
) -> dict:
    payload = await _build_store(novel_id)
    seg = next(s for s in payload["segments"] if s["index"] == seg_index)
    async with open_conn() as conn:
        result = await segments_svc.update_segment(
            conn, novel_id, 1, seg_index,
            action=action, after_text=after_text,
            client_rev=payload["chapter_rev"],
            before_target_hash=seg["target_hash"],
        )
        await conn.commit()
    return result


def _db_rows(chapter_id: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT seg_index, source_text, target_text, machine_text, "
            "status, origin, aligned FROM chapter_segments "
            "WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        ).fetchall()
    finally:
        conn.close()


def _db_chapter(chapter_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT translated_text, refined_text, refinement_status, "
            "segments_state, segmentation_version, segments_rev "
            "FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
    finally:
        conn.close()


def _set_chapter(chapter_id: int, **cols) -> None:
    # Column names are test literals, never user input.
    assignments = ", ".join(f"{c} = ?" for c in cols)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            f"UPDATE chapters SET {assignments} WHERE id = ?",
            (*cols.values(), chapter_id),
        )
        conn.commit()
    finally:
        conn.close()


async def _confirmed_row_1(novel_id: int) -> None:
    """Put a confirmed, user-authored paragraph at seg_index 1."""
    await _act(novel_id, 1, "save_and_confirm", _HUMAN)


# The out-of-band body that forces the next read through the aligner: its
# paragraph count no longer matches the 3 source paragraphs, so the rebuild
# calls full_alignment_path instead of mapping positionally.
_DRIFTED_BODY = "Welded opening paragraph.\n\nWelded closing paragraph."


# ---------------------------------------------------------------------------
# Rebuild: an empty alignment slot must never erase a human row
# ---------------------------------------------------------------------------


async def test_rebuild_refuses_when_alignment_empties_a_confirmed_row(
    monkeypatch,
):
    """The merge2-follow-on / bare-del case: the aligner hands index 1 an
    empty string. Writing it would erase the confirmed paragraph, so the
    whole rebuild is refused: every row retained, chapter 'unaligned'."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _confirmed_row_1(novel_id)
    before = _db_rows(chapter_id)
    assert before[1]["target_text"] == _HUMAN

    _set_chapter(chapter_id, translated_text=_DRIFTED_BODY)

    def _fake_path(src, tgt):
        assert len(src) == 3 and len(tgt) == 2
        # Slot 1 is the follow-on row of a 2:1 merge: legitimately empty.
        return [("Welded opening paragraph.", False), ("", False),
                ("Welded closing paragraph.", True)]

    monkeypatch.setattr(
        segments_svc.tm_svc, "full_alignment_path", _fake_path
    )
    payload = await _build_store(novel_id)

    assert payload["segments_state"] == "unaligned"
    assert payload["aligned"] is False
    # Retained rows still reach the editor (read-only) rather than vanishing.
    assert len(payload["segments"]) == 3

    rows = _db_rows(chapter_id)
    assert len(rows) == 3
    # Nothing was rewritten: the confirmed paragraph is intact, verbatim.
    assert rows[1]["target_text"] == _HUMAN
    assert rows[1]["status"] == "confirmed"
    assert rows[1]["origin"] == "human"
    assert [r["target_text"] for r in rows] == [
        r["target_text"] for r in before
    ]

    ch = _db_chapter(chapter_id)
    assert ch["segments_state"] == "unaligned"
    assert ch["segmentation_version"] == SEGMENTATION_VERSION
    # Rev-gated like every other retained-rows verdict: reads stop re-running
    # the aligner until the body changes again.
    assert ch["segments_rev"] == segments_svc.chapter_rev(_DRIFTED_BODY)


async def test_rebuild_refuses_for_an_edited_row_too(monkeypatch):
    """Same guard for status='edited' (an unconfirmed save is user work
    exactly as much as a confirmed one)."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _act(novel_id, 1, "save", _HUMAN)
    _set_chapter(chapter_id, translated_text=_DRIFTED_BODY)

    monkeypatch.setattr(
        segments_svc.tm_svc,
        "full_alignment_path",
        lambda src, tgt: [("Welded opening paragraph.", False), ("", False),
                          ("Welded closing paragraph.", True)],
    )
    payload = await _build_store(novel_id)

    assert payload["segments_state"] == "unaligned"
    rows = _db_rows(chapter_id)
    assert rows[1]["target_text"] == _HUMAN
    assert rows[1]["status"] == "edited"


async def test_rebuild_still_reprojects_a_non_empty_slot(monkeypatch):
    """Regression pin: the guard is scoped to EMPTY slots. A rebuild whose
    slot carries real text stays text-authoritative exactly as before, B3
    stamp included."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _confirmed_row_1(novel_id)
    _set_chapter(chapter_id, translated_text=_DRIFTED_BODY)

    monkeypatch.setattr(
        segments_svc.tm_svc,
        "full_alignment_path",
        lambda src, tgt: [("Rebuilt one.", True), ("Rebuilt two.", True),
                          ("Rebuilt three.", True)],
    )
    payload = await _build_store(novel_id)

    assert payload["segments_state"] == "ok"
    rows = _db_rows(chapter_id)
    assert len(rows) == 3
    assert rows[1]["target_text"] == "Rebuilt two."
    # Status rides through (Phase 3 invariant); origin detaches (B3).
    assert rows[1]["status"] == "confirmed"
    assert rows[1]["origin"] == "reprojected"


async def test_rebuild_empty_slot_on_a_machine_row_is_unaffected(monkeypatch):
    """The guard reads human rows only: an empty slot over a machine row is
    the ordinary 'partial' outcome, not a refusal."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _confirmed_row_1(novel_id)
    _set_chapter(chapter_id, translated_text=_DRIFTED_BODY)

    monkeypatch.setattr(
        segments_svc.tm_svc,
        "full_alignment_path",
        # Empty slot at index 2, which is a machine row.
        lambda src, tgt: [("Welded opening paragraph.", False),
                          ("Welded closing paragraph.", True), ("", False)],
    )
    payload = await _build_store(novel_id)

    assert payload["segments_state"] == "partial"
    rows = _db_rows(chapter_id)
    assert rows[2]["target_text"] == ""
    assert rows[2]["status"] == "machine"
    # The human row took its (non-empty) slot text, so it was not refused.
    assert rows[1]["target_text"] == "Welded closing paragraph."


# ---------------------------------------------------------------------------
# Merge: an already-empty human target heals instead of dropping a paragraph
# ---------------------------------------------------------------------------


def _empty_the_human_target(chapter_id: int) -> None:
    """Reproduce the damage the pre-fix rebuild left on live stores."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE chapter_segments SET target_text = '' "
            "WHERE chapter_id = ? AND seg_index = 1",
            (chapter_id,),
        )
        conn.commit()
    finally:
        conn.close()


_FRESH_PARAS = [
    "Fresh gate paragraph.",
    "Fresh elders paragraph.",
    "Fresh snow paragraph.",
]


async def _merge(novel_id: int, chapter_id: int) -> list[str]:
    async with open_conn() as conn:
        merged = await segments_svc.apply_machine_translation(
            conn, novel_id=novel_id, chapter_id=chapter_id,
            new_paragraphs=list(_FRESH_PARAS), kind="llm",
            src_paras=_SRC_PARAS,
        )
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE id = ?",
            (segments_svc.join_paragraphs(merged), chapter_id),
        )
        await conn.commit()
    return merged


async def test_merge_heals_an_empty_confirmed_target(monkeypatch):
    """A confirmed row whose stored target is "" must not silently drop its
    paragraph from the merged body: the slot's fresh machine text fills it
    and the row is stamped 'reprojected' (status untouched)."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _confirmed_row_1(novel_id)
    _empty_the_human_target(chapter_id)

    merged = await _merge(novel_id, chapter_id)

    # I1: the paragraph is back in the body instead of being skipped.
    assert merged == _FRESH_PARAS
    assert segments_svc.join_paragraphs(merged).count("\n\n") == 2

    rows = _db_rows(chapter_id)
    assert rows[1]["target_text"] == "Fresh elders paragraph."
    assert rows[1]["origin"] == "reprojected"
    assert rows[1]["status"] == "confirmed"
    # The store now reproduces the committed body (self-healed).
    assert segments_svc.join_paragraphs(
        [r["target_text"] for r in rows]
    ) == _db_chapter(chapter_id)["translated_text"]


async def test_merge_heals_an_empty_target_on_the_alignment_path(monkeypatch):
    """Same heal through the drift path (full_alignment_path), where the
    entry is the aligner's rather than positional."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _confirmed_row_1(novel_id)
    _empty_the_human_target(chapter_id)

    monkeypatch.setattr(
        segments_svc.tm_svc,
        "full_alignment_path",
        lambda src, tgt: [("Fresh gate paragraph.", True),
                          ("Fresh elders paragraph.", True),
                          ("Fresh snow paragraph.", True)],
    )
    async with open_conn() as conn:
        merged = await segments_svc.apply_machine_translation(
            conn, novel_id=novel_id, chapter_id=chapter_id,
            new_paragraphs=["Fresh gate paragraph.",
                            "Fresh elders paragraph.",
                            "Fresh snow paragraph.",
                            "One extra machine paragraph."],
            kind="llm", src_paras=_SRC_PARAS,
        )
        await conn.commit()

    assert merged[1] == "Fresh elders paragraph."
    rows = _db_rows(chapter_id)
    assert rows[1]["target_text"] == "Fresh elders paragraph."
    assert rows[1]["origin"] == "reprojected"
    assert rows[1]["status"] == "confirmed"


async def test_merge_keeps_a_non_empty_human_target_verbatim():
    """Regression pin around the heal: the normal merge is untouched. The
    human target rides through verbatim, origin stays 'human', and only
    machine_text refreshes."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _confirmed_row_1(novel_id)

    merged = await _merge(novel_id, chapter_id)

    assert merged[1] == _HUMAN
    rows = _db_rows(chapter_id)
    assert rows[1]["target_text"] == _HUMAN
    assert rows[1]["origin"] == "human"
    assert rows[1]["status"] == "confirmed"
    assert rows[1]["machine_text"] == "Fresh elders paragraph."


async def test_merge_leaves_an_empty_target_alone_with_no_machine_text():
    """Nothing to heal with: an empty slot over the empty human target keeps
    the row as it is rather than inventing text."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _confirmed_row_1(novel_id)
    _empty_the_human_target(chapter_id)

    async with open_conn() as conn:
        merged = await segments_svc.apply_machine_translation(
            conn, novel_id=novel_id, chapter_id=chapter_id,
            new_paragraphs=["Fresh gate paragraph.", "",
                            "Fresh snow paragraph."],
            kind="llm", src_paras=_SRC_PARAS,
        )
        await conn.commit()

    assert merged[1] == ""
    rows = _db_rows(chapter_id)
    assert rows[1]["target_text"] == ""
    assert rows[1]["origin"] == "human"
    assert rows[1]["status"] == "confirmed"


# ---------------------------------------------------------------------------
# Mid-refinement write guard
# ---------------------------------------------------------------------------


async def _save_expecting(novel_id: int, kind: str) -> str:
    payload = await _build_store(novel_id)
    seg = next(s for s in payload["segments"] if s["index"] == 1)
    with pytest.raises(segments_svc.SegmentStaleError) as excinfo:
        async with open_conn() as conn:
            await segments_svc.update_segment(
                conn, novel_id, 1, 1,
                action="save", after_text=_HUMAN,
                client_rev=payload["chapter_rev"],
                before_target_hash=seg["target_hash"],
            )
    assert excinfo.value.kind == kind
    return str(excinfo.value)


@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_save_refused_while_refinement_is_in_flight(status):
    """The refine commit is about to rematerialize the body from its own
    merge, so a save landing now would be discarded after a 200. Refuse it
    with the 409 kind the editor already handles."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    _set_chapter(chapter_id, refinement_status=status)

    message = await _save_expecting(novel_id, "chapter_translating")
    assert "refined" in message

    # The paragraph was not written.
    assert _db_rows(chapter_id)[1]["target_text"] == _TGT_PARAS[1]


@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_confirm_all_refused_while_refinement_is_in_flight(status):
    novel_id, chapter_id = await _seed_chapter()
    payload = await _build_store(novel_id)
    _set_chapter(chapter_id, refinement_status=status)

    with pytest.raises(segments_svc.SegmentStaleError) as excinfo:
        async with open_conn() as conn:
            await segments_svc.confirm_all(
                conn, novel_id, 1, client_rev=payload["chapter_rev"]
            )
    assert excinfo.value.kind == "chapter_translating"
    assert all(r["status"] == "machine" for r in _db_rows(chapter_id))


@pytest.mark.parametrize("status", ["none", "done", "error"])
async def test_writes_allowed_outside_the_refinement_window(status):
    """Settled states stay editable, including 'error' (a failed retry keeps
    its retained refined body displayed, and editing it is the recovery)."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    _set_chapter(chapter_id, refinement_status=status)

    result = await _act(novel_id, 1, "save", _HUMAN)
    assert result["segment"]["target_text"] == _HUMAN
    assert _db_rows(chapter_id)[1]["target_text"] == _HUMAN
