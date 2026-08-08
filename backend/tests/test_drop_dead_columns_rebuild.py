"""Regression tests for the one-shot humanizer-era rebuild (_drop_dead_columns).

The rebuild used to recreate chapters / novels / style_edits from a hand-copied
CREATE TABLE frozen at the 2026-05-26 column set. init_db runs
`_ADDITIVE_MIGRATIONS` BEFORE the rebuild, so any DB that took the rebuild path
got its tables back WITHOUT every column added after that date:

  chapters: free_draft_text / free_draft_status / free_draft_error /
            free_draft_completed_at, translated_by_provider_id,
            refined_by_provider_id, prompt_config_snapshot, fixup_audit,
            segments_state, segmentation_version, segments_rev
  novels:   author, original_title, synopsis, status, cover_image_path,
            cover_source, series_name, series_index, deleted_at,
            disabled_observers

That boot then 500s on nearly every query ("no such column"), and on the
DROP-COLUMN-unsupported fall-through a modern populated DB loses the DATA in
those columns permanently.

The rebuild now renders the SAME CREATE TABLE templates SCHEMA uses and copies
the old/new column INTERSECTION, so it is column-complete by construction: a
column added to SCHEMA later lands in the rebuild automatically.

Covered here:
  1. Full rebuild (humanized_text sentinel with data) keeps every current
     column AND every populated value, on all three rebuilt tables.
  2. Idempotency: another init_db does not rebuild again and changes nothing.
  3. The cheap DROP-COLUMN fast path (sentinel present, all NULL) still skips
     the rebuild.
"""

from __future__ import annotations

import logging

import pytest

from backend.db import init_db, open_conn

pytestmark = pytest.mark.asyncio

# Log line emitted only by the FULL rebuild path.
_REBUILD_LOG = "rebuilding chapters / novels / style_edits"

# Columns the rebuild is SUPPOSED to drop: dead humanizer / review / queue
# columns that _ADDITIVE_MIGRATIONS still re-adds on every boot (the list is
# append-only) but that SCHEMA no longer declares. Anything outside these sets
# disappearing from a rebuilt table is the bug this module pins.
_EXPECTED_DROPPED = {
    "chapters": {
        "humanized_text",
        "humanizer_report",
        "humanizer_status",
        "humanizer_error",
        "humanize_queued",
        "review_status",
        "review_error",
        "pre_review_text",
        "queue_position",
    },
    "novels": {
        "humanizer_tone",
        "humanizer_honorific",
        "humanizer_intensity",
    },
    "style_edits": {"variant"},
}

# Columns added AFTER the frozen 2026-05-26 DDL. Listed explicitly (rather than
# only diffed against a reference DB) so the test names the regression.
_POST_FREEZE_CHAPTER_COLUMNS = (
    "free_draft_text",
    "free_draft_status",
    "free_draft_error",
    "free_draft_completed_at",
    "translated_by_provider_id",
    "refined_by_provider_id",
    "prompt_config_snapshot",
    "fixup_audit",
    "segments_state",
    "segmentation_version",
    "segments_rev",
)
_POST_FREEZE_NOVEL_COLUMNS = (
    "author",
    "original_title",
    "synopsis",
    "status",
    "cover_image_path",
    "cover_source",
    "series_name",
    "series_index",
    "deleted_at",
    "disabled_observers",
)


def _use_db(monkeypatch, db_path) -> None:
    """Point backend.db at `db_path` (it binds DB_PATH at import time)."""
    from backend import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", db_path, raising=True)


async def _columns(table: str) -> set[str]:
    async with open_conn() as conn:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        return {r[1] for r in await cur.fetchall()}


async def _seed_rich_rows() -> None:
    """Insert a novel + two chapters + a style edit that populate the columns
    the frozen rebuild used to throw away, then arm the legacy sentinel.

    Chapter 300 carries the values and leaves humanized_text NULL; chapter 301
    carries the sentinel, so the value assertions stay independent of the
    humanized_text to translated_text promotion that runs just before the
    rebuild."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO providers (name, provider_type, model_id) "
            "VALUES ('rebuild-probe', 'gemini', 'model-x')"
        )
        provider_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, source_url, created_at, "
            "style_note, source_language, genre, custom_style_brief, "
            "translator_provider_id, author, original_title, synopsis, status, "
            "cover_image_path, cover_source, series_name, series_index, "
            "deleted_at, disabled_observers, import_status, "
            "last_read_chapter_num, last_read_at) "
            "VALUES (?, 'paste', NULL, '2026-01-01', ?, 'zh', 'xianxia', ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, ?)",
            (
                "rebuild-survivor", "voice anchor", "custom prose", provider_id,
                "作者", "原名", "a synopsis", "ongoing",
                "covers/7.jpg", "epub", "The Series", 3,
                "2026-08-01 09:00:00", '["mt_texture"]',
                300, "2026-08-02 10:00:00",
            ),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, title_en, "
            "original_text, translated_text, status, translate_queued, "
            "queue_priority, refinement_status, refined_text, input_tokens, "
            "output_tokens, cached_input_tokens, cost_usd, translated_at, "
            "import_source_url, import_fetched_at, free_draft_text, "
            "free_draft_status, free_draft_error, free_draft_completed_at, "
            "translated_by_provider_id, prompt_config_snapshot, fixup_audit, "
            "segments_state, segmentation_version, segments_rev) "
            "VALUES (?, 300, '第三百章', 'Chapter 300: Survivor', ?, ?, 'done', "
            "0, 7, 'done', ?, 12345, 6789, 1024, 0.42, '2026-08-01 08:00:00', "
            "'https://example.test/300', '2026-07-31 08:00:00', ?, 'done', "
            "NULL, '2026-07-31 09:00:00', ?, ?, ?, 'partial', 3, 'abc123def456')",
            (
                novel_id, "原文正文", "draft body", "refined body",
                "mechanical draft", provider_id,
                '{"template_version": "v9"}', '{"rules": {"enforce_em_dash": 2}}',
            ),
        )
        chapter_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 301, '第二章原文', 'draft 301', 'done')",
            (novel_id,),
        )
        await conn.execute(
            "INSERT INTO style_edits (novel_id, chapter_id, before_text, "
            "after_text, created_at) VALUES (?, ?, 'stiff line', 'better line', "
            "'2026-08-03 11:00:00')",
            (novel_id, chapter_id),
        )
        # Arm the legacy sentinel: the first init_db dropped humanized_text via
        # the cheap fast path, so re-add it and give ONE row a value. That is
        # exactly the shape of a pre-restructure user DB and forces the full
        # rebuild on the next init_db.
        await conn.execute("ALTER TABLE chapters ADD COLUMN humanized_text TEXT")
        await conn.execute(
            "UPDATE chapters SET humanized_text = 'legacy humanized body' "
            "WHERE chapter_num = 301"
        )
        await conn.commit()


async def _read_survivor() -> dict:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT * FROM novels WHERE title = 'rebuild-survivor'"
        )
        novel = await cur.fetchone()
        cur = await conn.execute(
            "SELECT * FROM chapters WHERE novel_id = ? AND chapter_num = 300",
            (novel["id"],),
        )
        chapter = await cur.fetchone()
        cur = await conn.execute(
            "SELECT * FROM chapters WHERE novel_id = ? AND chapter_num = 301",
            (novel["id"],),
        )
        legacy_chapter = await cur.fetchone()
        cur = await conn.execute(
            "SELECT * FROM style_edits WHERE novel_id = ?", (novel["id"],)
        )
        style_edit = await cur.fetchone()
    return {
        "novel": novel,
        "chapter": chapter,
        "legacy_chapter": legacy_chapter,
        "style_edit": style_edit,
    }


def _assert_values_survived(rows: dict) -> None:
    novel, chapter = rows["novel"], rows["chapter"]
    assert novel is not None, "novel row was dropped during rebuild"
    assert chapter is not None, "chapter row was dropped during rebuild"

    assert novel["author"] == "作者"
    assert novel["original_title"] == "原名"
    assert novel["synopsis"] == "a synopsis"
    assert novel["status"] == "ongoing"
    assert novel["cover_image_path"] == "covers/7.jpg"
    assert novel["cover_source"] == "epub"
    assert novel["series_name"] == "The Series"
    assert novel["series_index"] == 3
    assert novel["deleted_at"] == "2026-08-01 09:00:00"
    assert novel["disabled_observers"] == '["mt_texture"]'
    assert novel["import_status"] == "done"
    assert novel["style_note"] == "voice anchor"
    assert novel["genre"] == "xianxia"
    assert novel["custom_style_brief"] == "custom prose"
    assert novel["last_read_chapter_num"] == 300
    assert novel["last_read_at"] == "2026-08-02 10:00:00"
    assert novel["created_at"] == "2026-01-01"

    assert chapter["title_en"] == "Chapter 300: Survivor"
    assert chapter["translated_text"] == "draft body"
    assert chapter["refined_text"] == "refined body"
    assert chapter["refinement_status"] == "done"
    assert chapter["queue_priority"] == 7
    assert chapter["input_tokens"] == 12345
    assert chapter["output_tokens"] == 6789
    assert chapter["cached_input_tokens"] == 1024
    assert chapter["cost_usd"] == pytest.approx(0.42, abs=1e-6)
    assert chapter["translated_at"] == "2026-08-01 08:00:00"
    assert chapter["import_source_url"] == "https://example.test/300"
    assert chapter["free_draft_text"] == "mechanical draft"
    assert chapter["free_draft_status"] == "done"
    assert chapter["free_draft_completed_at"] == "2026-07-31 09:00:00"
    assert chapter["translated_by_provider_id"] is not None
    assert chapter["prompt_config_snapshot"] == '{"template_version": "v9"}'
    assert chapter["fixup_audit"] == '{"rules": {"enforce_em_dash": 2}}'
    assert chapter["segments_state"] == "partial"
    assert chapter["segmentation_version"] == 3
    assert chapter["segments_rev"] == "abc123def456"

    # The humanized_text row is promoted into translated_text before the
    # rebuild drops the column (that promotion is the whole point of the
    # one-shot migration) and must not be lost by the table swap.
    assert rows["legacy_chapter"]["translated_text"] == "legacy humanized body"

    edit = rows["style_edit"]
    assert edit is not None, "style_edits row was dropped during rebuild"
    assert edit["before_text"] == "stiff line"
    assert edit["after_text"] == "better line"
    assert edit["created_at"] == "2026-08-03 11:00:00"


async def _reference_columns(monkeypatch_target, tmp_path) -> dict[str, set[str]]:
    """Column sets of a pristine init_db DB, for parity comparison."""
    ref_path = tmp_path / "reference.db"
    _use_db(monkeypatch_target, ref_path)
    await init_db()
    return {
        table: await _columns(table)
        for table in ("chapters", "novels", "style_edits")
    }


async def test_full_rebuild_keeps_every_current_column_and_value(
    tmp_path, monkeypatch, caplog,
):
    """A humanizer-era DB with modern data: the rebuild must drop ONLY the dead
    columns and must not lose a single populated value."""
    db_path = tmp_path / "humanizer-era.db"
    _use_db(monkeypatch, db_path)
    await init_db()
    await _seed_rich_rows()

    with caplog.at_level(logging.INFO, logger="backend.db"):
        await init_db()
    assert any(_REBUILD_LOG in r.message for r in caplog.records), (
        "the humanized_text sentinel should have forced the full rebuild"
    )

    rebuilt = {
        table: await _columns(table)
        for table in ("chapters", "novels", "style_edits")
    }
    _assert_values_survived(await _read_survivor())

    # Named regression pin: the columns the frozen DDL used to delete.
    for col in _POST_FREEZE_CHAPTER_COLUMNS:
        assert col in rebuilt["chapters"], f"chapters.{col} lost in the rebuild"
    for col in _POST_FREEZE_NOVEL_COLUMNS:
        assert col in rebuilt["novels"], f"novels.{col} lost in the rebuild"

    # Parity against a pristine DB: the only difference may be the dead
    # columns the rebuild exists to remove. humanized_text is excluded because
    # a pristine DB does not carry it either (its cheap DROP COLUMN fast path
    # removed it), so it can never show up in this diff.
    reference = await _reference_columns(monkeypatch, tmp_path)
    for table, ref_cols in reference.items():
        assert rebuilt[table] - ref_cols == set(), (
            f"{table} gained columns a fresh DB does not have"
        )
        assert ref_cols - rebuilt[table] == _EXPECTED_DROPPED[table] - {
            "humanized_text"
        }, f"{table} lost live columns in the rebuild"


async def test_rebuild_is_idempotent(tmp_path, monkeypatch, caplog):
    """A third init_db must not rebuild again, and must change nothing."""
    db_path = tmp_path / "idempotent.db"
    _use_db(monkeypatch, db_path)
    await init_db()
    await _seed_rich_rows()
    await init_db()

    before = {k: dict(v) for k, v in (await _read_survivor()).items()}
    columns_before = {
        table: await _columns(table)
        for table in ("chapters", "novels", "style_edits")
    }

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="backend.db"):
        await init_db()
    assert not any(_REBUILD_LOG in r.message for r in caplog.records), (
        "the rebuild ran a second time; it must be one-shot"
    )

    # Compare on the columns both snapshots share: the append-only migration
    # list re-adds the dead legacy columns on every boot, so a post-rebuild
    # row legitimately grows extra (all-NULL) keys.
    after = {k: dict(v) for k, v in (await _read_survivor()).items()}
    for key, before_row in before.items():
        after_row = after[key]
        shared = set(before_row) & set(after_row)
        assert {c: after_row[c] for c in shared} == {
            c: before_row[c] for c in shared
        }, f"{key} changed on a follow-up init_db"
    _assert_values_survived(await _read_survivor())

    columns_after = {
        table: await _columns(table)
        for table in ("chapters", "novels", "style_edits")
    }
    # No live column may disappear. Each table may only GROW back the dead
    # legacy columns, which the append-only migration list re-adds on every
    # boot (humanized_text is then dropped again by the cheap fast path).
    for table, dead in _EXPECTED_DROPPED.items():
        assert columns_before[table] - columns_after[table] == set(), (
            f"{table} lost a column on a follow-up init_db"
        )
        assert columns_after[table] - columns_before[table] <= dead, (
            f"{table} gained an unexpected column on a follow-up init_db"
        )
    assert "humanized_text" not in columns_after["chapters"]


async def test_fast_path_drops_column_without_rebuilding(
    tmp_path, monkeypatch, caplog,
):
    """Sentinel present but all-NULL (every fresh DB, every boot): drop the
    column in place, never rebuild."""
    db_path = tmp_path / "fast-path.db"
    _use_db(monkeypatch, db_path)
    await init_db()

    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('fast-path', 'paste')"
        )
        novel_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, 'src', 'body', 'done')",
            (novel_id,),
        )
        # Re-add the sentinel with no data, exactly as _ADDITIVE_MIGRATIONS
        # does on every boot.
        await conn.execute("ALTER TABLE chapters ADD COLUMN humanized_text TEXT")
        await conn.commit()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="backend.db"):
        await init_db()
    assert not any(_REBUILD_LOG in r.message for r in caplog.records), (
        "an all-NULL sentinel must take the cheap DROP COLUMN path"
    )

    cols = await _columns("chapters")
    assert "humanized_text" not in cols
    for col in _POST_FREEZE_CHAPTER_COLUMNS:
        assert col in cols

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text FROM chapters WHERE chapter_num = 1"
        )
        row = await cur.fetchone()
    assert row["translated_text"] == "body"
