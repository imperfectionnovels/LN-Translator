"""Translation memory — paragraph-aligned source ↔ target index
(Initiative 5).

Populated by the queue worker on every successful chapter commit; queried
by the reader's concordance panel and by inconsistency detection.

**Alignment.** Source paragraphs use `\\r\\n\\r\\n` (CRLF blank lines)
while the target uses `\\n\\n` (LF); both are normalized and the leading
Chinese title line is stripped from the source (the target's title lives
in `title_en`). A naive positional zip then assumed the two sides had the
same paragraph count in the same order, which the faithful one-line-per-
sentence style breaks constantly: the target adds standalone beats (a
lone "CRACK!"), or splits a paragraph the source kept whole, and every
later pair silently shifts onto the wrong line. Even equal counts did not
guarantee correspondence (one insertion plus one merge nets to delta 0).

The aligner is now length-based (Gale-Church-lite): a target paragraph is
expected to run `ratio` times the length of its source paragraph, and a
short dynamic program finds the minimum-cost order-preserving alignment,
keeping only its confident 1:1 anchors. Inserted or deleted paragraphs
are dropped, so a stored pair always describes genuinely corresponding
text. When too few paragraphs anchor, the chapter is skipped rather than
populated with doubtful rows.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


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


# A 1:1 anchor is dropped as implausible when its target paragraph runs under
# 1/_OUTLIER_SHORT_FACTOR of the length the source predicts AND the absolute
# character gap exceeds _OUTLIER_MIN_GAP. The gap floor keeps short paragraphs,
# where a terse rendering is normal, from ever being dropped on ratio alone.
_OUTLIER_SHORT_FACTOR = 4
_OUTLIER_MIN_GAP = 20


def _length_align(src: list[str], tgt: list[str]) -> list[tuple[int, int]] | None:
    """Order-preserving length-based alignment (Gale-Church-lite).

    Returns the list of confident 1:1 `(source_index, target_index)`
    matches, or None when too few paragraphs anchor to trust the result.

    The model: a target paragraph's length is expected to be `ratio` times
    its source paragraph's length, where `ratio` is the whole-chapter
    English-to-Chinese character expansion. A 1:1 match costs the absolute
    length discrepancy; skipping a paragraph (a pure insertion or deletion)
    costs its length plus a penalty. A dynamic program finds the minimum-
    cost order-preserving alignment, and we keep only its 1:1 anchors, so an
    inserted beat the source lacks is dropped instead of mispaired.
    """
    m, n = len(src), len(tgt)
    src_len = [len(s) for s in src]
    tgt_len = [len(t) for t in tgt]
    total_src = sum(src_len) or 1
    total_tgt = sum(tgt_len) or 1
    ratio = total_tgt / total_src
    # Skip penalty scales with the mean target paragraph so behavior is the
    # same regardless of paragraph size. It biases toward keeping a real 1:1
    # match rather than splitting it into a separate insert and delete.
    penalty = 0.5 * (total_tgt / n)

    inf = float("inf")
    # dp[i][j] = min cost to align src[:i] with tgt[:j]; back[i][j] is the
    # move taken to reach it, as (kind, prev_i, prev_j).
    dp = [[inf] * (n + 1) for _ in range(m + 1)]
    back: list[list[tuple[str, int, int] | None]] = [
        [None] * (n + 1) for _ in range(m + 1)
    ]
    dp[0][0] = 0.0
    for i in range(m + 1):
        for j in range(n + 1):
            if i == 0 and j == 0:
                continue
            best, move = inf, None
            # Match src[i-1] with tgt[j-1]. Evaluated first so it wins ties:
            # when matching and skipping cost the same, keep the pair.
            if i > 0 and j > 0:
                c = dp[i - 1][j - 1] + abs(tgt_len[j - 1] - ratio * src_len[i - 1])
                if c < best:
                    best, move = c, ("match", i - 1, j - 1)
            # Delete src[i-1]: a source paragraph with no target counterpart.
            if i > 0:
                c = dp[i - 1][j] + ratio * src_len[i - 1] + penalty
                if c < best:
                    best, move = c, ("del", i - 1, j)
            # Insert tgt[j-1]: a target-only paragraph, e.g. an added beat.
            if j > 0:
                c = dp[i][j - 1] + tgt_len[j - 1] + penalty
                if c < best:
                    best, move = c, ("ins", i, j - 1)
            dp[i][j] = best
            back[i][j] = move

    matches: list[tuple[int, int]] = []
    i, j = m, n
    while i > 0 or j > 0:
        move = back[i][j]
        assert move is not None  # every non-origin cell has a predecessor
        kind, pi, pj = move
        if kind == "match":
            matches.append((pi, pj))
        i, j = pi, pj
    matches.reverse()

    # Drop length-implausible anchors. The DP always prefers a 1:1 match over
    # delete-plus-insert, so when a long source paragraph's real rendering was
    # moved elsewhere and a tiny standalone beat ("Amitabha.") sits in its
    # slot, the two get matched anyway. An anchor whose target runs far under
    # its expected length is almost certainly that case, not a genuinely terse
    # translation, so drop it rather than store a misleading pair.
    matches = [
        (i, j)
        for (i, j) in matches
        if not (
            tgt_len[j] * _OUTLIER_SHORT_FACTOR < ratio * src_len[i]
            and ratio * src_len[i] - tgt_len[j] > _OUTLIER_MIN_GAP
        )
    ]

    # Confidence guard: if fewer than half the paragraphs on the longer side
    # found a 1:1 anchor, the two texts do not correspond well enough to
    # trust. Skip rather than store doubtful pairs (the conservative stance
    # the old delta gate took, now content-aware instead of count-based).
    if len(matches) < 0.5 * max(m, n):
        return None
    return matches


def align_paragraphs(
    source_text: str, target_text: str
) -> list[AlignedPair] | None:
    """Return confident 1:1 (source, target) paragraph pairs, or None when
    the two texts do not align well enough to trust.

    Splits both sides on blank lines, drops a leading Chinese chapter
    heading from the source (the target's title lives in `title_en`), then
    runs a length-based alignment that tolerates the target inserting or
    splitting paragraphs the source did not. Only the 1:1 anchors are
    returned; inserted and deleted paragraphs are dropped so a stored pair
    always describes genuinely corresponding text. Stable: same inputs
    always produce the same pairs.
    """
    if not source_text or not target_text:
        return None
    src = _drop_leading_heading(_split_paragraphs(source_text))
    tgt = _split_paragraphs(target_text)
    if not src or not tgt:
        return None
    matches = _length_align(src, tgt)
    if not matches:
        return None
    return [
        AlignedPair(
            paragraph_index=i,
            source_text=src[i],
            target_text=tgt[j],
            source_hash=_hash_source(src[i]),
        )
        for (i, j) in matches
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
