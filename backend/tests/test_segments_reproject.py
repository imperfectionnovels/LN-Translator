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
            client_rev=payload["chapter_rev"],
            before_target_hash=seg["target_hash"],
        )
        await conn.commit()


def _db_rows(chapter_id: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, seg_index, source_hash, target_text, machine_text, "
            "status, aligned, origin FROM chapter_segments "
            "WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        ).fetchall()
    finally:
        conn.close()


def _assert_i1(chapter_id: int) -> None:
    """Same shape as test_segments_service._assert_i1: join(non-empty
    targets) reproduces the displayed body AND the stamped segments_rev
    matches it."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ch = conn.execute(
            "SELECT translated_text, refined_text, refinement_status, "
            "segments_rev FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
    finally:
        conn.close()
    if ch["refined_text"]:  # presence keying, matches displayed_body
        body = ch["refined_text"]
    else:
        body = ch["translated_text"] or ""
    targets = [r["target_text"] for r in _db_rows(chapter_id)]
    assert "\n\n".join(t for t in targets if t) == \
        "\n\n".join(split_target_paragraphs(body))
    assert ch["segments_rev"] == segments_svc.chapter_rev(body)


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
async def test_glossary_alias_apply_stamps_reprojected_origin_preserving_status():
    """Block 1: the alias-aware apply is still a text-authoritative mutation
    routed through reproject_from_body, so the Phase 3 invariant (status
    survives) and the B3 provenance rule (a CHANGED human row is stamped
    origin='reprojected') both hold for an aliased rename. The single-owner
    rule is intact: no chapter_segments SQL was added to find_replace."""
    novel_id, chapter_id = await _seed_chapter()
    await _confirm(novel_id, 0)
    assert _db_rows(chapter_id)[0]["origin"] != "reprojected"

    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun / Xiaochun", new_en="Lord Bai",
            novel_id=novel_id,
        )
    assert result.chapters_updated == 1

    rows = _db_rows(chapter_id)
    assert rows[0]["status"] == "confirmed"  # Phase 3: status survives
    assert rows[0]["origin"] == "reprojected"  # B3: provenance does not
    assert rows[0]["target_text"] == "Lord Bai walked forward through the gate."
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
async def test_unaligned_retained_chapter_skips_positional_fast_path():
    """A retained-rows 'unaligned' chapter can coincidentally count-match
    the body (the retained rows have zero alignment confidence for it).
    The reproject hook must route it through the preservation-aware
    rebuild, never the positional in-place update, or the preserved human
    text would be overwritten by unrelated body paragraphs."""
    novel_id, chapter_id = await _seed_chapter()
    await _confirm(novel_id, 1)
    # Vanish the source (every hash changes) and force a rebuild: the
    # confirmed row cannot anchor, so ALL rows are retained as 'unaligned'
    # while the 3-paragraph body still count-matches the 3 retained rows.
    new_src = "\n\n".join([_zh_para("戊"), _zh_para("己"), _zh_para("庚")])
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET original_text = ?, segmentation_version = 0 "
        "WHERE id = ?",
        (new_src, chapter_id),
    )
    conn.commit()
    conn.close()
    payload = await _build_store(novel_id)
    assert payload["segments_state"] == "unaligned"
    assert len(payload["segments"]) == 3
    before = [dict(r) for r in _db_rows(chapter_id)]

    # Body-mutating find-replace fires the reproject hook. Row 0's target
    # contains the match; a positional fast path would overwrite it (and
    # stamp a fresh rev under the unaligned verdict).
    await _commit_replace(novel_id, "Bai Xiaochun", "Lord Bai")

    rows = _db_rows(chapter_id)
    assert [r["id"] for r in rows] == [r["id"] for r in before]
    assert rows[0]["target_text"] == before[0]["target_text"]  # untouched
    assert rows[1]["status"] == "confirmed"
    assert rows[1]["target_text"] == before[1]["target_text"]
    conn = sqlite3.connect(DB_PATH)
    state, body = conn.execute(
        "SELECT segments_state, translated_text FROM chapters WHERE id = ?",
        (chapter_id,),
    ).fetchone()
    conn.close()
    assert state == "unaligned"
    assert "Lord Bai" in body  # the replacement itself landed in the body


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


# ---------------------------------------------------------------------------
# Bug hunt 2026-08-04 (B12): divergent (tm_exact) machine rows keep their
# machine_text through text-authoritative mutations
# ---------------------------------------------------------------------------

_AI_RENDERING = "Bai Xiaochun walked forward through the gate."
_PREFILLED = "Lord Bai strode politely through the gate."


async def _make_seg0_divergent(novel_id: int, chapter_id: int) -> None:
    """Give seg 0 the tm_exact prefill shape: target = a confirmed
    cross-chapter rendering, machine_text = the AI's own suggestion. The
    chapter body is updated in lockstep so the self-heal join==body check
    holds (this mirrors what apply_machine_translation's prefill commit
    produces)."""
    await _build_store(novel_id)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapter_segments SET target_text = ?, origin = 'tm_exact' "
            "WHERE chapter_id = ? AND seg_index = 0",
            (_PREFILLED, chapter_id),
        )
        cur = await conn.execute(
            "SELECT target_text FROM chapter_segments "
            "WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        )
        body = "\n\n".join(r["target_text"] for r in await cur.fetchall())
        await conn.execute(
            "UPDATE chapters SET translated_text = ?, segments_rev = ? "
            "WHERE id = ?",
            (body, segments_svc.chapter_rev(body), chapter_id),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_fast_path_preserves_divergent_machine_text():
    """A find-replace over a tm_exact row rewrites the TARGET only; the
    stored AI rendering behind it survives, so revert-to-AI stays
    reachable."""
    novel_id, chapter_id = await _seed_chapter()
    await _make_seg0_divergent(novel_id, chapter_id)

    await _commit_replace(novel_id, "politely", "stiffly")

    rows = _db_rows(chapter_id)
    assert rows[0]["target_text"] == "Lord Bai strode stiffly through the gate."
    assert rows[0]["machine_text"] == _AI_RENDERING  # preserved
    assert rows[0]["status"] == "machine"
    _assert_i1(chapter_id)

    # revert_machine still reachable: the swap lands the AI rendering.
    payload = await _build_store(novel_id)
    seg0 = next(s for s in payload["segments"] if s["index"] == 0)
    assert seg0["machine_differs"] is True
    async with open_conn() as conn:
        result = await segments_svc.update_segment(
            conn, novel_id, 1, 0, action="revert_machine", after_text=None,
            client_rev=payload["chapter_rev"],
            before_target_hash=seg0["target_hash"],
        )
        await conn.commit()
    assert result["segment"]["target_text"] == _AI_RENDERING


@pytest.mark.asyncio
async def test_fast_path_still_refreshes_non_divergent_machine_rows():
    """Non-divergent machine rows (machine_text == target) keep following
    the body, exactly as before."""
    novel_id, chapter_id = await _seed_chapter()
    await _make_seg0_divergent(novel_id, chapter_id)
    await _commit_replace(novel_id, "sect elders", "sect ancients")
    rows = _db_rows(chapter_id)
    assert "sect ancients" in rows[1]["target_text"]
    assert rows[1]["machine_text"] == rows[1]["target_text"]
    # The divergent row was not hit and is untouched.
    assert rows[0]["machine_text"] == _AI_RENDERING
    _assert_i1(chapter_id)


@pytest.mark.asyncio
async def test_rebuild_path_carries_divergent_machine_text():
    """The count-drift fallback (a replacement containing a paragraph
    break) re-mints machine rows from the new body; the divergent AI
    rendering rides across keyed by source paragraph."""
    novel_id, chapter_id = await _seed_chapter()
    await _make_seg0_divergent(novel_id, chapter_id)

    # Split the LAST paragraph in two: the reproject fast path sees a count
    # mismatch and falls back to build_segments_from_alignment.
    await _commit_replace(
        novel_id, "as night arrived", "as night arrived.\n\nAll was still",
    )

    rows = _db_rows(chapter_id)
    row0 = next(r for r in rows if r["seg_index"] == 0)
    assert row0["target_text"].startswith("Lord Bai strode")
    assert row0["machine_text"] == _AI_RENDERING  # carried across the rebuild
    _assert_i1(chapter_id)
