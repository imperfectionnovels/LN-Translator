"""Bug hunt 2026-08-04, the provenance cluster (B3+F1+F2+B14).

chapter_segments.origin is real provenance now:

  - B3: any text-authoritative write (reproject fast path, preservation
    rebuild) that CHANGES a human row's target stamps origin='reprojected'
    (status untouched). The restore-poison repro (snapshot restore swapping a
    confirmed row's target back to machine text) therefore mints NO style
    pair and NO exemplar. A later per-row save/confirm restores 'human'.
  - F2: style-pair feeds (recent_edited_pairs, edited_pairs_for_chapter)
    require origin='human', so confirm-all over tm_exact prefills stops
    minting fake (fresh-AI, old-confirmed) pairs. Exemplars keep
    status='confirmed' semantics (confirm-as-is = endorsement) but exclude
    'reprojected'.
  - F1+B14: apply_machine_translation honors the alignment entry's
    confidence for human rows: aligned column takes the entry's flag,
    machine_text refreshes only from confident slots, and segments_state
    can demote to 'partial' through a human row.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.services import find_replace as fr
from backend.services import segments as segments_svc
from backend.services.fr_snapshots import restore_snapshot

pytestmark = pytest.mark.asyncio


def _unlink_db_files() -> None:
    # WAL gotcha (docs/decisions.md): delete the -wal/-shm trio, not just the
    # main file, and clean up on teardown.
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH.parent / (DB_PATH.name + suffix)
        if p.exists():
            p.unlink()


@pytest.fixture(autouse=True)
def _reset_db_and_tokens():
    _unlink_db_files()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    fr._reset_token_store_for_tests()
    yield
    _unlink_db_files()


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


async def _seed_chapter(chapter_num: int = 1) -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, translated_text, status) "
            "VALUES (?, ?, ?, ?, 'done')",
            (novel_id, chapter_num, _SRC, _TGT),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _build_store(novel_id: int, chapter_num: int = 1) -> dict:
    async with open_conn() as conn:
        payload = await segments_svc.get_segments(conn, novel_id, chapter_num)
        await conn.commit()
    return payload


async def _act(
    novel_id: int, seg_index: int, action: str,
    after_text: str | None = None, chapter_num: int = 1,
) -> dict:
    payload = await _build_store(novel_id, chapter_num)
    seg = next(s for s in payload["segments"] if s["index"] == seg_index)
    async with open_conn() as conn:
        result = await segments_svc.update_segment(
            conn, novel_id, chapter_num, seg_index,
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
            "SELECT id, seg_index, target_text, machine_text, status, "
            "origin, aligned FROM chapter_segments "
            "WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        ).fetchall()
    finally:
        conn.close()


async def _commit_replace(
    novel_id: int, find: str, replacement: str
) -> fr.CommitResult:
    query = fr.FindReplaceQuery(
        find=find, replacement=replacement,
        scope_kind="novel", scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
        return await fr.commit_preview(conn, preview.token)


async def _pairs(novel_id: int, exclude_chapter_id: int = 0):
    async with open_conn() as conn:
        return await segments_svc.recent_edited_pairs(
            conn, novel_id, exclude_chapter_id, 10
        )


async def _exemplars(novel_id: int, exclude_chapter_id: int = 0):
    async with open_conn() as conn:
        return await segments_svc.fetch_confirmed_exemplar_pairs(
            conn, novel_id, exclude_chapter_id, 10
        )


# ---------------------------------------------------------------------------
# B3: the restore-poison repro
# ---------------------------------------------------------------------------


async def test_restore_poison_mints_no_pair_and_no_exemplar():
    """The exact B3 repro: find-replace, then a human confirm of the new
    text, then a snapshot restore that swaps the confirmed row's target back
    to machine text. The (M2, M1) machine-vs-machine delta must produce NO
    style pair and NO exemplar; status stays confirmed; origin records
    'reprojected'."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    commit = await _commit_replace(novel_id, "Bai Xiaochun", "Lord Bai")
    assert len(commit.snapshot_ids) == 1

    # The user rewrites paragraph 0 by hand and confirms it: a genuine edit.
    human_text = "My own careful phrasing of the gate scene."
    await _act(novel_id, 0, "save_and_confirm", human_text)
    rows = _db_rows(chapter_id)
    assert rows[0]["origin"] == "human"
    machine_before_restore = rows[0]["machine_text"]  # M2: "Lord Bai ..."
    assert (machine_before_restore, human_text) in await _pairs(novel_id)
    assert any(en == human_text for _zh, en in await _exemplars(novel_id))

    # Snapshot restore: the body reverts to the pre-replace machine text.
    async with open_conn() as conn:
        result = await restore_snapshot(conn, commit.snapshot_ids[0])
    assert result["chapters_restored"] == 1

    rows = _db_rows(chapter_id)
    assert rows[0]["status"] == "confirmed"          # status untouched
    assert rows[0]["origin"] == "reprojected"        # provenance recorded
    assert rows[0]["target_text"] == _TGT_PARAS[0]   # M1, machine text
    # The poisoned (M2, M1) delta ships nowhere:
    assert await _pairs(novel_id) == []
    assert await _exemplars(novel_id) == []
    # learn_from_edits' per-chapter feed is gated identically.
    async with open_conn() as conn:
        assert await segments_svc.edited_pairs_for_chapter(conn, chapter_id) == []


async def test_reproject_fast_path_stamps_only_changed_human_rows():
    """A find-replace that hits one confirmed row's text demotes THAT row to
    'reprojected'; a confirmed row the replacement does not touch keeps
    origin='human'; machine rows keep their machine origin."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _act(novel_id, 0, "save_and_confirm",
               "Bai Xiaochun stepped through the gate at last.")
    await _act(novel_id, 2, "save_and_confirm",
               "Snowfall wrapped the mountain in the dark.")

    await _commit_replace(novel_id, "Bai Xiaochun", "Lord Bai")

    rows = _db_rows(chapter_id)
    assert rows[0]["origin"] == "reprojected"  # text changed by the rewrite
    assert rows[0]["status"] == "confirmed"
    assert rows[2]["origin"] == "human"        # untouched row keeps provenance
    assert rows[1]["status"] == "machine"
    assert rows[1]["origin"] == "aligned_backfill"


async def test_rebuild_path_stamps_only_changed_slots():
    """The preservation-aware rebuild (out-of-band body edit detected by the
    self-heal) is text-authoritative too: a human row whose slot text changed
    stamps 'reprojected'; one whose slot text is unchanged keeps 'human'."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _act(novel_id, 0, "save_and_confirm", "Human gate paragraph.")
    await _act(novel_id, 2, "save", "Human snow paragraph.")

    # Out-of-band body write (same paragraph count, so the self-heal
    # rebuild maps positionally and the per-slot outcome is deterministic):
    # paragraph 0 replaced, paragraph 2 kept verbatim.
    new_body = "\n\n".join([
        "Restored machine gate paragraph.",
        _TGT_PARAS[1],
        "Human snow paragraph.",
    ])
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translated_text = ?, segments_rev = 'stale' "
        "WHERE id = ?",
        (new_body, chapter_id),
    )
    conn.commit()
    conn.close()

    payload = await _build_store(novel_id)
    assert payload["segments_state"] == "ok"
    rows = _db_rows(chapter_id)
    row0 = next(r for r in rows if r["seg_index"] == 0)
    row2 = next(r for r in rows if r["seg_index"] == 2)
    assert row0["origin"] == "reprojected"
    assert row0["status"] == "confirmed"
    assert row2["origin"] == "human"
    assert row2["target_text"] == "Human snow paragraph."


async def test_save_and_confirm_restore_human_origin():
    """A later human write re-earns provenance: save on a reprojected row
    stamps 'human'; unconfirm+confirm on a reprojected row stamps 'human'
    (the user re-vouched with eyes on the row); both re-enter the feeds."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    commit = await _commit_replace(novel_id, "Bai Xiaochun", "Lord Bai")
    await _act(novel_id, 0, "save_and_confirm", "Hand-written gate scene.")
    await _act(novel_id, 2, "save_and_confirm", "Hand-written snow scene.")
    async with open_conn() as conn:
        await restore_snapshot(conn, commit.snapshot_ids[0])
    rows = _db_rows(chapter_id)
    assert rows[0]["origin"] == "reprojected"
    assert rows[2]["origin"] == "reprojected"

    # Row 0: the user rewrites it again (save).
    await _act(novel_id, 0, "save", "Second hand-written gate scene.")
    # Row 2: the user re-endorses the restored text as-is.
    await _act(novel_id, 2, "unconfirm")
    await _act(novel_id, 2, "confirm")

    rows = _db_rows(chapter_id)
    assert rows[0]["origin"] == "human"
    assert rows[0]["status"] == "edited"
    assert rows[2]["origin"] == "human"
    assert rows[2]["status"] == "confirmed"
    pairs = await _pairs(novel_id)
    assert any(after == "Second hand-written gate scene." for _b, after in pairs)
    assert any(en == _TGT_PARAS[2] for _zh, en in await _exemplars(novel_id))


async def test_confirm_as_is_keeps_machine_origin():
    """Plain confirm on machine-origin rows must NOT stamp 'human' (or
    confirm-all sweeps would launder tm_exact rows into fake style pairs);
    revert_machine returns origin to 'llm'."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapter_segments SET origin = 'tm_exact' "
        "WHERE chapter_id = ? AND seg_index = 1",
        (chapter_id,),
    )
    conn.commit()
    conn.close()

    await _act(novel_id, 0, "confirm")
    await _act(novel_id, 1, "confirm")
    rows = _db_rows(chapter_id)
    assert rows[0]["origin"] == "aligned_backfill"
    assert rows[1]["origin"] == "tm_exact"
    assert rows[0]["status"] == rows[1]["status"] == "confirmed"

    # revert_machine on a human row goes back to a machine origin.
    await _act(novel_id, 2, "save", "Hand-written line.")
    await _act(novel_id, 2, "revert_machine")
    rows = _db_rows(chapter_id)
    assert rows[2]["origin"] == "llm"
    assert rows[2]["status"] == "machine"


# ---------------------------------------------------------------------------
# F2: confirm-all over tm_exact prefills
# ---------------------------------------------------------------------------


async def test_confirm_all_over_tm_exact_mints_no_pairs():
    """confirm_all flips a divergent tm_exact machine row (fresh AI
    machine_text behind a cross-chapter confirmed target) to confirmed with
    a fresh confirmed_at. It must NOT mint a style pair; it MAY still serve
    as an exemplar (confirm-as-is is endorsement). A genuine edited row
    swept by the same confirm-all keeps minting its pair."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    # Row 1 becomes a divergent tm_exact prefill: target = the confirmed
    # cross-chapter rendering, machine_text = the AI's own suggestion.
    prefilled = "The elders looked on, saying nothing."
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapter_segments SET origin = 'tm_exact', target_text = ? "
        "WHERE chapter_id = ? AND seg_index = 1",
        (prefilled, chapter_id),
    )
    cur = conn.execute(
        "SELECT target_text FROM chapter_segments "
        "WHERE chapter_id = ? ORDER BY seg_index",
        (chapter_id,),
    )
    body = "\n\n".join(r[0] for r in cur.fetchall())
    conn.execute(
        "UPDATE chapters SET translated_text = ?, segments_rev = ? "
        "WHERE id = ?",
        (body, segments_svc.chapter_rev(body), chapter_id),
    )
    conn.commit()
    conn.close()

    # A genuine edit on row 2 rides along in the sweep.
    await _act(novel_id, 2, "save", "A genuine human correction.")

    payload = await _build_store(novel_id)
    async with open_conn() as conn:
        await segments_svc.confirm_all(
            conn, novel_id, 1, client_rev=payload["chapter_rev"]
        )
        await conn.commit()

    rows = _db_rows(chapter_id)
    assert all(r["status"] == "confirmed" for r in rows)
    assert rows[1]["origin"] == "tm_exact"
    assert rows[1]["machine_text"] != rows[1]["target_text"]

    pairs = await _pairs(novel_id)
    # No fake (fresh-AI, old-confirmed) pair from the tm_exact row.
    assert all(after != prefilled for _b, after in pairs)
    # The genuine correction still teaches.
    assert any(after == "A genuine human correction." for _b, after in pairs)
    # Exemplars: the tm_exact confirmation still counts as endorsement.
    assert any(en == prefilled for _zh, en in await _exemplars(novel_id))


# ---------------------------------------------------------------------------
# F1 + B14: merge confidence for human rows
# ---------------------------------------------------------------------------


async def test_merge_honors_alignment_confidence_for_human_rows(monkeypatch):
    """On a count-drifted merge, a human row anchored to an UNCONFIDENT slot
    keeps its old machine_text and demotes aligned=0 (chapter 'partial'); a
    human row on a confident slot refreshes machine_text as before. The
    style-pair feed then carries the true old rendering, not the misaligned
    paragraph."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _act(novel_id, 0, "save", "Human gate rewrite.")
    await _act(novel_id, 1, "save", "Human elders rewrite.")
    old_machine_1 = _db_rows(chapter_id)[1]["machine_text"]

    new_paras = [
        "Fresh machine gate paragraph.",
        "Fresh machine elders paragraph, first half.",
        "Fresh machine elders paragraph, second half.",
        "Fresh machine snow paragraph.",
    ]

    def _fake_path(src, tgt):
        assert len(src) == 3 and len(tgt) == 4
        return [
            ("Fresh machine gate paragraph.", True),
            ("Fresh machine elders paragraph, first half.", False),
            ("Fresh machine snow paragraph.", True),
        ]

    monkeypatch.setattr(
        segments_svc.tm_svc, "full_alignment_path", _fake_path
    )
    async with open_conn() as conn:
        merged = await segments_svc.apply_machine_translation(
            conn, novel_id=novel_id, chapter_id=chapter_id,
            new_paragraphs=new_paras, kind="llm", src_paras=_SRC_PARAS,
        )
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE id = ?",
            (segments_svc.join_paragraphs(merged), chapter_id),
        )
        await conn.commit()

    # Human targets survive verbatim in the merged body.
    assert merged[0] == "Human gate rewrite."
    assert merged[1] == "Human elders rewrite."

    rows = _db_rows(chapter_id)
    # Confident slot: machine_text refreshed, aligned stays 1.
    assert rows[0]["machine_text"] == "Fresh machine gate paragraph."
    assert rows[0]["aligned"] == 1
    # Unconfident slot: OLD machine_text kept, aligned demoted (F1), and the
    # chapter state reflects it (B14).
    assert rows[1]["machine_text"] == old_machine_1
    assert rows[1]["aligned"] == 0
    conn = sqlite3.connect(DB_PATH)
    state, = conn.execute(
        "SELECT segments_state FROM chapters WHERE id = ?", (chapter_id,)
    ).fetchone()
    conn.close()
    assert state == "partial"

    # The style feed teaches from the true old rendering.
    pairs = await _pairs(novel_id)
    assert (old_machine_1[:400], "Human elders rewrite.") in pairs


async def test_merge_equal_counts_still_refreshes_and_aligns():
    """Regression guard around the F1 change: the common 1:1 merge (equal
    counts, every entry confident) behaves exactly as before: human target
    verbatim, machine_text refreshed, aligned=1, state 'ok'."""
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _act(novel_id, 1, "save", "Human elders rewrite.")

    new_paras = [
        "Fresh gate paragraph.",
        "Fresh elders paragraph.",
        "Fresh snow paragraph.",
    ]
    async with open_conn() as conn:
        merged = await segments_svc.apply_machine_translation(
            conn, novel_id=novel_id, chapter_id=chapter_id,
            new_paragraphs=new_paras, kind="llm", src_paras=_SRC_PARAS,
        )
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE id = ?",
            (segments_svc.join_paragraphs(merged), chapter_id),
        )
        await conn.commit()
    assert merged[1] == "Human elders rewrite."
    rows = _db_rows(chapter_id)
    assert rows[1]["machine_text"] == "Fresh elders paragraph."
    assert rows[1]["aligned"] == 1
    conn = sqlite3.connect(DB_PATH)
    state, = conn.execute(
        "SELECT segments_state FROM chapters WHERE id = ?", (chapter_id,)
    ).fetchone()
    conn.close()
    assert state == "ok"


# ---------------------------------------------------------------------------
# learn_from_edits per-chapter feed
# ---------------------------------------------------------------------------


async def test_edited_pairs_for_chapter_requires_human_origin():
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _act(novel_id, 0, "save", "Genuine correction.")
    conn = sqlite3.connect(DB_PATH)
    # Forge the two non-human shapes on the other rows: a reprojected edited
    # row and a tm_exact confirmed row, both with machine != target.
    conn.execute(
        "UPDATE chapter_segments SET status = 'edited', "
        "origin = 'reprojected', machine_text = 'Other rendering A.' "
        "WHERE chapter_id = ? AND seg_index = 1",
        (chapter_id,),
    )
    conn.execute(
        "UPDATE chapter_segments SET status = 'confirmed', "
        "origin = 'tm_exact', machine_text = 'Other rendering B.', "
        "confirmed_at = datetime('now') "
        "WHERE chapter_id = ? AND seg_index = 2",
        (chapter_id,),
    )
    conn.commit()
    conn.close()
    async with open_conn() as conn:
        pairs = await segments_svc.edited_pairs_for_chapter(conn, chapter_id)
    assert [after for _b, after in pairs] == ["Genuine correction."]
