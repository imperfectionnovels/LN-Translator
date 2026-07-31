"""CAT Phase 3: text-authoritative mutators re-sync the segment store.

`find_replace.commit_preview`, `find_replace.apply_in_place_for_glossary_term`
and `fr_snapshots.restore_snapshot` each call
`segments.reproject_from_body` per touched chapter inside their own
transaction. The invariants pinned here:

  - statuses survive (a novel-wide find-replace across confirmed segments
    must not un-confirm them);
  - the fast path refreshes machine_text only for machine rows (a human
    row's revert anchor keeps the AI's original rendering);
  - count drift (a replacement containing a paragraph break) falls back to
    the preservation-aware rebuild: human rows survive, re-anchored;
  - refined-variant chapters reproject their refined body;
  - chapters without segment rows are untouched (lazy build on next open);
  - I1 (join(targets) == displayed body) holds after every hook.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.services import find_replace as fr
from backend.services import segments as segments_svc
from backend.services.segmentation import split_target_paragraphs


@pytest.fixture(autouse=True)
def _reset_db_and_tokens():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    fr._reset_token_store_for_tests()
    yield


def _zh_para(ch: str, length: int = 30) -> str:
    return ch * (length - 1) + "。"


_SRC = "\n\n".join([_zh_para("甲"), _zh_para("乙"), _zh_para("丙")])
_TGT = (
    "Bai Xiaochun walked forward through the gate.\n\n"
    "The sect elders watched him in silence for a long time.\n\n"
    "Snow fell over the mountain as night arrived."
)


async def _seed_chapter(
    translated: str = _TGT,
    refined: str | None = None,
    refinement_status: str = "none",
) -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, translated_text, "
            " refined_text, refinement_status, status) "
            "VALUES (?, 1, ?, ?, ?, ?, 'done')",
            (novel_id, _SRC, translated, refined, refinement_status),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _build_store(novel_id: int) -> dict:
    async with open_conn() as conn:
        payload = await segments_svc.get_segments(conn, novel_id, 1)
        await conn.commit()
    return payload


async def _confirm(novel_id: int, seg_index: int) -> None:
    payload = await _build_store(novel_id)
    seg = next(s for s in payload["segments"] if s["index"] == seg_index)
    async with open_conn() as conn:
        await segments_svc.update_segment(
            conn, novel_id, 1, seg_index,
            action="confirm", after_text=None,
            chapter_rev=payload["chapter_rev"],
            before_target_hash=seg["target_hash"],
        )
        await conn.commit()


def _db_rows(chapter_id: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, seg_index, source_hash, target_text, machine_text, "
            "status, aligned FROM chapter_segments "
            "WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        ).fetchall()
    finally:
        conn.close()


def _assert_i1(chapter_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ch = conn.execute(
            "SELECT translated_text, refined_text, refinement_status "
            "FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
    finally:
        conn.close()
    if (ch["refinement_status"] or "none") == "done" and ch["refined_text"]:
        body = ch["refined_text"]
    else:
        body = ch["translated_text"] or ""
    targets = [r["target_text"] for r in _db_rows(chapter_id)]
    assert "\n\n".join(t for t in targets if t) == \
        "\n\n".join(split_target_paragraphs(body))


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


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_replace_commit_preserves_confirmed_status():
    novel_id, chapter_id = await _seed_chapter()
    await _confirm(novel_id, 0)
    result = await _commit_replace(novel_id, "Bai Xiaochun", "Lord Bai")
    assert result.chapters_updated == 1

    rows = _db_rows(chapter_id)
    assert rows[0]["status"] == "confirmed"  # NOT un-confirmed
    assert rows[0]["target_text"] == (
        "Lord Bai walked forward through the gate."
    )
    # Fast path refreshes machine_text for machine rows only: the confirmed
    # row keeps the AI's original rendering as its revert anchor.
    assert rows[0]["machine_text"] == (
        "Bai Xiaochun walked forward through the gate."
    )
    _assert_i1(chapter_id)

    # A later GET serves the reprojected rows without a rebuild (row ids
    # stable): the hook left the store in sync.
    ids = [r["id"] for r in _db_rows(chapter_id)]
    payload = await _build_store(novel_id)
    assert payload["segments_state"] == "ok"
    assert [r["id"] for r in _db_rows(chapter_id)] == ids


@pytest.mark.asyncio
async def test_find_replace_updates_machine_rows_machine_text():
    novel_id, chapter_id = await _seed_chapter()
    await _build_store(novel_id)
    await _commit_replace(novel_id, "sect elders", "sect ancients")
    rows = _db_rows(chapter_id)
    assert rows[1]["status"] == "machine"
    assert "sect ancients" in rows[1]["target_text"]
    assert rows[1]["machine_text"] == rows[1]["target_text"]
    _assert_i1(chapter_id)


@pytest.mark.asyncio
async def test_glossary_apply_in_place_preserves_statuses():
    novel_id, chapter_id = await _seed_chapter()
    await _confirm(novel_id, 0)
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Bai Hen", novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    rows = _db_rows(chapter_id)
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["target_text"].startswith("Bai Hen walked")
    _assert_i1(chapter_id)


@pytest.mark.asyncio
async def test_snapshot_restore_preserves_statuses():
    from backend.services.fr_snapshots import restore_snapshot
    novel_id, chapter_id = await _seed_chapter()
    await _confirm(novel_id, 0)
    commit = await _commit_replace(novel_id, "Bai Xiaochun", "Lord Bai")
    assert len(commit.snapshot_ids) == 1

    async with open_conn() as conn:
        result = await restore_snapshot(conn, commit.snapshot_ids[0])
    assert result["chapters_restored"] == 1
    rows = _db_rows(chapter_id)
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["target_text"] == (
        "Bai Xiaochun walked forward through the gate."
    )
    _assert_i1(chapter_id)


@pytest.mark.asyncio
async def test_count_drift_falls_back_to_preserving_rebuild():
    novel_id, chapter_id = await _seed_chapter()
    await _confirm(novel_id, 2)
    human_id = _db_rows(chapter_id)[2]["id"]

    # The replacement introduces a paragraph break: 3 -> 4 body paragraphs,
    # so the positional fast path is impossible and the preservation-aware
    # rebuild takes over. The confirmed row (untouched by the match) must
    # survive with its status, on the same DB row.
    await _commit_replace(
        novel_id, "walked forward", "walked.\n\nHe pressed forward"
    )
    conn = sqlite3.connect(DB_PATH)
    body, = conn.execute(
        "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,)
    ).fetchone()
    conn.close()
    assert len(body.split("\n\n")) == 4

    rows = _db_rows(chapter_id)
    assert len(rows) == 3  # one row per source paragraph, always
    assert rows[2]["status"] == "confirmed"
    assert rows[2]["id"] == human_id
    assert rows[2]["target_text"] == (
        "Snow fell over the mountain as night arrived."
    )
    # The split paragraphs landed somewhere (join reproduces the body).
    _assert_i1(chapter_id)


@pytest.mark.asyncio
async def test_refined_variant_chapter_reprojects_refined_body():
    refined = (
        "Polished Bai Xiaochun strode through the gate.\n\n"
        "The elders looked on without a word.\n\n"
        "Night snow settled over the peak."
    )
    novel_id, chapter_id = await _seed_chapter(
        refined=refined, refinement_status="done",
    )
    await _confirm(novel_id, 1)
    await _commit_replace(novel_id, "Bai Xiaochun", "Lord Bai")
    rows = _db_rows(chapter_id)
    # Segments mirror the DISPLAYED (refined) body.
    assert rows[0]["target_text"] == (
        "Polished Lord Bai strode through the gate."
    )
    assert rows[1]["status"] == "confirmed"
    _assert_i1(chapter_id)


@pytest.mark.asyncio
async def test_chapter_without_segments_is_untouched():
    novel_id, chapter_id = await _seed_chapter()
    # No editor open ever happened: zero rows. The hook must not build any.
    await _commit_replace(novel_id, "Bai Xiaochun", "Lord Bai")
    assert _db_rows(chapter_id) == []
    conn = sqlite3.connect(DB_PATH)
    state, = conn.execute(
        "SELECT segments_state FROM chapters WHERE id = ?", (chapter_id,)
    ).fetchone()
    conn.close()
    assert state is None
