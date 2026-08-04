"""Prompt-input fetchers: per-novel data the translator prompt is built from.

These pull the dynamic blocks the runtime user prompt stacks on top of the
static base.md + genre overlay: the captured style edits, the per-novel style
note, the previous-chapter tail, the genre / custom-brief / source-language
metadata, and the resolved translator Provider. They are read-only over the DB
and carry no queue state, so they live here as a shared surface rather than as
queue-worker internals: the queue worker and the ab_style_edits A/B script both
consume them.

Each fetcher honors its A/B env flag (PROMPT_INCLUDE_STYLE_EDITS /
PROMPT_INCLUDE_STYLE_NOTE / PROMPT_INCLUDE_CONFIRMED_EXEMPLARS /
PREVIOUS_CONTEXT_ENABLED) at the fetch site, so a single flag flip
suppresses one block for an A/B arm without DB mutation.
"""

from __future__ import annotations

import logging
from typing import TypedDict

import aiosqlite

from backend.config import (
    PREVIOUS_CONTEXT_ENABLED,
    PREVIOUS_CONTEXT_MAX_GAP,
    PREVIOUS_CONTEXT_PARAGRAPHS,
    PROMPT_INCLUDE_CONFIRMED_EXEMPLARS,
    PROMPT_INCLUDE_STYLE_EDITS,
    PROMPT_INCLUDE_STYLE_NOTE,
)
from backend.services import segments as segments_svc
from backend.services.providers import (
    Provider,
    get_default_provider,
    load_provider,
)

logger = logging.getLogger(__name__)

STYLE_EDIT_LIMIT = 10
CONFIRMED_EXEMPLAR_LIMIT = 5


async def fetch_confirmed_exemplars(
    conn: aiosqlite.Connection, novel_id: int, exclude_chapter_id: int
) -> list[tuple[str, str]]:
    """Recent CONFIRMED segment pairs from OTHER chapters of this novel, for
    the APPROVED TRANSLATION EXAMPLES prompt block (CAT Phase 5): zh->EN
    renderings the user explicitly confirmed in the CAT editor, shown to the
    model as voice and phrasing precedent. Capped at
    CONFIRMED_EXEMPLAR_LIMIT, recency-selected, deduped by source.

    Returns [] when the flag is off, when nothing is confirmed yet, or when
    the chapter_segments table doesn't exist (older DB). The SQL lives in
    services/segments.py (the single chapter_segments owner); this wrapper
    owns the flag gate + limit, matching its sibling fetchers."""
    if not PROMPT_INCLUDE_CONFIRMED_EXEMPLARS:
        return []
    try:
        return await segments_svc.fetch_confirmed_exemplar_pairs(
            conn, novel_id, exclude_chapter_id, CONFIRMED_EXEMPLAR_LIMIT
        )
    except aiosqlite.OperationalError:
        return []


# Dedupe-key truncation for merged style pairs, matching the ~400-char bound
# format_style_edits renders with (and the segment pairs are stored at), so
# two pairs that would render identically in the prompt count as one.
_STYLE_PAIR_KEY_CHARS = 400


async def fetch_style_edits(
    conn: aiosqlite.Connection,
    novel_id: int,
    exclude_chapter_id: int | None = None,
) -> list[tuple[str, str]]:
    """Pull the most-recent user paragraph edits for this novel, capped at
    STYLE_EDIT_LIMIT. Used as "preferred rewrites" examples in the translator
    prompt: the LLM learns the user's phrasing over time without manual
    prompt engineering.

    Two sources merge under the one flag and cap, deduped on the before
    text (newest wins): CAT-editor segment pairs first (the ledger's
    edited|confirmed machine->target rows via segments.recent_edited_pairs,
    the fresher signal since Phase 6 retired the style_edits in-app
    producer), then legacy style_edits rows to fill the remaining slots
    (the CLI ingest script is that table's only remaining producer).
    `exclude_chapter_id` keeps the chapter being translated out of the
    segment side (its own edits ride the APPROVED TRANSLATIONS block).

    Returns [] when the tables don't exist (older DB), no edits are
    captured yet, or PROMPT_INCLUDE_STYLE_EDITS is disabled."""
    if not PROMPT_INCLUDE_STYLE_EDITS:
        return []
    result: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _take(before: str, after: str) -> None:
        key = (before or "").strip()[:_STYLE_PAIR_KEY_CHARS]
        if not key or key in seen:
            return
        seen.add(key)
        result.append((before, after))

    try:
        segment_pairs = await segments_svc.recent_edited_pairs(
            conn, novel_id, exclude_chapter_id or 0, STYLE_EDIT_LIMIT
        )
    except aiosqlite.OperationalError:
        segment_pairs = []
    for before, after in segment_pairs:
        _take(before, after)

    if len(result) < STYLE_EDIT_LIMIT:
        try:
            cur = await conn.execute(
                "SELECT before_text, after_text FROM style_edits "
                "WHERE novel_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (novel_id, STYLE_EDIT_LIMIT),
            )
            rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            rows = []
        for r in rows:
            if len(result) >= STYLE_EDIT_LIMIT:
                break
            _take(r["before_text"], r["after_text"])
    return result[:STYLE_EDIT_LIMIT]


class NovelGenreBrief(TypedDict):
    """Fixed 3-key shape returned by `fetch_novel_genre_brief`. The queue
    worker subscripts these keys directly when building the translator call,
    so naming the shape lets a typo or a dropped key surface at type-check
    time instead of as a runtime KeyError."""
    genre: str | None
    custom_style_brief: str | None
    source_language: str


async def fetch_novel_genre_brief(
    conn: aiosqlite.Connection, novel_id: int
) -> NovelGenreBrief:
    """Pull the novel's genre, custom_style_brief, and source_language for
    prompt building. Genre / brief may be NULL: the translator's
    build_system_instruction handles NULL to DEFAULT_GENRE fallback.
    source_language defaults to 'zh' for legacy rows."""
    try:
        cur = await conn.execute(
            "SELECT genre, custom_style_brief, source_language FROM novels WHERE id = ?",
            (novel_id,),
        )
        row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return {"genre": None, "custom_style_brief": None, "source_language": "zh"}
    if row is None:
        return {"genre": None, "custom_style_brief": None, "source_language": "zh"}
    return {
        "genre": row["genre"],
        "custom_style_brief": row["custom_style_brief"],
        "source_language": row["source_language"] or "zh",
    }


async def resolve_translator_provider(
    conn: aiosqlite.Connection, novel_id: int
) -> Provider | None:
    """Resolve the Provider this novel's chapters should route to.

    Lookup order: novel's translator_provider_id to its providers row, then
    fallback to the global default provider (is_default=1). Returns None if
    no providers are configured at all: `translate_chapter` then falls
    through to the backward-compat `translator_factory()` so the env-var-
    driven setup still works for the startup probe and tests.
    """
    try:
        cur = await conn.execute(
            "SELECT translator_provider_id FROM novels WHERE id = ?",
            (novel_id,),
        )
        row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if row is not None and row["translator_provider_id"] is not None:
        provider = await load_provider(row["translator_provider_id"])
        if provider is not None:
            return provider
        logger.warning(
            "novel %d references provider %d but the row is gone; falling back to default",
            novel_id, row["translator_provider_id"],
        )
    return await get_default_provider()


async def fetch_style_note(
    conn: aiosqlite.Connection, novel_id: int
) -> str | None:
    """Pull the per-novel style brief (250-300 word voice anchor). Returns
    None when the novel hasn't had one generated yet, or when
    PROMPT_INCLUDE_STYLE_NOTE is disabled: the prompt drops the block
    entirely in either case."""
    if not PROMPT_INCLUDE_STYLE_NOTE:
        return None
    try:
        cur = await conn.execute(
            "SELECT style_note FROM novels WHERE id = ?", (novel_id,)
        )
        r = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if r is None:
        return None
    note = r["style_note"]
    return note if note and note.strip() else None


async def fetch_previous_chapter_tail(
    conn: aiosqlite.Connection, novel_id: int, chapter_num: int
) -> str | None:
    """Pull a previous chapter's final paragraphs (English) as a tone reference
    for the translator.

    Search rule: nearest earlier chapter with status='done', within
    PREVIOUS_CONTEXT_MAX_GAP chapters back. Returns None on the first chapter,
    when no done chapter exists within the gap window, or when the feature
    is disabled."""
    if not PREVIOUS_CONTEXT_ENABLED or chapter_num <= 1:
        return None
    floor = chapter_num - PREVIOUS_CONTEXT_MAX_GAP
    cur = await conn.execute(
        "SELECT translated_text FROM chapters "
        "WHERE novel_id = ? AND chapter_num < ? AND chapter_num >= ? "
        "  AND status = 'done' "
        "ORDER BY chapter_num DESC LIMIT 1",
        (novel_id, chapter_num, floor),
    )
    prev = await cur.fetchone()
    if prev is None:
        return None
    body = prev["translated_text"]
    if not body:
        return None
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    if not paragraphs:
        return None
    tail = paragraphs[-PREVIOUS_CONTEXT_PARAGRAPHS:]
    return "\n\n".join(tail)
