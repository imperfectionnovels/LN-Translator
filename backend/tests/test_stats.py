"""Initiative 6 — stats dashboard tests.

Per-chapter cost tracking was removed (chapters.cost_usd is vestigial), so
these tests cover word counts, coverage, throughput, and global rollups.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.services import stats as stats_svc


@pytest.fixture(autouse=True)
def _reset_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    yield


async def _seed_novel(rows):
    """rows: list of dicts with keys chapter_num, source, target, status,
    translated_at (optional). Returns novel_id."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, source_url) VALUES (?, ?, NULL)",
            ("TestNovel", "paste"),
        )
        novel_id = cur.lastrowid
        for r in rows:
            await conn.execute(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, original_text, translated_text, "
                "status, translated_at, refinement_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    novel_id, r["chapter_num"], r["source"], r["target"],
                    r.get("status", "done"),
                    r.get("translated_at"),
                    r.get("refinement_status", "none"),
                ),
            )
        await conn.commit()
    return novel_id


# ---- Word counting ------------------------------------------------------


@pytest.mark.asyncio
async def test_word_count_only_counts_done_chapters():
    """Source / target word counts must skip pending or error chapters —
    those rows have no useful translation yet, including them inflates
    the source-words count for chapters the dashboard can't measure
    coverage for."""
    novel_id = await _seed_novel([
        {"chapter_num": 1, "source": "源段。", "target": "Target one two three.",
         "status": "done"},
        {"chapter_num": 2, "source": "未翻译。", "target": None,
         "status": "pending"},
    ])
    async with open_conn() as conn:
        result = await stats_svc.novel_stats(conn, novel_id)
    # Only ch 1's source counts: "源段。" = 3 non-whitespace chars
    assert result["words"]["source_chars"] == 3
    # "Target one two three." → 4 words
    assert result["words"]["english_words"] == 4


# ---- Coverage -----------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_uses_chapter_aggregate():
    novel_id = await _seed_novel([
        {"chapter_num": 1, "source": "源。", "target": "T.", "status": "done"},
        {"chapter_num": 2, "source": "源。", "target": "T.", "status": "done"},
        {"chapter_num": 3, "source": "源。", "target": None, "status": "pending"},
    ])
    async with open_conn() as conn:
        result = await stats_svc.novel_stats(conn, novel_id)
    assert result["coverage"]["total_chapters"] == 3
    assert result["coverage"]["done_chapters"] == 2


# ---- Throughput ---------------------------------------------------------


@pytest.mark.asyncio
async def test_throughput_excludes_null_translated_at():
    """Chapters predating the migration have NULL translated_at and must
    NOT appear in the throughput series (they'd land on day=None and
    crash the sparkline)."""
    novel_id = await _seed_novel([
        {"chapter_num": 1, "source": "源。", "target": "T.",
         "translated_at": None},
    ])
    async with open_conn() as conn:
        result = await stats_svc.novel_stats(conn, novel_id)
    assert result["throughput"] == []


@pytest.mark.asyncio
async def test_throughput_groups_by_day():
    novel_id = await _seed_novel([
        {"chapter_num": 1, "source": "源。", "target": "T.",
         "translated_at": "2026-05-20 10:00:00"},
        {"chapter_num": 2, "source": "源。", "target": "T.",
         "translated_at": "2026-05-20 14:00:00"},
        {"chapter_num": 3, "source": "源。", "target": "T.",
         "translated_at": "2026-05-21 09:00:00"},
    ])
    async with open_conn() as conn:
        result = await stats_svc.novel_stats(conn, novel_id)
    by_day = {r["day"]: r["count"] for r in result["throughput"]}
    assert by_day.get("2026-05-20") == 2
    assert by_day.get("2026-05-21") == 1


# ---- Global -------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_stats_sums_across_novels():
    """Global novel_count + chapter aggregates equal per-novel sums."""
    nid_a = await _seed_novel([
        {"chapter_num": 1, "source": "源。", "target": "T."},
    ])
    # _seed_novel creates a new novel each call; second call is novel id 2.
    nid_b = await _seed_novel([
        {"chapter_num": 1, "source": "源。", "target": "T."},
        {"chapter_num": 2, "source": "源。", "target": "T."},
    ])
    async with open_conn() as conn:
        g = await stats_svc.global_stats(conn)
        per_a = await stats_svc.novel_stats(conn, nid_a)
        per_b = await stats_svc.novel_stats(conn, nid_b)
    assert g["novel_count"] == 2
    assert g["coverage"]["total_chapters"] == (
        per_a["coverage"]["total_chapters"]
        + per_b["coverage"]["total_chapters"]
    )
