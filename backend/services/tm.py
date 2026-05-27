"""Translation memory — paragraph-aligned source ↔ target index
(Initiative 5).

Populated by the queue worker on every successful chapter commit; queried
by the reader's concordance panel and by inconsistency detection.

**Alignment.** Phase 5.0 empirical check showed source paragraphs use
`\\r\\n\\r\\n` (CRLF blank lines) while target uses `\\n\\n` (LF). After
normalizing both and stripping the leading Chinese title line, delta is
0–1 for every sampled chapter — the off-by-one is the title-on-first-line
case. The aligner accepts delta ≤ 2; anything more signals a parser
hiccup and we skip the chapter rather than risk wrong-paragraph TM rows.

The plan's `alignment_confidence` column turned out to be unnecessary —
the empirical bimodal pattern is "well-aligned" or "skip entirely",
with nothing in between worth surfacing as a confidence score.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


# Tolerance for the source / target paragraph count delta. delta=0 means
# exact match; delta=1 is the "source had a Chinese title line, target
# put title in title_en" case. delta=2 gives a tiny cushion. Anything
# more probably means the parser or the model dropped a paragraph —
# better to skip the whole chapter than store wrong-paragraph pairs.
_MAX_ALIGNMENT_DELTA = 2

# CJK paragraph break: blank line under either CRLF or LF line endings.
_PARAGRAPH_BREAK_RE = re.compile(r"(?:\r?\n){2,}")

# CJK chapter heading patterns we want to drop from the source before
# pairing — the target's title is in title_en (a separate column) so the
# corresponding paragraph index would otherwise be off-by-one. Conservative
# match: only strip when the FIRST source paragraph is a heading-shaped
# short line. Mirrors `_PRINTED_NUMBER_RE` in parser.py.
_HEADING_RE = re.compile(
    r"^[ \t]*第[\d零〇一二三四五六七八九十百千万两]+[ \t]*[章回节][^\n]*$",
)


def _hash_source(text: str) -> str:
    """Stable 16-hex prefix of SHA256(text). Cheap, collision-resistant
    enough for per-novel TM lookup."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank lines (CRLF or LF), strip whitespace, drop empties."""
    if not text:
        return []
    return [p.strip() for p in _PARAGRAPH_BREAK_RE.split(text) if p.strip()]


def _drop_leading_heading(paragraphs: list[str]) -> list[str]:
    """If the first paragraph is a Chinese chapter heading, drop it.

    Keeps source and target paragraph indices in step after the title
    extraction. Idempotent: returns the input unchanged when no heading
    is on the first line."""
    if paragraphs and _HEADING_RE.match(paragraphs[0]):
        return paragraphs[1:]
    return paragraphs


@dataclass(frozen=True)
class AlignedPair:
    paragraph_index: int
    source_text: str
    target_text: str
    source_hash: str


def align_paragraphs(
    source_text: str, target_text: str
) -> list[AlignedPair] | None:
    """Return aligned (source, target) pairs, or None when alignment
    fails (delta > _MAX_ALIGNMENT_DELTA).

    On delta = 1: the shorter side is treated as the canonical paragraph
    sequence; the extra paragraph on the longer side is dropped from the
    tail (a paragraph the model split that the source didn't, or vice
    versa). Stable: same inputs always produce the same pairs.
    """
    if not source_text or not target_text:
        return None
    src = _drop_leading_heading(_split_paragraphs(source_text))
    tgt = _split_paragraphs(target_text)
    if not src or not tgt:
        return None
    delta = abs(len(src) - len(tgt))
    if delta > _MAX_ALIGNMENT_DELTA:
        return None
    n = min(len(src), len(tgt))
    return [
        AlignedPair(
            paragraph_index=i,
            source_text=src[i],
            target_text=tgt[i],
            source_hash=_hash_source(src[i]),
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Populate / replace
# ---------------------------------------------------------------------------


async def replace_chapter_segments(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_id: int,
    source_text: str,
    target_text: str,
) -> int:
    """Replace the TM rows for one chapter with a fresh aligned set.

    Atomic relative to the surrounding transaction — the queue worker
    calls this between its chapter UPDATE and the COMMIT, so a chapter's
    TM stays in lockstep with the chapter body it describes.

    Returns the number of rows written. 0 means alignment failed (count
    too far off) — the chapter is left without TM coverage rather than
    populating wrong-paragraph pairs.
    """
    pairs = align_paragraphs(source_text, target_text)
    # Always wipe prior rows first — even if we won't repopulate. A
    # retranslation whose new output doesn't align should not leave the
    # PREVIOUS run's rows in place; that would silently misrepresent the
    # chapter's current text.
    await conn.execute(
        "DELETE FROM tm_segments WHERE chapter_id = ?", (chapter_id,)
    )
    if not pairs:
        logger.info(
            "tm: chapter %d skipped (paragraph alignment failed)", chapter_id
        )
        return 0
    await conn.executemany(
        "INSERT INTO tm_segments "
        "(novel_id, chapter_id, paragraph_index, source_text, target_text, source_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (novel_id, chapter_id, p.paragraph_index, p.source_text,
             p.target_text, p.source_hash)
            for p in pairs
        ],
    )
    return len(pairs)


# ---------------------------------------------------------------------------
# Concordance search
# ---------------------------------------------------------------------------


@dataclass
class ConcordanceHit:
    chapter_id: int
    chapter_num: int
    chapter_title_en: str | None
    paragraph_index: int
    source_text: str
    target_text: str
    # 'source' when the query matched the Chinese; 'target' when it
    # matched the English. The reader uses this to know which pane to
    # scroll on the matched chapter.
    matched_side: str


_MIN_QUERY_LENGTH = 2
_CONCORDANCE_LIMIT = 50


async def search(
    conn: aiosqlite.Connection,
    novel_id: int,
    query: str,
    search_sides: tuple[str, ...] = ("source", "target"),
) -> list[ConcordanceHit]:
    """Substring search across one novel's TM. Case-insensitive on the
    English side (target_text); the source side matches verbatim because
    Chinese is unambiguously cased.

    Capped at `_CONCORDANCE_LIMIT` to keep the panel responsive on
    very-common queries (a character name might match every chapter).
    Capped queries truncate from the end — the user sees the FIRST hits
    in reading order, which is what concordance is for."""
    q = (query or "").strip()
    if len(q) < _MIN_QUERY_LENGTH:
        return []

    # Build the WHERE clause. INSTR returns the 1-based offset (0 = miss)
    # and is SQLite-native, so we get index-aware scans on
    # idx_tm_novel_hash for the novel_id filter plus a linear scan over
    # the matching rows for the substring — which is fast at 5-50k rows.
    conditions: list[str] = []
    params: list = [novel_id]
    if "source" in search_sides:
        conditions.append("INSTR(t.source_text, ?) > 0")
        params.append(q)
    if "target" in search_sides:
        # Case-insensitive on English: lower(target) LIKE lower(query)
        # with proper escaping. INSTR is case-sensitive in SQLite by
        # default, so we go LIKE for the target side. The %-anchors cost
        # us prefix-index optimization, but TM tables stay small per
        # novel so we accept the linear scan.
        conditions.append("LOWER(t.target_text) LIKE LOWER(?)")
        params.append(f"%{q}%")
    if not conditions:
        return []

    sql = (
        "SELECT t.chapter_id, c.chapter_num, c.title_en, t.paragraph_index, "
        "       t.source_text, t.target_text, "
        f"       CASE WHEN INSTR(t.source_text, ?) > 0 THEN 'source' "
        "            ELSE 'target' END AS matched_side "
        "FROM tm_segments t "
        "JOIN chapters c ON c.id = t.chapter_id "
        f"WHERE t.novel_id = ? AND ({' OR '.join(conditions)}) "
        "ORDER BY c.chapter_num, t.paragraph_index "
        "LIMIT ?"
    )
    # The CASE-WHEN above needs its own bound parameter (q again).
    bound = [q, novel_id] + params[1:] + [_CONCORDANCE_LIMIT]
    cur = await conn.execute(sql, bound)
    rows = await cur.fetchall()
    return [
        ConcordanceHit(
            chapter_id=r["chapter_id"],
            chapter_num=r["chapter_num"],
            chapter_title_en=r["title_en"],
            paragraph_index=r["paragraph_index"],
            source_text=r["source_text"],
            target_text=r["target_text"],
            matched_side=r["matched_side"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Inconsistency detection
# ---------------------------------------------------------------------------


@dataclass
class InconsistencyGroup:
    """One source paragraph that the model has rendered into ≥ 2 distinct
    English forms across the novel. The renderings ARE the disagreement;
    the reader's QA panel surfaces this as an observation kind so the
    user can pick which rendering to standardize on."""

    source_text: str
    source_hash: str
    renderings: list[dict]  # [{target_text, chapters: [{chapter_id, chapter_num, title_en}]}]
    total_occurrences: int


async def find_inconsistencies(
    conn: aiosqlite.Connection, novel_id: int
) -> list[InconsistencyGroup]:
    """Group TM rows by source_hash and return groups where the same
    source paragraph maps to multiple distinct target_text values.

    Deliberately exact-match on target — a one-character casing
    difference IS the kind of drift this is designed to surface. The
    reader's QA panel can then offer "standardize on rendering X"
    (which would invoke Initiative 4's find/replace engine)."""
    cur = await conn.execute(
        "SELECT source_hash, source_text, target_text, chapter_id, "
        "       c.chapter_num, c.title_en "
        "FROM tm_segments t "
        "JOIN chapters c ON c.id = t.chapter_id "
        "WHERE t.novel_id = ? "
        "ORDER BY source_hash, target_text",
        (novel_id,),
    )
    rows = await cur.fetchall()
    # Group: source_hash → {target_text → [chapter_meta]}
    by_hash: dict[str, dict] = {}
    src_text_by_hash: dict[str, str] = {}
    for r in rows:
        h = r["source_hash"]
        src_text_by_hash.setdefault(h, r["source_text"])
        by_target = by_hash.setdefault(h, {})
        by_target.setdefault(r["target_text"], []).append(
            {
                "chapter_id": r["chapter_id"],
                "chapter_num": r["chapter_num"],
                "title_en": r["title_en"],
            }
        )
    out: list[InconsistencyGroup] = []
    for h, by_target in by_hash.items():
        if len(by_target) < 2:
            continue
        renderings = [
            {"target_text": t, "chapters": chs}
            for t, chs in sorted(by_target.items())
        ]
        total = sum(len(r["chapters"]) for r in renderings)
        out.append(
            InconsistencyGroup(
                source_text=src_text_by_hash[h],
                source_hash=h,
                renderings=renderings,
                total_occurrences=total,
            )
        )
    # Surface highest-impact (most-occurring) inconsistencies first.
    out.sort(key=lambda g: -g.total_occurrences)
    return out
