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
from dataclasses import dataclass

import aiosqlite

# The paragraph splitter, the heading detector, and their regexes are OWNED by
# services/segmentation.py (they are load-bearing for the version-frozen 1:1
# segmentation contract; any behavior change there bumps SEGMENTATION_VERSION).
# Re-exported here under their historical names so this module's callers
# (consistency.py, consistency_eval.py, tests) keep working unchanged.
from backend.services.segmentation import (  # noqa: F401  (re-export)
    _HEADING_RE,
    _PARAGRAPH_BREAK_RE,
    _drop_leading_heading,
    _split_paragraphs,
)

logger = logging.getLogger(__name__)


def _hash_source(text: str) -> str:
    """Stable 16-hex prefix of SHA256(text). Cheap, collision-resistant
    enough for per-novel TM lookup."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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


def _expansion_ratio(src: list[str], tgt: list[str]) -> float:
    """Whole-chapter English-to-Chinese character expansion ratio."""
    total_src = sum(len(s) for s in src) or 1
    total_tgt = sum(len(t) for t in tgt) or 1
    return total_tgt / total_src


def _dp_moves(
    src: list[str], tgt: list[str], *, groups: bool = False
) -> list[tuple[str, int, int]]:
    """Minimum-cost order-preserving alignment path (Gale-Church-lite DP).

    Returns the COMPLETE move list in forward order. Each move is
    (kind, i, j):
      - 'match' pairs src[i] with tgt[j];
      - 'merge2' pairs src[i] AND src[i+1] with tgt[j] (the translation
        merged two source paragraphs into one; only with groups=True);
      - 'split2' pairs src[i] with tgt[j] AND tgt[j+1] (the translation
        split one source paragraph in two; only with groups=True);
      - 'del' means src[i] has no target counterpart (j is the target
        cursor, unused); 'ins' means tgt[j] is a target-only paragraph
        (i is the source cursor, unused).

    The base cost model: a target paragraph's length is expected to be
    `ratio` times its source paragraph's length, where `ratio` is the
    whole-chapter expansion. A 1:1 match costs the absolute length
    discrepancy; skipping a paragraph costs its length plus a penalty.

    groups=True (the segment-store path, which must assign EVERY source a
    slot) additionally ports the pre-pivot client-side reader aligner's
    model: a 2:1 or 1:2 group must cut the mismatch by more than one
    average paragraph (the step penalty) to beat 1:1, so equal-count
    chapters stay 1:1 instead of reshuffling on length noise, and dropping
    a paragraph outright becomes a last resort (4x penalty): real
    translations merge and split, they almost never delete content, and a
    cheap drop lets the DP delete short dialogue lines and confidently
    mispair everything between two real merge points. The TM-anchor path
    (`_length_align`) keeps groups=False: a group has no valid single 1:1
    anchor to store, so it prefers the legacy drop-tolerant model that
    maximizes clean anchors.
    """
    m, n = len(src), len(tgt)
    src_len = [len(s) for s in src]
    tgt_len = [len(t) for t in tgt]
    ratio = _expansion_ratio(src, tgt)
    # Penalties scale with the mean target paragraph so behavior is the
    # same regardless of paragraph size.
    avg_t = (sum(tgt_len) or 1) / n if n else 0.0
    step_pen = avg_t
    drop_pen = 4.0 * avg_t if groups else 0.5 * avg_t

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
            # when matching and grouping cost the same, keep the pair.
            if i > 0 and j > 0:
                c = dp[i - 1][j - 1] + abs(tgt_len[j - 1] - ratio * src_len[i - 1])
                if c < best:
                    best, move = c, ("match", i - 1, j - 1)
            # Merge src[i-2]+src[i-1] into tgt[j-1].
            if groups and i > 1 and j > 0:
                c = dp[i - 2][j - 1] + step_pen + abs(
                    tgt_len[j - 1] - ratio * (src_len[i - 2] + src_len[i - 1])
                )
                if c < best:
                    best, move = c, ("merge2", i - 2, j - 1)
            # Split src[i-1] into tgt[j-2]+tgt[j-1].
            if groups and i > 0 and j > 1:
                c = dp[i - 1][j - 2] + step_pen + abs(
                    tgt_len[j - 2] + tgt_len[j - 1] - ratio * src_len[i - 1]
                )
                if c < best:
                    best, move = c, ("split2", i - 1, j - 2)
            # Delete src[i-1]: a source paragraph with no target counterpart.
            if i > 0:
                c = dp[i - 1][j] + ratio * src_len[i - 1] + drop_pen
                if c < best:
                    best, move = c, ("del", i - 1, j)
            # Insert tgt[j-1]: a target-only paragraph, e.g. an added beat.
            if j > 0:
                c = dp[i][j - 1] + tgt_len[j - 1] + drop_pen
                if c < best:
                    best, move = c, ("ins", i, j - 1)
            dp[i][j] = best
            back[i][j] = move

    moves: list[tuple[str, int, int]] = []
    i, j = m, n
    while i > 0 or j > 0:
        move = back[i][j]
        assert move is not None  # every non-origin cell has a predecessor
        kind, pi, pj = move
        moves.append((kind, pi, pj))
        i, j = pi, pj
    moves.reverse()
    return moves


def _drop_short_outliers(
    matches: list[tuple[int, int]], src: list[str], tgt: list[str]
) -> list[tuple[int, int]]:
    """Drop length-implausible anchors. The DP always prefers a 1:1 match over
    delete-plus-insert, so when a long source paragraph's real rendering was
    moved elsewhere and a tiny standalone beat ("Amitabha.") sits in its
    slot, the two get matched anyway. An anchor whose target runs far under
    its expected length is almost certainly that case, not a genuinely terse
    translation, so drop it rather than store a misleading pair."""
    ratio = _expansion_ratio(src, tgt)
    return [
        (i, j)
        for (i, j) in matches
        if not (
            len(tgt[j]) * _OUTLIER_SHORT_FACTOR < ratio * len(src[i])
            and ratio * len(src[i]) - len(tgt[j]) > _OUTLIER_MIN_GAP
        )
    ]


def _length_align(src: list[str], tgt: list[str]) -> list[tuple[int, int]] | None:
    """Order-preserving length-based alignment (Gale-Church-lite).

    Returns the list of confident 1:1 `(source_index, target_index)`
    matches, or None when too few paragraphs anchor to trust the result.
    We keep only the DP path's 1:1 anchors, so an inserted beat the source
    lacks is dropped instead of mispaired.
    """
    moves = _dp_moves(src, tgt)
    matches = [(i, j) for kind, i, j in moves if kind == "match"]
    matches = _drop_short_outliers(matches, src, tgt)

    # Confidence guard: if fewer than half the paragraphs on the longer side
    # found a 1:1 anchor, the two texts do not correspond well enough to
    # trust. Skip rather than store doubtful pairs (the conservative stance
    # the old delta gate took, now content-aware instead of count-based).
    if len(matches) < 0.5 * max(len(src), len(tgt)):
        return None
    return matches


def full_alignment_path(
    src: list[str], tgt: list[str]
) -> list[tuple[str, bool]] | None:
    """Complete per-source alignment for the segment store (CAT Phase 2).

    Where `_length_align` keeps only the confident 1:1 anchors, this walks
    the same DP path and assigns EVERY source index a target string:

      - a confident 1:1 anchor maps directly (aligned=True);
      - a 2:1 merge ('merge2') lands the shared target on the FIRST source
        row of the group; the follow-on row stays empty; both rows are
        aligned=False (the target covers more than either source alone);
      - a 1:2 split ('split2') lands both target halves on their source
        row, blank-line joined, aligned=False;
      - a target-only paragraph ('ins' move) attaches to its nearest source
        neighbor on the path (the source consumed just before it, or the
        first source when inserts precede every source), joined with a blank
        line, so the concatenation of all targets still reproduces the body;
      - a source paragraph with no plausible target ('del' move) is
        aligned=False; it ends up with "" unless an inserted target attaches
        to it (every target must land somewhere to keep the join invariant);
      - a length-implausible anchor (the same outlier rule `_length_align`
        drops) keeps its target text but is demoted to aligned=False.

    Returns a list of (target_text, aligned) tuples, one per source index,
    or None below the <50% coverage gate. Coverage counts every index that
    takes part in a confident 1:1 anchor or a 2:1 / 1:2 group (a group is
    a deliberate length fit, not a skip), so legacy merge-heavy chapters
    that anchor few pure 1:1 pairs still align through their groups.
    """
    if not src or not tgt:
        return None
    # Very divergent counts: 1:1 / 2:1 / 1:2 moves cannot span the gap
    # cleanly, and a forced grouping misleads more than it helps (same
    # pre-gate the proven client-side aligner used before falling back to
    # independent panes).
    if abs(len(src) - len(tgt)) / max(len(src), len(tgt)) > 0.5:
        return None
    moves = _dp_moves(src, tgt, groups=True)
    matches = [(i, j) for kind, i, j in moves if kind == "match"]
    confident = set(_drop_short_outliers(matches, src, tgt))
    covered_src = covered_tgt = 0
    for kind, i, j in moves:
        if kind == "match" and (i, j) in confident:
            covered_src += 1
            covered_tgt += 1
        elif kind == "merge2":
            covered_src += 2
            covered_tgt += 1
        elif kind == "split2":
            covered_src += 1
            covered_tgt += 2
    if covered_src < 0.5 * len(src) or covered_tgt < 0.5 * len(tgt):
        return None

    # Assemble per-source target pieces by walking the path in order.
    parts: list[list[str]] = [[] for _ in src]
    clean_match: list[bool] = [False] * len(src)
    has_extra: list[bool] = [False] * len(src)
    pending: list[str] = []  # inserts seen before the first source index
    last_src: int | None = None
    for kind, i, j in moves:
        if kind == "match":
            parts[i].append(tgt[j])
            clean_match[i] = (i, j) in confident
            last_src = i
        elif kind == "merge2":
            parts[i].append(tgt[j])
            has_extra[i] = True
            last_src = i + 1
        elif kind == "split2":
            parts[i].extend((tgt[j], tgt[j + 1]))
            has_extra[i] = True
            last_src = i
        elif kind == "del":
            last_src = i
        else:  # 'ins'
            if last_src is None:
                pending.append(tgt[j])
            else:
                parts[last_src].append(tgt[j])
                has_extra[last_src] = True
    if pending:
        parts[0] = pending + parts[0]
        has_extra[0] = True

    return [
        ("\n\n".join(parts[i]), clean_match[i] and not has_extra[i])
        for i in range(len(src))
    ]


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


# The old Inconsistency-detection block (InconsistencyGroup +
# find_inconsistencies) was removed 2026-07-30: after its route died it had
# zero production callers. The queue's tm_inconsistency observations come
# from queue.py::_emit_tm_inconsistency_observations' own inline SQL.
