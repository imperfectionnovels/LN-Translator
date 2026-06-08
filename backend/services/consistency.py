"""Edit-mode consistency aid: deterministic, read-only cross-chapter drift.

Powers the reader's edit-mode consistency rail. Two detectors, no LLM:

1. **Fuzzy tier** (the OmegaT-style feature): for each paragraph of the chapter
   being edited, find near-duplicate Chinese source paragraphs ELSEWHERE in the
   novel (via `rapidfuzz` over script-folded Han) whose English rendering
   differs from this chapter's current rendering. Exact (script-folded) source
   matches are marked `exact`: that tier is the classic same-source /
   different-target drift the TM concordance already knows about, in context.

2. **Glossary tier**: locked glossary terms whose Chinese appears in the source
   but whose English is absent from the translation (the existing
   `glossary.missing_translator_terms` observer, recomputed LIVE against the
   current glossary + current body instead of frozen at translate time).

Correctness contracts (see the consistency-aid plan):
  - The current chapter is indexed + rendered LIVE: paragraphs come from
    `tm.align_paragraphs(original_text, displayed_body)`, so `paragraph_index`
    matches the reader's `data-paragraph-index` (used by `_scrollToParagraph`)
    and `current_rendering` reflects post-edit / refined text, never the
    chapter's own (possibly stale) `tm_segments` rows.
  - `displayed_body` is `refined_text` when refinement is done, else
    `translated_text`, matching the reader's `_displayedEnglish`.
  - Only OTHER chapters' `tm_segments` form the comparison corpus.

This module performs NO writes. The rail's fix actions reuse the reader's
existing in-context tools (paragraph edit, glossary editor); nothing here
mutates a chapter or the glossary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiosqlite
from rapidfuzz import fuzz, process

from backend.models import GlossaryEntry
from backend.services import glossary as glossary_svc
from backend.services import tm as tm_svc
from backend.services.glossary_filters import canonical_zh, split_aliases

logger = logging.getLogger(__name__)

# Fuzzy similarity floor (rapidfuzz ratio is 0..100). 0.90 keeps the rail to
# genuine near-duplicates; combined with the "rendering differs" gate it stays
# high-signal.
_DEFAULT_THRESHOLD = 0.90
# Per-paragraph cap on surfaced other-chapter renderings.
_DEFAULT_TOP_K = 3
# Hard cap on candidate source paragraphs scored per current paragraph, after
# the length-band prefilter, bounds the fuzzy compute on large novels.
_MAX_CANDIDATES = 100
# Skip trivially short source paragraphs (stock interjections); they fuzzy-
# match everything and add only noise. Measured in script-folded Han chars.
_MIN_SOURCE_LEN = 6
# A score this high is treated as an exact (script-folded) source match.
_EXACT_SCORE = 99.5


@dataclass
class OtherRendering:
    """How a near-duplicate source paragraph was rendered in another chapter."""

    chapter_num: int
    target_text: str
    similarity: float  # 0..1 (rapidfuzz ratio / 100)
    exact: bool


@dataclass
class ConsistencyMatch:
    """One current-chapter paragraph whose source recurs elsewhere with a
    different rendering than the one shown here."""

    paragraph_index: int
    source_text: str
    current_rendering: str
    others: list[OtherRendering] = field(default_factory=list)


@dataclass
class GlossaryFlag:
    """A locked glossary term present in the source but absent from the
    translation (likely rendered off-canon)."""

    term_id: int | None
    term_zh: str
    expected_en: str
    paragraph_index: int | None


@dataclass
class ConsistencyResult:
    status: str  # "ok" | "not_translated" | "tm_unavailable"
    matches: list[ConsistencyMatch] = field(default_factory=list)
    glossary_flags: list[GlossaryFlag] = field(default_factory=list)


def _displayed_body(row) -> str | None:
    """Mirror the reader's `_displayedEnglish`: refined when done, else draft."""
    if (row["refinement_status"] or "none") == "done" and row["refined_text"]:
        return row["refined_text"]
    return row["translated_text"]


async def consistency_for_chapter(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_num: int,
    *,
    threshold: float = _DEFAULT_THRESHOLD,
    top_k: int = _DEFAULT_TOP_K,
    max_candidates: int = _MAX_CANDIDATES,
    min_source_len: int = _MIN_SOURCE_LEN,
) -> ConsistencyResult | None:
    """Build the consistency findings for one chapter. Returns None when the
    chapter does not exist (the route maps that to 404). Read-only."""
    cur = await conn.execute(
        "SELECT id, original_text, translated_text, refined_text, "
        "       refinement_status "
        "FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    row = await cur.fetchone()
    if row is None:
        return None

    body = _displayed_body(row)
    original = row["original_text"] or ""
    if not body or not original:
        return ConsistencyResult(status="not_translated")

    chapter_id = row["id"]

    # Current chapter: align LIVE so paragraph_index matches the reader and
    # current_rendering reflects edits / refinement.
    pairs = tm_svc.align_paragraphs(original, body)
    glossary = await glossary_svc.list_for_novel(conn, novel_id)
    flags = _glossary_flags(original, body, glossary, pairs)

    if pairs is None:
        # Can't reliably map paragraphs -> skip the fuzzy tier but still
        # surface glossary flags (they scan the whole chapter).
        return ConsistencyResult(status="tm_unavailable", glossary_flags=flags)

    corpus = await _build_other_corpus(conn, novel_id, chapter_id, min_source_len)
    matches = _fuzzy_matches(pairs, corpus, threshold, top_k, max_candidates, min_source_len)
    return ConsistencyResult(status="ok", matches=matches, glossary_flags=flags)


async def glossary_flags_for_chapter(
    conn: aiosqlite.Connection, novel_id: int, chapter_num: int
) -> list[GlossaryFlag]:
    """Standalone locked-term flag pass for one chapter (read-only)."""
    cur = await conn.execute(
        "SELECT original_text, translated_text, refined_text, refinement_status "
        "FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    row = await cur.fetchone()
    if row is None:
        return []
    body = _displayed_body(row)
    original = row["original_text"] or ""
    if not body or not original:
        return []
    glossary = await glossary_svc.list_for_novel(conn, novel_id)
    pairs = tm_svc.align_paragraphs(original, body)
    return _glossary_flags(original, body, glossary, pairs)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _CorpusEntry:
    canon: str
    length: int
    # distinct rendering -> earliest chapter_num that produced it
    renderings: dict[str, int]


async def _build_other_corpus(
    conn: aiosqlite.Connection,
    novel_id: int,
    current_chapter_id: int,
    min_source_len: int,
) -> list[_CorpusEntry]:
    """Dedup every OTHER chapter's TM source paragraphs by script-folded form,
    collecting the distinct renderings (and the earliest chapter each)."""
    cur = await conn.execute(
        "SELECT t.source_text, t.target_text, c.chapter_num "
        "FROM tm_segments t JOIN chapters c ON c.id = t.chapter_id "
        "WHERE t.novel_id = ? AND t.chapter_id != ?",
        (novel_id, current_chapter_id),
    )
    rows = await cur.fetchall()
    by_canon: dict[str, _CorpusEntry] = {}
    for r in rows:
        src = r["source_text"] or ""
        canon = canonical_zh(src)
        if len(canon) < min_source_len:
            continue
        target = r["target_text"] or ""
        if not target:
            continue
        ch = r["chapter_num"]
        entry = by_canon.get(canon)
        if entry is None:
            by_canon[canon] = _CorpusEntry(canon=canon, length=len(canon),
                                           renderings={target: ch})
            continue
        prev = entry.renderings.get(target)
        if prev is None or ch < prev:
            entry.renderings[target] = ch
    return list(by_canon.values())


def _fuzzy_matches(
    pairs,
    corpus: list[_CorpusEntry],
    threshold: float,
    top_k: int,
    max_candidates: int,
    min_source_len: int,
) -> list[ConsistencyMatch]:
    if not corpus:
        return []
    cutoff = threshold * 100.0
    matches: list[ConsistencyMatch] = []
    for p in pairs:
        c_canon = canonical_zh(p.source_text)
        if len(c_canon) < min_source_len:
            continue
        current = p.target_text
        lo = len(c_canon) * 0.85
        hi = len(c_canon) * 1.15
        candidates = [e for e in corpus if lo <= e.length <= hi]
        if not candidates:
            continue
        if len(candidates) > max_candidates:
            candidates.sort(key=lambda e: abs(e.length - len(c_canon)))
            candidates = candidates[:max_candidates]
        index = {e.canon: e for e in candidates}
        # rapidfuzz returns (choice, score, key) desc by score, >= cutoff.
        scored = process.extract(
            c_canon, list(index.keys()),
            scorer=fuzz.ratio, score_cutoff=cutoff, limit=top_k * 4 or 4,
        )
        others: list[OtherRendering] = []
        seen_targets: set[str] = set()
        for choice, score, _key in scored:
            entry = index[choice]
            exact = score >= _EXACT_SCORE
            sim = round(score / 100.0, 3)
            for target, ch in entry.renderings.items():
                if target == current or target in seen_targets:
                    continue
                seen_targets.add(target)
                others.append(OtherRendering(chapter_num=ch, target_text=target,
                                             similarity=sim, exact=exact))
        if not others:
            continue
        others.sort(key=lambda o: (not o.exact, -o.similarity, o.chapter_num))
        matches.append(ConsistencyMatch(
            paragraph_index=p.paragraph_index,
            source_text=p.source_text,
            current_rendering=current,
            others=others[:top_k],
        ))
    return matches


def _glossary_flags(
    original: str,
    body: str,
    glossary: list[GlossaryEntry],
    pairs,
) -> list[GlossaryFlag]:
    """Locked terms present in the source but absent from the translation.

    Reuses the existing locked-only `missing_translator_terms` (with all its
    alias / variant / false-split guards), then maps each hit back to its
    glossary row id and a best-effort paragraph index for the jump action.
    """
    missing = glossary_svc.missing_translator_terms(original, body, glossary)
    if not missing:
        return []
    # canonical zh variant -> owning glossary entry
    cmap: dict[str, GlossaryEntry] = {}
    for g in glossary:
        if not g.locked:
            continue
        for zh, _en in split_aliases(g.term_zh or "", g.term_en or ""):
            cz = canonical_zh(zh)
            if cz:
                cmap.setdefault(cz, g)
    # Source paragraphs for the best-effort jump index. align_paragraphs drops
    # the leading heading from source, so source index i == reader index i in
    # the aligned region.
    src_paras = tm_svc._drop_leading_heading(tm_svc._split_paragraphs(original))
    src_canons = [canonical_zh(s) for s in src_paras]
    flags: list[GlossaryFlag] = []
    for zh, en in missing:
        cz = canonical_zh(zh)
        g = cmap.get(cz)
        pidx = next((i for i, sc in enumerate(src_canons) if cz and cz in sc), None)
        flags.append(GlossaryFlag(
            term_id=g.id if g else None,
            term_zh=zh,
            expected_en=en,
            paragraph_index=pidx,
        ))
    return flags
