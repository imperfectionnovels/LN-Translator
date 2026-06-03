"""Stats aggregation (Initiative 6).

Pure SQL aggregation backing the dashboard. Two entry points:
  * `novel_stats(novel_id)` — full per-novel rollup.
  * `global_stats()` — library-wide aggregate.

Both return plain dicts so the routes layer can pass them through without
a Pydantic round-trip. The router still wraps the responses in models for
schema discoverability.

Per-chapter cost tracking was removed (chapters.cost_usd went permanently
NULL when per-model pricing input was dropped in the 2026-05-26 catalog
redesign), so these rollups carry token counts and throughput only.
"""

from __future__ import annotations

import logging
import re
from typing import TypedDict

import aiosqlite

logger = logging.getLogger(__name__)


# Words counter. Chinese source is character-based; English target is
# whitespace-split. Same primitive the reader uses for its end-of-chapter
# "N words" badge.
_WORD_RE = re.compile(r"\S+")


def _english_word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _chinese_char_count(text: str) -> int:
    # Strip whitespace and count remaining chars. Punctuation counts as a
    # "char" too — matches the convention CJK reader stats use.
    return sum(1 for ch in (text or "") if not ch.isspace())


# ---------------------------------------------------------------------------
# Return shapes
# ---------------------------------------------------------------------------
# The dashboard route and frontend depend on the exact nested key shape of
# these rollups. Capturing them as TypedDicts (the same precedent as
# NovelGenreBrief / RestoreResult elsewhere in services) makes a dropped or
# renamed key a type-check failure instead of a runtime surprise in the UI.
# They remain plain dicts at runtime, so the route still passes them through
# without a Pydantic round-trip.


class ThroughputPoint(TypedDict):
    day: str
    count: int


class WordStats(TypedDict):
    source_chars: int
    english_words: int
    refined_words: int
    # target / source ratio; None when the source is empty.
    english_words_per_source_char: float | None


class NovelCoverage(TypedDict):
    total_chapters: int
    done_chapters: int
    refined_chapters: int
    style_edit_chapters: int
    observation_chapters: int


class TokenStats(TypedDict):
    input_tokens_total: int
    output_tokens_total: int
    cached_input_tokens_total: int


class ObservationKindCount(TypedDict):
    kind: str
    count: int


class ObservationStats(TypedDict):
    by_kind: list[ObservationKindCount]
    total_undismissed: int


class NovelGlossaryStats(TypedDict):
    locked: int
    auto: int
    global_total: int


class NovelTmStats(TypedDict):
    total_segments: int
    distinct_source_hashes: int
    duplication_ratio: float | None
    avg_source_chars: float
    avg_target_chars: float


class NovelStats(TypedDict):
    novel_id: int
    novel_title: str
    words: WordStats
    throughput: list[ThroughputPoint]
    coverage: NovelCoverage
    tokens: TokenStats
    observations: ObservationStats
    glossary: NovelGlossaryStats
    tm: NovelTmStats


class GlobalCoverage(TypedDict):
    total_chapters: int
    done_chapters: int
    refined_chapters: int


class ProviderMixEntry(TypedDict):
    provider_name: str
    chapter_count: int


class GlobalGlossaryStats(TypedDict):
    global_total: int


class GlobalTmStats(TypedDict):
    total_segments: int


class GlobalStats(TypedDict):
    novel_count: int
    coverage: GlobalCoverage
    throughput: list[ThroughputPoint]
    provider_mix: list[ProviderMixEntry]
    glossary: GlobalGlossaryStats
    tm: GlobalTmStats


# ---------------------------------------------------------------------------
# Per-novel
# ---------------------------------------------------------------------------


async def novel_stats(
    conn: aiosqlite.Connection, novel_id: int
) -> NovelStats | None:
    """Full per-novel rollup. Returns None when the novel doesn't exist
    (the route turns that into a 404)."""
    cur = await conn.execute(
        "SELECT id, title FROM novels WHERE id = ?", (novel_id,)
    )
    novel_row = await cur.fetchone()
    if novel_row is None:
        return None

    # Chapter-level aggregates that don't require streaming text content.
    cur = await conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN refinement_status = 'done' THEN 1 ELSE 0 END) AS refined,
            COALESCE(SUM(input_tokens), 0) AS input_tokens_total,
            COALESCE(SUM(output_tokens), 0) AS output_tokens_total,
            COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens_total
        FROM chapters WHERE novel_id = ?
        """,
        (novel_id,),
    )
    chapter_agg = await cur.fetchone()

    # Word counts — stream chapter texts, count in Python. Cap per chapter
    # is essentially the chapter size; tens of thousands of words per
    # novel is fine for a single dashboard request.
    cur = await conn.execute(
        "SELECT original_text, translated_text, refined_text "
        "FROM chapters WHERE novel_id = ? AND status = 'done'",
        (novel_id,),
    )
    source_chars = 0
    english_words = 0
    refined_words = 0
    for r in await cur.fetchall():
        source_chars += _chinese_char_count(r["original_text"])
        english_words += _english_word_count(r["translated_text"])
        refined_words += _english_word_count(r["refined_text"])

    # Throughput: chapters translated per day for the last 30 days. NULL
    # translated_at rows are excluded (chapters that pre-date the
    # initiative-6 migration) — the dashboard explains this in tooltip.
    cur = await conn.execute(
        """
        SELECT date(translated_at) AS day, COUNT(*) AS n
        FROM chapters
        WHERE novel_id = ?
          AND translated_at IS NOT NULL
          AND translated_at >= date('now', '-30 days')
        GROUP BY day
        ORDER BY day
        """,
        (novel_id,),
    )
    throughput = [
        {"day": r["day"], "count": r["n"]}
        for r in await cur.fetchall()
    ]

    # Coverage: distinct chapters with style edits / observations.
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT chapter_id) AS n FROM style_edits "
        "WHERE novel_id = ? AND chapter_id IS NOT NULL",
        (novel_id,),
    )
    style_edit_chapters = (await cur.fetchone())["n"] or 0

    cur = await conn.execute(
        """
        SELECT COUNT(DISTINCT c.id) AS n
        FROM chapters c
        JOIN chapter_observations o ON o.chapter_id = c.id
        WHERE c.novel_id = ?
        """,
        (novel_id,),
    )
    observation_chapters = (await cur.fetchone())["n"] or 0

    # Observation kind breakdown — undismissed only, so the dashboard
    # tracks unresolved drift, not the historical log.
    cur = await conn.execute(
        """
        SELECT o.kind, COUNT(*) AS n
        FROM chapter_observations o
        JOIN chapters c ON c.id = o.chapter_id
        WHERE c.novel_id = ? AND o.dismissed_at IS NULL
        GROUP BY o.kind
        ORDER BY n DESC
        """,
        (novel_id,),
    )
    observation_kinds = [
        {"kind": r["kind"], "count": r["n"]}
        for r in await cur.fetchall()
    ]

    # Glossary totals — locked / auto split for the per-novel section,
    # plus the global total so the user knows how many cross-novel terms
    # are in flight for this novel's translations.
    cur = await conn.execute(
        """
        SELECT
            SUM(CASE WHEN locked = 1 THEN 1 ELSE 0 END) AS locked,
            SUM(CASE WHEN locked = 0 THEN 1 ELSE 0 END) AS auto
        FROM glossary_entries WHERE novel_id = ?
        """,
        (novel_id,),
    )
    g_row = await cur.fetchone()
    cur = await conn.execute("SELECT COUNT(*) AS n FROM global_glossary_entries")
    global_glossary_total = (await cur.fetchone())["n"] or 0

    # TM stats — segment count, distinct source hashes, duplication.
    cur = await conn.execute(
        """
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT source_hash) AS distinct_hashes,
               AVG(LENGTH(source_text)) AS avg_source_chars,
               AVG(LENGTH(target_text)) AS avg_target_chars
        FROM tm_segments WHERE novel_id = ?
        """,
        (novel_id,),
    )
    tm_row = await cur.fetchone()

    return {
        "novel_id": novel_row["id"],
        "novel_title": novel_row["title"],
        "words": {
            "source_chars": source_chars,
            "english_words": english_words,
            "refined_words": refined_words,
            # Ratio is target / source — character-per-word measure of how
            # condensed the English is vs the source. Null when source
            # is empty.
            "english_words_per_source_char": (
                english_words / source_chars if source_chars > 0 else None
            ),
        },
        "throughput": throughput,
        "coverage": {
            "total_chapters": chapter_agg["total"] or 0,
            "done_chapters": chapter_agg["done"] or 0,
            "refined_chapters": chapter_agg["refined"] or 0,
            "style_edit_chapters": style_edit_chapters,
            "observation_chapters": observation_chapters,
        },
        "tokens": {
            "input_tokens_total": chapter_agg["input_tokens_total"] or 0,
            "output_tokens_total": chapter_agg["output_tokens_total"] or 0,
            "cached_input_tokens_total": chapter_agg["cached_input_tokens_total"] or 0,
        },
        "observations": {
            "by_kind": observation_kinds,
            "total_undismissed": sum(o["count"] for o in observation_kinds),
        },
        "glossary": {
            "locked": g_row["locked"] or 0,
            "auto": g_row["auto"] or 0,
            "global_total": global_glossary_total,
        },
        "tm": {
            "total_segments": tm_row["total"] or 0,
            "distinct_source_hashes": tm_row["distinct_hashes"] or 0,
            "duplication_ratio": (
                (tm_row["total"] / tm_row["distinct_hashes"])
                if tm_row["distinct_hashes"] else None
            ),
            "avg_source_chars": float(tm_row["avg_source_chars"] or 0.0),
            "avg_target_chars": float(tm_row["avg_target_chars"] or 0.0),
        },
    }


# ---------------------------------------------------------------------------
# Global
# ---------------------------------------------------------------------------


async def global_stats(conn: aiosqlite.Connection) -> GlobalStats:
    """Library-wide aggregate."""
    cur = await conn.execute("SELECT COUNT(*) AS n FROM novels")
    novel_count = (await cur.fetchone())["n"]

    cur = await conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN refinement_status = 'done' THEN 1 ELSE 0 END) AS refined
        FROM chapters
        """,
    )
    chapter_agg = await cur.fetchone()

    # Throughput across all novels.
    cur = await conn.execute(
        """
        SELECT date(translated_at) AS day, COUNT(*) AS n
        FROM chapters
        WHERE translated_at IS NOT NULL
          AND translated_at >= date('now', '-30 days')
        GROUP BY day ORDER BY day
        """,
    )
    throughput = [
        {"day": r["day"], "count": r["n"]} for r in await cur.fetchall()
    ]

    # Provider mix: group successful chapters by their resolved provider
    # (translator_provider_id on the novel). NULL provider rows fall under
    # "default" so the UI doesn't show an empty label.
    cur = await conn.execute(
        """
        SELECT
            COALESCE(p.name, '(default)') AS provider_name,
            COUNT(c.id) AS chapter_count
        FROM chapters c
        JOIN novels n ON n.id = c.novel_id
        LEFT JOIN providers p ON p.id = n.translator_provider_id
        WHERE c.status = 'done'
        GROUP BY provider_name
        ORDER BY chapter_count DESC
        """,
    )
    provider_mix = [
        {
            "provider_name": r["provider_name"],
            "chapter_count": r["chapter_count"],
        }
        for r in await cur.fetchall()
    ]

    cur = await conn.execute("SELECT COUNT(*) AS n FROM global_glossary_entries")
    global_glossary = (await cur.fetchone())["n"] or 0

    cur = await conn.execute("SELECT COUNT(*) AS n FROM tm_segments")
    tm_total = (await cur.fetchone())["n"] or 0

    return {
        "novel_count": novel_count,
        "coverage": {
            "total_chapters": chapter_agg["total"] or 0,
            "done_chapters": chapter_agg["done"] or 0,
            "refined_chapters": chapter_agg["refined"] or 0,
        },
        "throughput": throughput,
        "provider_mix": provider_mix,
        "glossary": {
            "global_total": global_glossary,
        },
        "tm": {
            "total_segments": tm_total,
        },
    }
