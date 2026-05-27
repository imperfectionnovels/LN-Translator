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
