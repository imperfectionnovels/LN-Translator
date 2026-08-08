"""Initiative 4 — find/replace engine tests.

The load-bearing invariant from the plan: the preview token freezes the
matched chapter set + content hashes; commit refuses if any chapter has
changed since. These tests pin that contract plus the surrounding
input-validation behavior.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.services import find_replace as fr


@pytest.fixture(autouse=True)
def _reset_db_and_tokens():
    """Each test gets a fresh DB and an empty token store. Reset both so
    cross-test state doesn't leak through the process-local dict."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    fr._reset_token_store_for_tests()
    yield


async def _seed_novel_with_chapters(payload: list[tuple[int, str, str | None]]) -> int:
    """Insert one novel + N done chapters. Returns the novel id.

    `payload` is a list of (chapter_num, translated_text, refined_text)."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, source_url) "
            "VALUES (?, ?, NULL)",
            ("TestNovel", "paste"),
        )
        novel_id = cur.lastrowid
        for chapter_num, translated, refined in payload:
            await conn.execute(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, original_text, translated_text, "
                "refined_text, status) "
                "VALUES (?, ?, ?, ?, ?, 'done')",
                (novel_id, chapter_num, "原文", translated, refined),
            )
        await conn.commit()
    return novel_id


# ---- Preview shape -------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_counts_hits_and_issues_token():
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked. Bai Xiaochun smiled.", None),
        (2, "The sect.", None),
        (3, "Bai Xiaochun spoke.", "Bai Xiaochun spoke (refined)."),
    ])
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun",
        replacement="Bai Xiao Chun",
        scope_kind="novel",
        scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        result = await fr.build_preview(conn, query)
    assert result.token  # non-empty
    assert result.total_chapters == 2  # ch 1 + ch 3 have matches
    # ch 1: 2 hits in translated, 0 in refined; ch 3: 1+1
    assert result.total_hits_translated == 3
    assert result.total_hits_refined == 1


# ---- Drift detection -----------------------------------------------------


@pytest.mark.asyncio
async def test_commit_succeeds_when_no_drift():
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun",
        replacement="Bai Xiao Chun",
        scope_kind="novel",
        scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
        result = await fr.commit_preview(conn, preview.token)
    assert result.chapters_updated == 1
    assert result.rows_updated_translated == 1
    # Verify the substitution actually landed.
    async with open_conn() as conn:
        cur = await conn.execute("SELECT translated_text FROM chapters WHERE chapter_num = 1")
        row = await cur.fetchone()
    assert row["translated_text"] == "Bai Xiao Chun walked."


@pytest.mark.asyncio
async def test_commit_refuses_when_chapter_drifts_between_preview_and_commit():
    """The viability invariant: any chapter content change between
    preview and commit refuses the commit. Otherwise a background
    translation finishing mid-flow would silently rewrite text the user
    didn't see."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
        (2, "Bai Xiaochun spoke.", None),
    ])
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun",
        replacement="Bai Xiao Chun",
        scope_kind="novel",
        scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)

    # Simulate a concurrent edit to chapter 2 (translator finished while
    # the user was reading the preview).
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE chapter_num = 2",
            ("Bai Xiaochun spoke politely.",),
        )
        await conn.commit()

    async with open_conn() as conn:
        with pytest.raises(fr.PreviewDriftError) as exc_info:
            await fr.commit_preview(conn, preview.token)
    assert len(exc_info.value.drifted_chapter_ids) == 1


@pytest.mark.asyncio
async def test_commit_drift_does_not_partially_apply():
    """Drift on ANY chapter aborts the WHOLE commit — no partial writes.
    Without this, a single drifted chapter would silently leave the
    novel half-substituted."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
        (2, "Bai Xiaochun spoke.", None),
    ])
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun", replacement="Bai Xiao Chun",
        scope_kind="novel", scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE chapter_num = 2",
            ("Bai Xiaochun spoke politely.",),
        )
        await conn.commit()
    async with open_conn() as conn:
        with pytest.raises(fr.PreviewDriftError):
            await fr.commit_preview(conn, preview.token)
    # Chapter 1 should still be untouched — no partial write.
    async with open_conn() as conn:
        cur = await conn.execute("SELECT translated_text FROM chapters WHERE chapter_num = 1")
        row = await cur.fetchone()
    assert row["translated_text"] == "Bai Xiaochun walked."


# ---- Token lifecycle -----------------------------------------------------


@pytest.mark.asyncio
async def test_commit_with_unknown_token_raises():
    async with open_conn() as conn:
        with pytest.raises(fr.TokenExpiredError):
            await fr.commit_preview(conn, "no-such-token")


@pytest.mark.asyncio
async def test_token_is_single_use(monkeypatch):
    """A successful commit consumes the token — a replay returns 410."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun", replacement="Bai Xiao Chun",
        scope_kind="novel", scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
        await fr.commit_preview(conn, preview.token)
        with pytest.raises(fr.TokenExpiredError):
            await fr.commit_preview(conn, preview.token)


@pytest.mark.asyncio
async def test_expired_token_raises(monkeypatch):
    """TTL expiry path. We patch the engine's clock so the test runs
    instantly."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun", replacement="Bai Xiao Chun",
        scope_kind="novel", scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
    # Pretend the wall clock jumped forward past TTL.
    monkeypatch.setattr(
        fr, "_now",
        lambda: fr._PREVIEW_STORE[preview.token].created_at
                 + fr.PREVIEW_TOKEN_TTL_SECONDS + 1,
    )
    async with open_conn() as conn:
        with pytest.raises(fr.TokenExpiredError):
            await fr.commit_preview(conn, preview.token)


# ---- Input validation ----------------------------------------------------


@pytest.mark.asyncio
async def test_empty_find_string_rejected():
    async with open_conn() as conn:
        with pytest.raises(fr.InvalidPatternError):
            await fr.build_preview(
                conn,
                fr.FindReplaceQuery(find="", replacement="x", scope_kind="all"),
            )


@pytest.mark.asyncio
async def test_invalid_regex_rejected():
    async with open_conn() as conn:
        with pytest.raises(fr.InvalidPatternError):
            await fr.build_preview(
                conn,
                fr.FindReplaceQuery(
                    find="(unclosed", replacement="x",
                    scope_kind="all", use_regex=True,
                ),
            )


@pytest.mark.asyncio
async def test_capture_group_in_replacement_rejected():
    """v1 explicitly forbids \\1 / \\g<…> replacements — the engine
    surface stays simple. This guard prevents accidental capture-based
    rewrites from sneaking through."""
    async with open_conn() as conn:
        with pytest.raises(fr.InvalidPatternError):
            await fr.build_preview(
                conn,
                fr.FindReplaceQuery(
                    find=r"(\w+)", replacement=r"\1 prefixed",
                    scope_kind="all", use_regex=True,
                ),
            )


# ---- In-place glossary helper -------------------------------------------


@pytest.mark.asyncio
async def test_apply_in_place_for_glossary_term_word_boundary():
    """The glossary helper uses word-boundary matching so 'Bai Xiaochun'
    doesn't ripple into 'Bai Xiaochuns' or substrings."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked. Bai Xiaochuns' clan watched.", None),
    ])
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Bai Xiao Chun",
            novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    async with open_conn() as conn:
        cur = await conn.execute("SELECT translated_text FROM chapters WHERE chapter_num = 1")
        row = await cur.fetchone()
    # The bare "Bai Xiaochun" became "Bai Xiao Chun". The possessive form
    # "Bai Xiaochuns'" stayed untouched because \\b doesn't fire mid-word.
    assert row["translated_text"] == "Bai Xiao Chun walked. Bai Xiaochuns' clan watched."


@pytest.mark.asyncio
async def test_apply_in_place_is_a_noop_when_old_equals_new():
    novel_id = await _seed_novel_with_chapters([(1, "Bai Xiaochun walked.", None)])
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Bai Xiaochun",
            novel_id=novel_id,
        )
    assert result.chapters_updated == 0


@pytest.mark.asyncio
async def test_apply_in_place_rewrites_title_en_too():
    """A renamed character should propagate to chapter titles, not just
    the body. Mirrors the inline-edit popover's promise that 'Renamed in
    N chapters' covers everything a reader sees."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked into the hall.", None),
        (2, "The sect deliberated.", None),
    ])
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET title_en = ? WHERE novel_id = ? AND chapter_num = 1",
            ("Chapter 1: Bai Xiaochun Arrives", novel_id),
        )
        await conn.execute(
            "UPDATE chapters SET title_en = ? WHERE novel_id = ? AND chapter_num = 2",
            ("Chapter 2: A Quiet Day", novel_id),
        )
        await conn.commit()

    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Bai Xiao Chun",
            novel_id=novel_id,
        )
    # Chapter 1 changes in BOTH title_en and translated_text — counted
    # once for chapters_updated, but once each for the per-column tallies.
    assert result.chapters_updated == 1
    assert result.rows_updated_translated == 1
    assert result.rows_updated_titles == 1
    assert result.rows_updated_refined == 0

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT chapter_num, title_en, translated_text FROM chapters "
            "WHERE novel_id = ? ORDER BY chapter_num",
            (novel_id,),
        )
        rows = await cur.fetchall()
    assert rows[0]["title_en"] == "Chapter 1: Bai Xiao Chun Arrives"
    assert rows[0]["translated_text"] == "Bai Xiao Chun walked into the hall."
    # Chapter 2 had no occurrences in any column — untouched.
    assert rows[1]["title_en"] == "Chapter 2: A Quiet Day"
    assert rows[1]["translated_text"] == "The sect deliberated."


@pytest.mark.asyncio
async def test_apply_in_place_title_only_match_still_counts_chapter():
    """If the term lives only in the title (not the body), the chapter
    should still be counted as updated so the toast isn't misleading."""
    novel_id = await _seed_novel_with_chapters([
        (1, "The mountain was quiet.", None),
    ])
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET title_en = ? WHERE novel_id = ?",
            ("Chapter 1: Bai Xiaochun's Birthday", novel_id),
        )
        await conn.commit()

    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Bai Xiao Chun",
            novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    assert result.rows_updated_titles == 1
    assert result.rows_updated_translated == 0
    assert result.rows_updated_refined == 0


# ---- CAT Phase 3: segment reproject hook ---------------------------------


@pytest.mark.asyncio
async def test_commit_reprojects_segment_store_preserving_statuses():
    """The commit hook re-syncs the segment store in the same transaction:
    targets pick up the substitution, statuses survive (a confirmed row is
    NOT un-confirmed), and join(targets) still reproduces the body. Deep
    coverage lives in test_segments_reproject.py; this pins the wiring."""
    from backend.services import segments as segments_svc

    src = "甲" * 29 + "。\n\n" + "乙" * 29 + "。"
    body = "Bai Xiaochun bowed.\n\nThe elder nodded slowly."
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('S', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, ?, ?, 'done')",
            (novel_id, src, body),
        )
        chapter_id = cur.lastrowid
        await conn.commit()

    # Build the store, confirm row 0.
    async with open_conn() as conn:
        payload = await segments_svc.get_segments(conn, novel_id, 1)
        await conn.commit()
    seg0 = payload["segments"][0]
    async with open_conn() as conn:
        await segments_svc.update_segment(
            conn, novel_id, 1, 0, action="confirm", after_text=None,
            client_rev=payload["chapter_rev"],
            before_target_hash=seg0["target_hash"],
        )
        await conn.commit()

    query = fr.FindReplaceQuery(
        find="Bai Xiaochun", replacement="Lord Bai",
        scope_kind="novel", scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
        await fr.commit_preview(conn, preview.token)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT target_text, status FROM chapter_segments "
            "WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        )
        rows = list(await cur.fetchall())
        cur = await conn.execute(
            "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,)
        )
        ch = await cur.fetchone()
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["target_text"] == "Lord Bai bowed."
    assert ch["translated_text"] == "\n\n".join(r["target_text"] for r in rows)


# ---- Bug hunt 2026-08-04 (B1): archived novels are out of scope ----------


async def _archive(novel_id: int) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE novels SET deleted_at = datetime('now') WHERE id = ?",
            (novel_id,),
        )
        await conn.commit()


async def _restore(novel_id: int) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE novels SET deleted_at = NULL WHERE id = ?", (novel_id,)
        )
        await conn.commit()


async def _chapter_body(novel_id: int, chapter_num: int) -> str:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text FROM chapters "
            "WHERE novel_id = ? AND chapter_num = ?",
            (novel_id, chapter_num),
        )
        return (await cur.fetchone())["translated_text"]


@pytest.mark.asyncio
async def test_scope_all_preview_and_commit_skip_archived_novels():
    """soft_delete contract: an archived novel's chapters are preserved
    untouched, so scope='all' must neither count nor rewrite them."""
    active_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    archived_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun slept.", None),
    ])
    await _archive(archived_id)

    query = fr.FindReplaceQuery(
        find="Bai Xiaochun", replacement="Lord Bai",
        scope_kind="all", scope_ids=[],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
        assert preview.total_chapters == 1  # active only
        assert {r.novel_id for r in preview.rows} == {active_id}
        result = await fr.commit_preview(conn, preview.token)
    assert result.chapters_updated == 1
    assert await _chapter_body(active_id, 1) == "Lord Bai walked."
    assert await _chapter_body(archived_id, 1) == "Bai Xiaochun slept."


@pytest.mark.asyncio
async def test_explicit_novel_scope_on_archived_novel_matches_nothing():
    """The single-novel scope excludes archived too (consistent with the
    contract; the only UI path to it would be a stale tab)."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    await _archive(novel_id)
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun", replacement="Lord Bai",
        scope_kind="novel", scope_ids=[novel_id],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
    assert preview.total_chapters == 0


@pytest.mark.asyncio
async def test_archive_between_preview_and_commit_counts_as_drift():
    """A novel archived AFTER the preview drops out of the commit fetch and
    registers as drift: the commit refuses instead of rewriting it."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun", replacement="Lord Bai",
        scope_kind="all", scope_ids=[],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
    await _archive(novel_id)
    async with open_conn() as conn:
        with pytest.raises(fr.PreviewDriftError):
            await fr.commit_preview(conn, preview.token)
    assert await _chapter_body(novel_id, 1) == "Bai Xiaochun walked."


@pytest.mark.asyncio
async def test_global_glossary_apply_in_place_skips_archived_novels():
    """apply_in_place_for_glossary_term with novel_id=None (the global
    glossary route's shape) rewrites active novels only."""
    active_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    archived_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun slept.", None),
    ])
    await _archive(archived_id)
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Lord Bai", novel_id=None,
        )
    assert result.chapters_updated == 1
    assert await _chapter_body(active_id, 1) == "Lord Bai walked."
    assert await _chapter_body(archived_id, 1) == "Bai Xiaochun slept."


@pytest.mark.asyncio
async def test_restore_then_apply_reaches_the_novel_again():
    """Restoring an archived novel puts it back in scope."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    await _archive(novel_id)
    await _restore(novel_id)
    query = fr.FindReplaceQuery(
        find="Bai Xiaochun", replacement="Lord Bai",
        scope_kind="all", scope_ids=[],
    )
    async with open_conn() as conn:
        preview = await fr.build_preview(conn, query)
        assert preview.total_chapters == 1
        await fr.commit_preview(conn, preview.token)
    assert await _chapter_body(novel_id, 1) == "Lord Bai walked."


# ---- Block 1 (2026-08-07): alias-aware, snapshot-recording apply ----------


async def _chapter_id(novel_id: int, chapter_num: int) -> int:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
            (novel_id, chapter_num),
        )
        return (await cur.fetchone())["id"]


async def _chapter_title(novel_id: int, chapter_num: int) -> str | None:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT title_en FROM chapters WHERE novel_id = ? AND chapter_num = ?",
            (novel_id, chapter_num),
        )
        return (await cur.fetchone())["title_en"]


async def _set_title(novel_id: int, chapter_num: int, title: str) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET title_en = ? "
            "WHERE novel_id = ? AND chapter_num = ?",
            (title, novel_id, chapter_num),
        )
        await conn.commit()


async def _set_refinement(novel_id: int, chapter_num: int, status: str) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refinement_status = ? "
            "WHERE novel_id = ? AND chapter_num = ?",
            (status, novel_id, chapter_num),
        )
        await conn.commit()


async def _insert_translating_chapter(
    novel_id: int, chapter_num: int, translated: str
) -> int:
    """A chapter mid-flight in the LLM lane. The scope SELECT pins
    status='done', so this row is invisible to the rewrite; the point of the
    test is that it is now COUNTED rather than silently dropped."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, translated_text, status) "
            "VALUES (?, ?, ?, ?, 'translating')",
            (novel_id, chapter_num, "原文", translated),
        )
        await conn.commit()
        return cur.lastrowid


def test_split_aliases_trims_dedupes_and_preserves_order():
    """`term_en` carries slash aliases in this app; the editor splits the same
    way (zhAliases). The backend split must agree: trim, drop empties, dedupe,
    keep first-seen order (the first alias is the PRIMARY rendering)."""
    assert fr._split_aliases("A / B /  A / ") == ["A", "B"]
    assert fr._split_aliases("Bai Xiaochun/Xiaochun") == ["Bai Xiaochun", "Xiaochun"]
    assert fr._split_aliases("Solo") == ["Solo"]
    assert fr._split_aliases("") == []
    assert fr._split_aliases("   ") == []
    assert fr._split_aliases(" / / ") == []


@pytest.mark.asyncio
async def test_apply_in_place_alias_split_replaces_each_alias_with_new_primary():
    """Every OLD alias absent from the new set collapses onto the new PRIMARY
    (first) alias. Before this, only the literal full 'A / B' string was
    searched for, so a slash-aliased rename rewrote nothing at all."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked. Xiaochun smiled.", None),
    ])
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun / Xiaochun", new_en="Lord Bai",
            novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    assert await _chapter_body(novel_id, 1) == "Lord Bai walked. Lord Bai smiled."


@pytest.mark.asyncio
async def test_apply_in_place_alias_surviving_in_new_set_is_untouched():
    """An alias that SURVIVES into the new set is not a rename target, and it
    also has to be protected from a shorter target nested inside it: rewriting
    bare 'Xiaochun' must not corrupt the surviving 'Bai Xiaochun' into
    'Bai Bai Xiaochun'."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Xiaochun bowed. Bai Xiaochun smiled.", None),
    ])
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn,
            old_en="Bai Xiaochun / Xiaochun",
            new_en="Bai Xiaochun / Xiao Chun",
            novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    assert await _chapter_body(novel_id, 1) == (
        "Bai Xiaochun bowed. Bai Xiaochun smiled."
    )


@pytest.mark.asyncio
async def test_apply_in_place_longest_alias_wins_no_double_substitution():
    """Overlapping aliases resolve longest-first inside ONE alternation. A
    sequential per-alias loop (or a shortest-first alternation) would match
    the nested 'Xiaochun' first and yield 'Bai Bai Xiao Chun bowed.'"""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun bowed.", None),
    ])
    async with open_conn() as conn:
        await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun / Xiaochun", new_en="Bai Xiao Chun",
            novel_id=novel_id,
        )
    assert await _chapter_body(novel_id, 1) == "Bai Xiao Chun bowed."


@pytest.mark.asyncio
async def test_apply_in_place_all_aliases_survive_is_noop():
    """Reordering the aliases (every old alias still present in the new set)
    renames nothing: no writes, no snapshot row."""
    novel_id = await _seed_novel_with_chapters([
        (1, "The Golden Core and the Gold Core.", None),
    ])
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn,
            old_en="Golden Core / Gold Core",
            new_en="Gold Core / Golden Core",
            novel_id=novel_id,
        )
    assert result.chapters_updated == 0
    assert result.snapshot_ids == []
    assert await _chapter_body(novel_id, 1) == "The Golden Core and the Gold Core."


@pytest.mark.asyncio
async def test_apply_in_place_backslash_in_new_en_is_literal():
    """The replacement is a CALLABLE, not a regex template. A backslash in the
    new rendering used to blow up as `re.error: bad escape` (a 500 out of the
    route); it must land literally instead."""
    novel_id = await _seed_novel_with_chapters([
        (1, "The Path opened.", None),
    ])
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Path", new_en="Path\\Way", novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    assert await _chapter_body(novel_id, 1) == "The Path\\Way opened."


@pytest.mark.asyncio
async def test_apply_in_place_records_restorable_snapshot():
    """The apply now writes a find_replace_snapshots row, so the existing
    History tab can undo a glossary rename. The snapshot carries title_en too,
    so the restore reverts everything the apply touched."""
    from backend.services.fr_snapshots import restore_snapshot

    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    await _set_title(novel_id, 1, "Chapter 1: Bai Xiaochun Arrives")

    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Lord Bai", novel_id=novel_id,
        )
    assert len(result.snapshot_ids) == 1
    assert await _chapter_body(novel_id, 1) == "Lord Bai walked."
    assert await _chapter_title(novel_id, 1) == "Chapter 1: Lord Bai Arrives"

    async with open_conn() as conn:
        restored = await restore_snapshot(conn, result.snapshot_ids[0])
    assert restored["chapters_restored"] == 1
    assert await _chapter_body(novel_id, 1) == "Bai Xiaochun walked."
    assert await _chapter_title(novel_id, 1) == "Chapter 1: Bai Xiaochun Arrives"


@pytest.mark.asyncio
async def test_apply_in_place_skips_refining_chapters_and_reports_ids():
    """A chapter mid-refinement is skipped ENTIRELY (body and title): the
    refiner's commit would clobber the rewrite on machine rows anyway. The
    ids come back so the caller can name them."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
        (2, "Bai Xiaochun spoke.", None),
        (3, "Bai Xiaochun slept.", None),
    ])
    await _set_title(novel_id, 2, "Chapter 2: Bai Xiaochun Speaks")
    await _set_refinement(novel_id, 2, "pending")
    await _set_refinement(novel_id, 3, "in_progress")
    ch2 = await _chapter_id(novel_id, 2)
    ch3 = await _chapter_id(novel_id, 3)

    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Lord Bai", novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    assert result.skipped_refining == 2
    assert sorted(result.skipped_refining_chapter_ids) == sorted([ch2, ch3])
    # Chapter 1 rewritten; the two refining chapters untouched in BOTH columns.
    assert await _chapter_body(novel_id, 1) == "Lord Bai walked."
    assert await _chapter_body(novel_id, 2) == "Bai Xiaochun spoke."
    assert await _chapter_title(novel_id, 2) == "Chapter 2: Bai Xiaochun Speaks"
    assert await _chapter_body(novel_id, 3) == "Bai Xiaochun slept."


@pytest.mark.asyncio
async def test_apply_in_place_refining_chapter_without_the_term_is_not_counted():
    """The skip list is a re-apply worklist, so it holds only chapters that
    WOULD have changed. A refining chapter with no occurrence is not noise in
    that list."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
        (2, "The sect deliberated.", None),
    ])
    await _set_refinement(novel_id, 2, "in_progress")
    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Lord Bai", novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    assert result.skipped_refining == 0
    assert result.skipped_refining_chapter_ids == []


@pytest.mark.asyncio
async def test_apply_in_place_counts_translating_chapters_as_skipped():
    """Chapters mid-translate are already invisible to the rewrite (the scope
    SELECT pins status='done'). Counting them turns a silent gap into an
    honest 'N skipped, re-apply after they finish'."""
    novel_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    await _insert_translating_chapter(novel_id, 2, "Bai Xiaochun spoke.")

    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Lord Bai", novel_id=novel_id,
        )
    assert result.chapters_updated == 1
    assert result.skipped_translating == 1
    assert await _chapter_body(novel_id, 2) == "Bai Xiaochun spoke."


@pytest.mark.asyncio
async def test_apply_in_place_translating_count_excludes_archived_novels():
    """The skip count honors the same archived-novel exclusion as the rewrite
    itself, so an archived novel's in-flight chapter is not reported as work
    the user should come back to."""
    active_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
    ])
    archived_id = await _seed_novel_with_chapters([
        (1, "Bai Xiaochun slept.", None),
    ])
    await _insert_translating_chapter(archived_id, 2, "Bai Xiaochun spoke.")
    await _archive(archived_id)

    async with open_conn() as conn:
        result = await fr.apply_in_place_for_glossary_term(
            conn, old_en="Bai Xiaochun", new_en="Lord Bai", novel_id=None,
        )
    assert result.chapters_updated == 1
    assert result.skipped_translating == 0
    assert await _chapter_body(active_id, 1) == "Lord Bai walked."
