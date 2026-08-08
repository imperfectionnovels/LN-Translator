"""Segment store service (CAT Phases 2+3). The ONLY module that reads or
writes `chapter_segments`.

A segment row is one effective source paragraph of a translated chapter
(`segmentation.chapter_source_paragraphs` over `original_text`) paired with
the paragraph of the DISPLAYED body that renders it. The displayed body (the
chapter text column the reader shows: refined whenever refined_text is
non-empty, else the draft; see `displayed_body`) stays canonical; segments
are a projection of it plus durable
status/provenance state, so `join(target_text ORDER BY seg_index)` always
reproduces the displayed paragraphs.

Phase 2 gave lazy backfill + read (`get_segments`). It must never assume a
paragraph count from the 1:1 pipeline: post-validation fixups (the
mid-sentence comma weld, leading-title strip) can change the committed body's
paragraph count relative to what the translator validated, so the split is
always recomputed from the stored text (docs/decisions.md, 2026-07-30).

Phase 3 adds the single write path:
  - `update_segment` / `confirm_all`: the editor's state machine. Any
    text-changing action rematerializes the displayed body column from
    `join(target_text)` in the SAME transaction (invariant I1).
  - human-row preservation: rebuilds and self-heals re-anchor rows with
    status != 'machine' by source_hash (seg_index fallback) instead of
    deleting them; when alignment fails on a chapter that has human rows,
    every row is RETAINED and the chapter is stamped 'unaligned' (rows kept,
    read-only in the editor) so nothing user-authored ever vanishes.
  - `reproject_from_body`: the hook for text-authoritative mutators
    (find-replace commit, glossary apply-in-place, snapshot restore). Body
    wins on TEXT, rows keep their STATUS (a novel-wide find-replace across
    confirmed segments must not un-confirm them).

Phase 4 makes the WORKER segments-authoritative for human rows:
  - `apply_machine_translation`: the translate/refine commit merge. Human
    rows (edited|confirmed) keep target_text VERBATIM and only refresh
    machine_text with the new AI rendering; machine rows regenerate; the
    returned merged paragraph list becomes the committed body, so a
    retranslate can no longer clobber confirmed work (closes the Phase 3
    "text-authoritative rebuild" interim defect).
  - `prefill_confirmed_exact`: cross-chapter exact-hash pre-fill of
    confirmed renderings (origin 'tm_exact').
  - `approved_prompt_pairs`: the APPROVED TRANSLATIONS prompt block feed
    (coherence aid only; the deterministic merge is the enforcement).
  - `segment_assist`: the editor's per-segment assist rail feed (TM exact +
    fuzzy + the stored machine rendering).

Phase 5 adds the provenance read surfaces (this module stays the ONLY one
with chapter_segments SQL):
  - `fetch_confirmed_exemplar_pairs`: recent confirmed pairs novel-wide for
    the APPROVED TRANSLATION EXAMPLES prompt block.
  - `corpus_for_consistency` / `chapter_ids_with_segments` /
    `search_segments`: the consistency rail and concordance now read
    chapter_segments first (status-carrying), with tm_segments demoted to a
    legacy fallback for chapters that have no segment rows.
  - `next_chapter_to_edit`: the editor's continue card feed.

Phase 6 (reader edit-mode retirement) makes the ledger the learn-from-edits
source: `edited_pairs_for_chapter` feeds learn_from_edits with
machine_text/target_text before-after pairs (style_edits' in-app producer,
the edit-paragraph endpoint, is gone), and `novel_segment_edit_stamp` gives
quality_dashboard its editor-write cache-bust signal.

Post-pivot gap audit (2026-08-04): `recent_edited_pairs` restores the USER
STYLE PREFERENCES prompt arm from the ledger, novel-wide edited|confirmed
before-after pairs replacing the severed style_edits in-app producer
(prompt_inputs.fetch_style_edits merges them ahead of the legacy rows).

Provenance gating (bug hunt 2026-08-04, B3+F1+F2+B14): `origin` carries real
provenance now, not just the producer tag. Values: 'human' (an update_segment
save wrote the text), 'llm' / 'llm_refined' / 'tm_exact' / 'aligned_backfill'
(machine producers), and 'reprojected' (a text-authoritative rebuild or
reproject CHANGED a human row's target out from under it: the text no longer
carries the user's endorsement, though status is preserved per the Phase 3
invariant). Style-pair feeds (`recent_edited_pairs`,
`edited_pairs_for_chapter`) require origin='human'; exemplar/endorsement
feeds keep status='confirmed' semantics (confirm-as-is IS endorsement) but
exclude origin='reprojected'. A later human save restores origin='human';
a single-row confirm restores it too (the user re-vouched with eyes on the
row); bulk confirm_all deliberately does not.

Async, aiosqlite. No route logic; callers own the commit.
"""

from __future__ import annotations

import hashlib
import logging
import re

import aiosqlite
from fastapi.concurrency import run_in_threadpool
from rapidfuzz import fuzz, process

from backend.services import tm as tm_svc
from backend.services.glossary_filters import canonical_zh
from backend.services.segmentation import (
    SEGMENTATION_VERSION,
    chapter_source_paragraphs,
    split_target_paragraphs,
)
from backend.services.translators.base import PROMPT_PAIR_SIDE_MAX_CHARS

logger = logging.getLogger(__name__)


class SegmentNotFoundError(Exception):
    """Chapter or segment row missing. Routes map this to 404."""


class SegmentActionError(Exception):
    """Invalid action for the segment's current state, unknown action name,
    or empty after_text. Routes map this to 400."""


class SegmentStaleError(Exception):
    """Write refused because the client's view is stale (or the chapter is
    not in an editable state). Routes map this to 409 with a structured
    detail {message, error_kind}; `kind` is one of 'stale_segment',
    'stale_chapter', 'chapter_translating'."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


_ACTIONS = frozenset(
    {"save", "confirm", "save_and_confirm", "unconfirm", "revert_machine"}
)

# Consecutive-newline runs inside a saved target. A segment is ONE paragraph:
# a pasted blank line would make its target span two paragraphs under
# split_target_paragraphs, desyncing the join==body paragraph count and
# firing drift on the next retranslate, so saves collapse runs to a single
# newline (paragraph-safe; single \n line breaks are preserved).
_BLANK_RUN_RE = re.compile(r"\n{2,}")


def hash16(text: str) -> str:
    """16-hex sha256 prefix, the same convention as tm.py's source_hash.

    Note the two stores hash DIFFERENT source units: chapter_segments hashes
    effective (pre-joined) source paragraphs while tm_segments hashes the
    raw blank-line split, so their source_hash values differ wherever the
    mid-sentence pre-join fired. Phase 4 assist lookups that bridge the two
    stores must account for that (hash-join only where no pre-join occurred,
    or re-hash on the effective split)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def displayed_body(row) -> tuple[str, str]:
    """(variant, text) for the body the reader displays.

    'refined' iff refined_text is non-empty, REGARDLESS of
    refinement_status (presence keying, 2026-07-31 retry-window fix):
    retained refined content stays displayed through a refinement retry
    window (status 'pending'/'in_progress') and after a failed retry
    ('error'), so confirmed segment work never vanishes from the reader and
    the store is never rebuilt against a mid-transition draft. First-ever
    refinements carry refined_text NULL until they commit, so the draft
    displays there; the translate success commit nulls refined_text, so a
    retranslate falls back to the draft too. Matches the FTS rule
    (COALESCE(refined_text, translated_text)) and the reader's
    `_displayedEnglish`; consistency.py imports this so the two stacks
    cannot drift. Text is "" (never None) when the chapter has no English.
    """
    if row["refined_text"]:
        return ("refined", row["refined_text"])
    return ("draft", row["translated_text"] or "")


def chapter_rev(text: str) -> str:
    """Revision token of a displayed body: 16-hex sha256. The editor sends it
    back on writes (Phase 3) as the stale-tab guard."""
    return hash16(text)


def _segments_match_body(targets: list[str], body: str) -> bool:
    """Self-heal check: do the stored targets still reproduce the displayed
    body's paragraph sequence?

    Compared against the normalized blank-line split of the body (not the raw
    column) so CRLF or extra blank lines in a stored body cannot force a
    rebuild on every read, and empty targets (the ""-target rows of a
    'partial' chapter) are skipped so partial chapters can pass too: every
    non-empty target came from the body's paragraph list in order, so the
    filtered join reproduces the normalized body exactly.
    """
    joined = "\n\n".join(t for t in targets if t)
    return joined == "\n\n".join(split_target_paragraphs(body))


async def _clear_and_stamp(
    conn: aiosqlite.Connection, chapter_id: int, state: str, rev: str
) -> None:
    await conn.execute(
        "DELETE FROM chapter_segments WHERE chapter_id = ?", (chapter_id,)
    )
    await conn.execute(
        "UPDATE chapters SET segments_state = ?, segmentation_version = ?, "
        "segments_rev = ? WHERE id = ?",
        (state, SEGMENTATION_VERSION, rev, chapter_id),
    )


async def _fetch_human_rows(
    conn: aiosqlite.Connection, chapter_id: int
) -> list[aiosqlite.Row]:
    cur = await conn.execute(
        "SELECT id, seg_index, source_hash, source_text, status, "
        "target_text, machine_text "
        "FROM chapter_segments "
        "WHERE chapter_id = ? AND status != 'machine' ORDER BY seg_index",
        (chapter_id,),
    )
    return list(await cur.fetchall())


def join_paragraphs(paragraphs: list[str]) -> str:
    """Displayed-body join: blank-line joined, empty entries skipped (the
    same empty-skip rule as `_materialize_body` / `_segments_match_body`,
    invariant I1)."""
    return "\n\n".join(p for p in paragraphs if p)


async def _retain_rows_stamp_unaligned(
    conn: aiosqlite.Connection, chapter_id: int, rev: str, reason: str
) -> str:
    """Alignment failed but the chapter has human rows (invariant I3: human
    rows die only by chapter/novel CASCADE). Keep EVERY row untouched and
    stamp the 'unaligned' verdict; the editor shows the retained rows
    read-only with a retranslate CTA, and the rev gate stops reads from
    re-running the aligner until the body changes again."""
    await conn.execute(
        "UPDATE chapters SET segments_state = 'unaligned', "
        "segmentation_version = ?, segments_rev = ? WHERE id = ?",
        (SEGMENTATION_VERSION, rev, chapter_id),
    )
    logger.info(
        "segments: chapter %d unalignable (%s); human rows retained",
        chapter_id, reason,
    )
    return "unaligned"


def _anchor_human_rows(
    human_rows: list, new_hashes: list[str], new_sources: list[str]
) -> dict[int, int] | None:
    """Map each human row id to its seg_index in the NEW source list.

    Anchor by source_hash AND full source_text equality (the 16-hex prefix
    alone could collide; prefill_confirmed_exact applies the same
    verification). When the source list is unchanged the row's own
    seg_index still carries the same hash, so position is the natural first
    candidate. Duplicate source paragraphs resolve to the nearest unclaimed
    index. Returns None when any human row cannot be anchored (its source
    paragraph vanished, or only a colliding hash matched): the caller must
    retain all rows instead of rebuilding, because a rebuild would have no
    slot for the human work.
    """
    used: set[int] = set()
    anchors: dict[int, int] = {}
    for r in human_rows:
        i = r["seg_index"]
        if (
            0 <= i < len(new_hashes)
            and new_hashes[i] == r["source_hash"]
            and new_sources[i] == r["source_text"]
            and i not in used
        ):
            anchors[r["id"]] = i
            used.add(i)
            continue
        candidates = [
            j for j, h in enumerate(new_hashes)
            if h == r["source_hash"]
            and new_sources[j] == r["source_text"]
            and j not in used
        ]
        if not candidates:
            return None
        j = min(candidates, key=lambda c: abs(c - i))
        anchors[r["id"]] = j
        used.add(j)
    return anchors


# Offset large enough that shifted seg_index values can never collide with a
# real index during the two-step re-key (UNIQUE(chapter_id, seg_index)).
_REKEY_OFFSET = 1_000_000


async def build_segments_from_alignment(
    conn: aiosqlite.Connection, chapter_row
) -> str:
    """(Re)build one chapter's segment rows from its committed text,
    PRESERVING human rows (the Phase 3 `_preserve_human_rows` seam, filled).

    Splits `effective_source_paragraphs(original_text)` against
    `split_target_paragraphs(displayed body)`. Equal counts map positionally
    (the 1:1 contract held: every row aligned=1). Unequal counts go through
    `tm.full_alignment_path`.

    Human rows (status edited|confirmed) are never deleted:
      - On a successful alignment they re-anchor by source_hash (their own
        seg_index is the first candidate, so an unchanged source list maps
        positionally); status / machine_text / edited_at / confirmed_at ride
        along. target_text takes the NEW body's paragraph for that slot:
        rebuilds are text-authoritative (the body already changed out of
        band), so the body wins on text and the row keeps its human status,
        exactly like `reproject_from_body`. When that slot's text CHANGES
        the row's target, origin is stamped 'reprojected' (B3): the text no
        longer carries the user's endorsement, and the provenance-gated
        feeds stop treating it as a human edit until a later save/confirm
        restores origin='human'.
      - When the alignment fails (below the <50% gate, empty split, a human
        row's source paragraph vanished, or the alignment left a human row's
        slot EMPTY, which would erase its text), every row is RETAINED
        untouched and the chapter is stamped 'unaligned' (rows kept). Only
        human-row-free chapters keep the Phase 2 zero-row behavior.

    All writes ride the caller's transaction. Returns the state written:
    'ok' | 'partial' | 'unaligned'.
    """
    chapter_id = chapter_row["id"]
    novel_id = chapter_row["novel_id"]
    _variant, body = displayed_body(chapter_row)
    rev = chapter_rev(body)
    # Canonical recipe (SEGMENTATION_VERSION 2): every writer derives the
    # source list through chapter_source_paragraphs so the lazy backfill and
    # the worker merges can never disagree on a source paragraph's text.
    src = chapter_source_paragraphs(chapter_row["original_text"] or "")
    tgt = split_target_paragraphs(body)
    human_rows = await _fetch_human_rows(conn, chapter_id)

    if not src or not tgt:
        if human_rows:
            return await _retain_rows_stamp_unaligned(
                conn, chapter_id, rev, "empty source or target split"
            )
        await _clear_and_stamp(conn, chapter_id, "unaligned", rev)
        return "unaligned"

    if len(src) == len(tgt):
        # The 1:1 contract held (or the counts happen to agree): position is
        # the key, every pair is a confident anchor.
        entries: list[tuple[str, bool]] = [(t, True) for t in tgt]
    else:
        path = tm_svc.full_alignment_path(src, tgt)
        if path is None:
            reason = (
                f"{len(src)} src vs {len(tgt)} tgt paragraphs below the "
                "confidence gate"
            )
            if human_rows:
                return await _retain_rows_stamp_unaligned(
                    conn, chapter_id, rev, reason
                )
            await _clear_and_stamp(conn, chapter_id, "unaligned", rev)
            logger.info("segments: chapter %d unalignable (%s)",
                        chapter_id, reason)
            return "unaligned"
        entries = path

    new_hashes = [hash16(s) for s in src]
    anchors = (
        _anchor_human_rows(human_rows, new_hashes, src) if human_rows else {}
    )
    if anchors is None:
        return await _retain_rows_stamp_unaligned(
            conn, chapter_id, rev, "human row's source paragraph vanished"
        )
    # The aligner legitimately hands back ("", False) slots (a merge2
    # follow-on row, a bare del). Writing one into an anchored human row
    # would ERASE the user's paragraph, and because join_paragraphs skips
    # empties the loss would be silent: the body still reproduces, but the
    # paragraph is gone from every body the store materializes afterwards.
    # Refusing the whole rebuild (rows kept, read-only until a retranslate)
    # is the deliberate conservative call: a partial rebuild is not worth
    # erasing user text.
    human_by_id = {r["id"]: r for r in human_rows}
    if any(
        human_by_id[row_id]["target_text"] and not entries[j][0]
        for row_id, j in anchors.items()
    ):
        return await _retain_rows_stamp_unaligned(
            conn, chapter_id, rev,
            "alignment produced no paragraph for an edited/confirmed row",
        )

    # B12 mirror: a DIVERGENT machine row (tm_exact prefill; machine_text
    # holds the AI's own rendering behind a prefilled target) is about to be
    # deleted and re-minted from the new body, which would destroy the
    # revert-to-AI anchor. Carry the divergent machine_text across the
    # rebuild keyed by source paragraph text (first row wins on duplicate
    # sources).
    cur = await conn.execute(
        "SELECT source_text, machine_text FROM chapter_segments "
        "WHERE chapter_id = ? AND status = 'machine' "
        "  AND machine_text IS NOT NULL AND machine_text != '' "
        "  AND machine_text != target_text "
        "ORDER BY seg_index",
        (chapter_id,),
    )
    divergent_machine: dict[str, str] = {}
    for dr in await cur.fetchall():
        divergent_machine.setdefault(dr["source_text"], dr["machine_text"])

    # Rebuild machine rows around the preserved human rows: delete only the
    # machine rows, shift the human rows out of the index space, then move
    # each onto its anchored slot with the new body's text for that slot.
    await conn.execute(
        "DELETE FROM chapter_segments "
        "WHERE chapter_id = ? AND status = 'machine'",
        (chapter_id,),
    )
    if anchors:
        await conn.execute(
            "UPDATE chapter_segments SET seg_index = seg_index + ? "
            "WHERE chapter_id = ?",
            (_REKEY_OFFSET, chapter_id),
        )
        for row_id, j in anchors.items():
            text, aligned = entries[j]
            # B3 (bug hunt 2026-08-04): a rebuild is text-authoritative, so
            # when the new body CHANGES this human row's text, the row's
            # (machine_text, target) delta is no longer a user edit and its
            # target no longer carries the user's endorsement. Stamp
            # origin='reprojected' (status untouched per the Phase 3
            # invariant); a later human save/confirm restores 'human'. The
            # CASE reads the PRE-update target_text, so an unchanged slot
            # keeps its origin.
            await conn.execute(
                "UPDATE chapter_segments SET seg_index = ?, source_text = ?, "
                "source_hash = ?, "
                "origin = CASE WHEN target_text != ? THEN 'reprojected' "
                "ELSE origin END, "
                "target_text = ?, aligned = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (j, src[j], new_hashes[j], text, text,
                 1 if aligned else 0, row_id),
            )
    human_indexes = set(anchors.values())
    await conn.executemany(
        "INSERT INTO chapter_segments "
        "(novel_id, chapter_id, seg_index, source_text, source_hash, "
        " target_text, machine_text, status, origin, aligned) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'machine', 'aligned_backfill', ?)",
        [
            (novel_id, chapter_id, i, src[i], new_hashes[i],
             text, divergent_machine.get(src[i], text), 1 if aligned else 0)
            for i, (text, aligned) in enumerate(entries)
            if i not in human_indexes
        ],
    )
    state = "ok" if all(aligned for _t, aligned in entries) else "partial"
    await conn.execute(
        "UPDATE chapters SET segments_state = ?, segmentation_version = ?, "
        "segments_rev = ? WHERE id = ?",
        (state, SEGMENTATION_VERSION, rev, chapter_id),
    )
    return state


_CHAPTER_SELECT = (
    "SELECT id, novel_id, chapter_num, title_zh, title_en, original_text, "
    "       translated_text, refined_text, refinement_status, status, "
    "       segments_state, segmentation_version, segments_rev "
    "FROM chapters WHERE novel_id = ? AND chapter_num = ?"
)


async def _fetch_segment_rows(
    conn: aiosqlite.Connection, chapter_id: int
) -> list[aiosqlite.Row]:
    cur = await conn.execute(
        "SELECT seg_index, source_text, source_hash, target_text, "
        "       machine_text, status, origin, aligned, confirmed_at "
        "FROM chapter_segments WHERE chapter_id = ? ORDER BY seg_index",
        (chapter_id,),
    )
    return list(await cur.fetchall())


def _payload(row, variant: str, body: str, state, seg_rows) -> dict:
    segments = [_segment_dict(r) for r in seg_rows]
    confirmed = sum(1 for s in segments if s["status"] == "confirmed")
    next_unconfirmed = next(
        (s["index"] for s in segments if s["status"] != "confirmed"), None
    )
    return {
        # Stable row id (chapter_num is mutable under a mid-novel insert);
        # the editor echoes it back on writes as the anti-renumber guard (B8).
        "chapter_id": row["id"],
        "chapter_num": row["chapter_num"],
        "title_en": row["title_en"],
        "title_zh": row["title_zh"],
        "chapter_status": row["status"],
        "variant": variant,
        "chapter_rev": chapter_rev(body) if body else None,
        "aligned": state == "ok",
        "segments_state": state,
        "progress": {"confirmed": confirmed, "total": len(segments)},
        "next_unconfirmed_index": next_unconfirmed,
        "segments": segments,
    }


async def get_segments(
    conn: aiosqlite.Connection, novel_id: int, chapter_num: int
) -> dict | None:
    """Segment payload for one chapter; None when the chapter row is missing
    (the route maps that to 404).

    Not-yet-translated chapters return a status-only payload with no writes.
    Translated chapters lazily backfill: rows are (re)built when absent, when
    their SEGMENTATION_VERSION is stale, or when the self-heal check finds
    the stored targets no longer reproduce the displayed body (an
    out-of-band edit: reader paragraph edit, find-replace, refinement
    landing). A persisted zero-row 'unaligned' verdict is trusted while
    `segments_rev` still matches the displayed body, so reads do not re-run
    the aligner until the body actually changes (a retranslate changes the
    rev, and the next read re-attempts). The caller owns the commit.
    """
    cur = await conn.execute(_CHAPTER_SELECT, (novel_id, chapter_num))
    row = await cur.fetchone()
    if row is None:
        return None

    variant, body = displayed_body(row)
    if row["status"] != "done" or not body.strip():
        # Pending / translating / error (or a done row with no text, which
        # should not happen): report state, write nothing.
        return _payload(row, variant, body, row["segments_state"], [])
    if (row["refinement_status"] or "none") in ("pending", "in_progress"):
        # Refinement mid-transition (initial pass or a retry): the refine
        # worker's merge commit is about to re-stamp the store, so a rebuild
        # here would be against a body with seconds to live, and a
        # text-authoritative rebuild mid-window could overwrite human rows.
        # Serve the stored rows exactly as they are; write nothing.
        seg_rows = await _fetch_segment_rows(conn, row["id"])
        return _payload(row, variant, body, row["segments_state"], seg_rows)

    seg_rows = await _fetch_segment_rows(conn, row["id"])
    state = row["segments_state"]
    verdict_stands = (
        state == "unaligned"
        and row["segmentation_version"] == SEGMENTATION_VERSION
        and row["segments_rev"] == chapter_rev(body)
    )
    if seg_rows:
        # An 'unaligned' chapter that RETAINED rows (human work preserved
        # through a failed re-alignment) deliberately does not satisfy the
        # join==body check, so it is rev-gated like the zero-row verdict:
        # rebuild only when the body has changed since the verdict.
        if state == "unaligned":
            needs_rebuild = not verdict_stands
        else:
            needs_rebuild = (
                row["segmentation_version"] != SEGMENTATION_VERSION
                or not _segments_match_body(
                    [r["target_text"] for r in seg_rows], body
                )
            )
    else:
        # Zero rows: never built, or a persisted 'unaligned' verdict. The
        # verdict stands only when THIS segmentation version made it against
        # THIS body; otherwise re-attempt the build.
        needs_rebuild = not verdict_stands
    if needs_rebuild:
        state = await build_segments_from_alignment(conn, row)
        # Refetch even for 'unaligned': the build may have RETAINED rows
        # (human-preservation path) and those must reach the editor.
        seg_rows = await _fetch_segment_rows(conn, row["id"])
    return _payload(row, variant, body, state, seg_rows)


# ---------------------------------------------------------------------------
# Phase 3: the single write path
# ---------------------------------------------------------------------------


def _segment_dict(r) -> dict:
    return {
        "index": r["seg_index"],
        "source_text": r["source_text"],
        "target_text": r["target_text"],
        "source_hash": r["source_hash"],
        "target_hash": hash16(r["target_text"]),
        "status": r["status"],
        "origin": r["origin"],
        "aligned": bool(r["aligned"]),
        "confirmed_at": r["confirmed_at"],
        # Cheap server-side diff flag: the stored AI rendering differs from
        # the displayed target. The editor keys the "kept" chip on it for
        # human rows (full machine_text ships via the assist endpoint, not
        # the list payload).
        "machine_differs": bool(r["machine_text"])
        and r["machine_text"] != r["target_text"],
    }


def _progress_of(seg_rows) -> tuple[dict, int | None]:
    confirmed = sum(1 for r in seg_rows if r["status"] == "confirmed")
    next_unconfirmed = next(
        (r["seg_index"] for r in seg_rows if r["status"] != "confirmed"), None
    )
    return {"confirmed": confirmed, "total": len(seg_rows)}, next_unconfirmed


async def _load_editable_chapter(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_num: int,
    client_rev: str,
    expected_chapter_id: int | None = None,
):
    """Shared write guards. Returns (chapter_row, variant, body).

    Raises SegmentNotFoundError (missing chapter), SegmentStaleError
    'chapter_translating' (not status='done' yet: pending, translating, or
    errored, or a refinement pass is mid-flight: the refine commit's merge
    would rematerialize the body from refined-derived targets and the next
    GET would text-authoritatively rebuild over the just-saved paragraph,
    so the write is refused rather than 200-ed and silently discarded; the
    same mid-transition window get_segments refuses to rebuild in),
    'stale_chapter' (client rev does not match the displayed body,
    the chapter is 'unaligned' so segment writes cannot rematerialize the
    body, or `expected_chapter_id` (the chapter row id the page loaded)
    no longer matches the row (novel_id, chapter_num) resolves to, i.e. a
    mid-novel insert renumbered the list (B8); the client recovery for all
    is the same re-GET).
    """
    cur = await conn.execute(_CHAPTER_SELECT, (novel_id, chapter_num))
    row = await cur.fetchone()
    if row is None:
        raise SegmentNotFoundError("chapter not found")
    if expected_chapter_id is not None and row["id"] != expected_chapter_id:
        raise SegmentStaleError(
            "stale_chapter",
            "the chapter list changed since the page loaded (chapters were "
            "renumbered). Reload the segments and retry.",
        )
    if row["status"] != "done":
        raise SegmentStaleError(
            "chapter_translating",
            f"chapter is not editable while its translation status is "
            f"'{row['status']}'. Wait for it to finish, then reload.",
        )
    if (row["refinement_status"] or "none") in ("pending", "in_progress"):
        # Same mid-transition window get_segments refuses to rebuild in: the
        # refine commit is about to re-stamp the store and the displayed
        # body, so a save landing now would be rematerialized away (a 200
        # the user's paragraph does not survive).
        raise SegmentStaleError(
            "chapter_translating",
            "chapter is being refined right now. Wait for the refinement "
            "to finish, then reload.",
        )
    variant, body = displayed_body(row)
    if not body.strip():
        raise SegmentStaleError(
            "stale_chapter",
            "chapter has no translated text. Reload the editor.",
        )
    if row["segments_state"] == "unaligned":
        raise SegmentStaleError(
            "stale_chapter",
            "paragraph alignment failed for this chapter; its segments are "
            "read only until it is retranslated.",
        )
    if client_rev != chapter_rev(body):
        raise SegmentStaleError(
            "stale_chapter",
            "this chapter changed since the page loaded. Reload the "
            "segments and retry.",
        )
    return row, variant, body


async def _materialize_body(
    conn: aiosqlite.Connection, chapter_row, variant: str
) -> str:
    """Regenerate the displayed body column from the segment targets (the
    empty-target skip matches `_segments_match_body`), stamp the new
    segments_rev, and recompute segments_state from the aligned flags.
    Same transaction as the segment write; FTS follows via the existing
    chapters triggers. Returns the new body."""
    seg_rows = await _fetch_segment_rows(conn, chapter_row["id"])
    new_body = "\n\n".join(r["target_text"] for r in seg_rows if r["target_text"])
    column = "refined_text" if variant == "refined" else "translated_text"
    state = "ok" if all(r["aligned"] for r in seg_rows) else "partial"
    # Column name is one of two hard-coded literals, never user input.
    await conn.execute(
        f"UPDATE chapters SET {column} = ?, segments_rev = ?, "
        "segments_state = ? WHERE id = ?",
        (new_body, chapter_rev(new_body), state, chapter_row["id"]),
    )
    return new_body


async def _refresh_state_only(
    conn: aiosqlite.Connection, chapter_id: int
) -> None:
    """Recompute segments_state from the aligned flags after a non-text
    write (confirm sets aligned=1, which can flip 'partial' to 'ok')."""
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM chapter_segments "
        "WHERE chapter_id = ? AND aligned = 0",
        (chapter_id,),
    )
    row = await cur.fetchone()
    state = "partial" if (row["n"] or 0) > 0 else "ok"
    await conn.execute(
        "UPDATE chapters SET segments_state = ? WHERE id = ?",
        (state, chapter_id),
    )


async def update_segment(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_num: int,
    seg_index: int,
    *,
    action: str,
    after_text: str | None,
    client_rev: str,
    before_target_hash: str,
    chapter_id: int | None = None,
) -> dict:
    """The segment state machine. Actions:

      - save:             write after_text, status -> 'edited' (a confirmed
                          row demotes), origin -> 'human', aligned -> 1.
      - confirm:          status -> 'confirmed', no text change. Idempotent
                          on an already-confirmed row. Confirm-as-is keeps a
                          machine origin (endorsement, not editing: the row
                          must not start minting style pairs), EXCEPT
                          origin='reprojected', which restores to 'human'
                          (the user re-vouched for the detached text with
                          eyes on the row). An already-confirmed reprojected
                          row is re-endorsed via unconfirm+confirm or a
                          save.
      - save_and_confirm: both in one write (status -> 'confirmed').
      - unconfirm:        confirmed -> 'edited' (other states are a 400).
      - revert_machine:   target_text := machine_text, status -> 'machine',
                          origin -> 'llm'. Allowed on edited|confirmed rows
                          and on machine rows whose target diverged from
                          machine_text (tm_exact prefill); 400 when
                          machine_text is NULL/empty or the target already
                          equals it.

    Guards (409 via SegmentStaleError): chapter must be status='done';
    `client_rev` must match the displayed body's current rev; the row's
    current target hash must match `before_target_hash`; `chapter_id`, when
    provided, must match the resolved chapter row (anti-renumber guard,
    B8). Empty after_text on a save is a 400 (a paragraph cannot be
    emptied; revert_machine is the escape hatch). Every row UPDATE re-checks
    its rowcount (B13): a row re-minted by a concurrent worker merge between
    this function's SELECT and its UPDATE surfaces as stale_segment instead
    of a false-success 200.

    After any text-changing action the displayed body column is regenerated
    from the targets in the SAME transaction (invariant I1); the caller owns
    the commit. Returns {segment, chapter_rev, segments_state, progress,
    next_unconfirmed_index}.
    """
    if action not in _ACTIONS:
        raise SegmentActionError(f"unknown action {action!r}")
    row, variant, body = await _load_editable_chapter(
        conn, novel_id, chapter_num, client_rev, chapter_id
    )
    cur = await conn.execute(
        "SELECT id, seg_index, source_text, source_hash, target_text, "
        "       machine_text, status, origin, aligned, edited_at, confirmed_at "
        "FROM chapter_segments WHERE chapter_id = ? AND seg_index = ?",
        (row["id"], seg_index),
    )
    seg = await cur.fetchone()
    if seg is None:
        raise SegmentNotFoundError("segment not found")
    if hash16(seg["target_text"]) != before_target_hash:
        raise SegmentStaleError(
            "stale_segment",
            "this segment changed since the page loaded. Reload the "
            "segments and retry.",
        )

    def _require_row_written(cur) -> None:
        # B13: the row can be deleted + re-minted (new id) by a concurrent
        # worker merge between this function's SELECT and its UPDATE; an
        # UPDATE-by-stale-id then writes 0 rows. Surfacing that as
        # stale_segment makes the client re-GET instead of trusting a 200
        # built from the regenerated row.
        if (cur.rowcount or 0) == 0:
            raise SegmentStaleError(
                "stale_segment",
                "this segment changed since the page loaded. Reload the "
                "segments and retry.",
            )

    status = seg["status"]
    text_changing = False
    if action in ("save", "save_and_confirm"):
        new_target = (
            (after_text or "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )
        new_target = _BLANK_RUN_RE.sub("\n", new_target)
        if not new_target:
            raise SegmentActionError(
                "a paragraph cannot be emptied. Use revert to AI instead."
            )
        new_status = "edited" if action == "save" else "confirmed"
        text_changing = True
        cur = await conn.execute(
            "UPDATE chapter_segments SET target_text = ?, status = ?, "
            "origin = 'human', aligned = 1, edited_at = datetime('now'), "
            "confirmed_at = CASE WHEN ? = 'confirmed' "
            "THEN datetime('now') ELSE NULL END, "
            "updated_at = datetime('now') WHERE id = ?",
            (new_target, new_status, new_status, seg["id"]),
        )
        _require_row_written(cur)
    elif action == "confirm":
        if not seg["target_text"]:
            raise SegmentActionError(
                "cannot confirm a segment with no paragraph. Write its "
                "text first."
            )
        if status != "confirmed":
            # Origin: confirm-as-is preserves machine origins (see the
            # docstring), but a 'reprojected' row restores to 'human': the
            # user looked at the detached text and vouched for it, so it
            # re-enters the endorsement feeds (B3).
            cur = await conn.execute(
                "UPDATE chapter_segments SET status = 'confirmed', "
                "aligned = 1, confirmed_at = datetime('now'), "
                "origin = CASE WHEN origin = 'reprojected' THEN 'human' "
                "ELSE origin END, "
                "updated_at = datetime('now') WHERE id = ?",
                (seg["id"],),
            )
            _require_row_written(cur)
    elif action == "unconfirm":
        if status != "confirmed":
            raise SegmentActionError(
                "only a confirmed segment can be unconfirmed."
            )
        cur = await conn.execute(
            "UPDATE chapter_segments SET status = 'edited', "
            "confirmed_at = NULL, updated_at = datetime('now') WHERE id = ?",
            (seg["id"],),
        )
        _require_row_written(cur)
    else:  # revert_machine
        if not seg["machine_text"]:
            # NULL or "" alike: an empty machine_text means the aligner had
            # no paragraph for this slot, so a revert would empty it (and on
            # a refined chapter whose only paragraph it was, flip the
            # displayed body back to the draft under a rev stamped against
            # ""). There is nothing to revert TO; refuse.
            raise SegmentActionError(
                "no AI translation is stored for this segment."
            )
        if status == "machine" and seg["target_text"] == seg["machine_text"]:
            raise SegmentActionError(
                "segment already shows the AI translation."
            )
        # Allowed on human rows AND on machine rows whose target diverged
        # from machine_text (a tm_exact prefill): the swap is the only way
        # to reach the fresh AI rendering behind a TM-prefilled row. Origin
        # becomes 'llm' (the worker merge is machine_text's producer; the
        # next merge re-stamps real provenance anyway).
        text_changing = True
        cur = await conn.execute(
            "UPDATE chapter_segments SET target_text = machine_text, "
            "status = 'machine', origin = 'llm', "
            "edited_at = NULL, confirmed_at = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (seg["id"],),
        )
        _require_row_written(cur)

    if text_changing:
        new_body = await _materialize_body(conn, row, variant)
    else:
        await _refresh_state_only(conn, row["id"])
        new_body = body

    seg_rows = await _fetch_segment_rows(conn, row["id"])
    updated = next(r for r in seg_rows if r["seg_index"] == seg_index)
    progress, next_unconfirmed = _progress_of(seg_rows)
    cur = await conn.execute(
        "SELECT segments_state FROM chapters WHERE id = ?", (row["id"],)
    )
    state_row = await cur.fetchone()
    return {
        "segment": _segment_dict(updated),
        "chapter_rev": hash16(new_body),
        "segments_state": state_row["segments_state"],
        "progress": progress,
        "next_unconfirmed_index": next_unconfirmed,
    }


async def confirm_all(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_num: int,
    *,
    client_rev: str,
    statuses: list[str] | None = None,
    chapter_id: int | None = None,
) -> dict:
    """Confirm every segment currently in one of `statuses` (default
    ['machine', 'edited']). Rev-guarded like update_segment (including the
    optional `chapter_id` anti-renumber guard, B8); empty-target
    rows are skipped (an unwritten paragraph cannot be confirmed). No text
    changes, so no rematerialization. Origin is deliberately untouched (F2):
    a bulk sweep is not per-row endorsement, so a tm_exact row it confirms
    keeps origin='tm_exact' (no style pair minted) and a 'reprojected' row
    stays excluded from exemplars until a per-row save/confirm restores
    'human'. Returns {confirmed, chapter_rev,
    segments_state, progress, next_unconfirmed_index}."""
    wanted = statuses if statuses is not None else ["machine", "edited"]
    if not wanted or any(s not in ("machine", "edited") for s in wanted):
        raise SegmentActionError(
            "statuses must be a non-empty subset of ['machine', 'edited']."
        )
    row, _variant, body = await _load_editable_chapter(
        conn, novel_id, chapter_num, client_rev, chapter_id
    )
    placeholders = ",".join("?" * len(wanted))
    cur = await conn.execute(
        f"UPDATE chapter_segments SET status = 'confirmed', aligned = 1, "
        f"confirmed_at = datetime('now'), updated_at = datetime('now') "
        f"WHERE chapter_id = ? AND status IN ({placeholders}) "
        f"AND target_text != ''",
        (row["id"], *wanted),
    )
    confirmed = cur.rowcount or 0
    await _refresh_state_only(conn, row["id"])
    seg_rows = await _fetch_segment_rows(conn, row["id"])
    progress, next_unconfirmed = _progress_of(seg_rows)
    cur = await conn.execute(
        "SELECT segments_state FROM chapters WHERE id = ?", (row["id"],)
    )
    state_row = await cur.fetchone()
    return {
        "confirmed": confirmed,
        "chapter_rev": client_rev,
        "segments_state": state_row["segments_state"],
        "progress": progress,
        "next_unconfirmed_index": next_unconfirmed,
    }


_CHAPTER_SELECT_BY_ID = _CHAPTER_SELECT.replace(
    "WHERE novel_id = ? AND chapter_num = ?", "WHERE id = ?"
)


async def reproject_from_body(
    conn: aiosqlite.Connection, chapter_id: int
) -> str | None:
    """Hook for text-authoritative mutators (find-replace commit, glossary
    apply-in-place, snapshot restore): re-sync the segment store to the
    chapter's new displayed body IN THE SAME TRANSACTION as the mutation
    (the caller owns the commit).

    Fast path: when the body's paragraph count matches the current non-empty
    row count positionally (and no stored target spans multiple paragraphs),
    target_text updates in place PRESERVING status: a novel-wide
    find-replace across confirmed segments must not un-confirm them. Human
    rows whose text changes are stamped origin='reprojected' (B3, see the
    module docstring): status survives, provenance does not.
    machine_text refreshes only for machine rows whose machine_text equaled
    the OLD target (non-divergent: for those the stored AI rendering IS
    the target, so it follows the text-authoritative rewrite); a DIVERGENT
    machine row (a tm_exact prefill: target = confirmed cross-chapter
    rendering, machine_text = the AI's own suggestion) keeps machine_text
    so revert-to-AI stays reachable (bug hunt 2026-08-04, B12). On count
    drift the preservation-aware `build_segments_from_alignment` takes over
    (which carries divergent machine_text across the rebuild the same way).

    No-ops (returns None) when the chapter has no segment rows yet: the lazy
    build on the next editor open sees the new body anyway. Returns the
    resulting segments_state otherwise.
    """
    cur = await conn.execute(_CHAPTER_SELECT_BY_ID, (chapter_id,))
    row = await cur.fetchone()
    if row is None or row["status"] != "done":
        return None
    _variant, body = displayed_body(row)
    if not body.strip():
        return None
    seg_rows = await _fetch_segment_rows(conn, chapter_id)
    if not seg_rows:
        return None
    if row["segments_state"] == "unaligned":
        # Retained-rows verdict: these rows have ZERO alignment confidence
        # for the current body (they were kept only so human work stays
        # visible), so a coincidental count match must never let the
        # positional fast path overwrite the preserved text. Always go
        # through the preservation-aware rebuild, which re-attempts the
        # alignment and retains the rows again if it still fails.
        return await build_segments_from_alignment(conn, row)
    paras = split_target_paragraphs(body)
    non_empty = [r for r in seg_rows if r["target_text"]]
    simple = all(
        len(split_target_paragraphs(r["target_text"])) <= 1 for r in non_empty
    )
    if simple and paras and len(paras) == len(non_empty):
        for r, new_text in zip(non_empty, paras):
            if r["target_text"] == new_text:
                continue
            # SET expressions read the PRE-update row, so the CASE compares
            # the OLD machine_text against the OLD target_text (B12). Human
            # rows whose text this rewrite CHANGES are stamped
            # origin='reprojected' (B3): the new target is
            # text-authoritative, not user-authored, so the provenance-gated
            # feeds must stop shipping it as an edit/endorsement until a
            # later save/confirm restores 'human'. (This loop only runs for
            # rows whose text actually changes.)
            await conn.execute(
                "UPDATE chapter_segments SET target_text = ?, "
                "machine_text = CASE WHEN status = 'machine' "
                "AND machine_text = target_text THEN ? "
                "ELSE machine_text END, "
                "origin = CASE WHEN status != 'machine' THEN 'reprojected' "
                "ELSE origin END, "
                "updated_at = datetime('now') "
                "WHERE chapter_id = ? AND seg_index = ?",
                (new_text, new_text, chapter_id, r["seg_index"]),
            )
        await conn.execute(
            "UPDATE chapters SET segments_rev = ? WHERE id = ?",
            (chapter_rev(body), chapter_id),
        )
        return row["segments_state"]
    return await build_segments_from_alignment(conn, row)


async def edited_pairs_for_chapter(
    conn: aiosqlite.Connection, chapter_id: int
) -> list[tuple[str, str]]:
    """(machine_text, target_text) before/after pairs for this chapter's
    human-touched segments: rows with status edited|confirmed whose stored AI
    rendering exists and differs from the human text. This is the ledger-backed
    successor to the style_edits capture (Phase 6): learn_from_edits derives
    its proposals from these pairs. Ordered by seg_index so proposal ids are
    stable across stage/commit re-derivations.

    Requires origin='human' (F2): a confirm-as-is over a tm_exact prefill
    (fresh AI machine_text behind a cross-chapter confirmed target) or a
    'reprojected' row (target detached from the user's writing by a
    text-authoritative rewrite) carries a machine-vs-machine delta, not a
    user edit, and must not teach as one."""
    cur = await conn.execute(
        "SELECT machine_text, target_text FROM chapter_segments "
        "WHERE chapter_id = ? AND status IN ('edited', 'confirmed') "
        "AND origin = 'human' "
        "AND machine_text IS NOT NULL AND machine_text != '' "
        "AND machine_text != target_text "
        "ORDER BY seg_index",
        (chapter_id,),
    )
    return [(r["machine_text"], r["target_text"]) for r in await cur.fetchall()]


async def novel_segment_counts(
    conn: aiosqlite.Connection, novel_id: int
) -> tuple[int, int]:
    """(total, human) chapter_segments counts for one novel, where human is
    the status != 'machine' subset (edited|confirmed): the rows carrying
    user work. Feeds the archive/purge quantified-confirm dialog
    (soft_delete.delete_counts, bug hunt 2026-08-04 B4); lives here because
    this module is the single chapter_segments SQL owner."""
    cur = await conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status != 'machine' THEN 1 ELSE 0 END) AS human "
        "FROM chapter_segments WHERE novel_id = ?",
        (novel_id,),
    )
    r = await cur.fetchone()
    return int(r["total"] or 0), int(r["human"] or 0)


async def novel_segment_edit_stamp(conn: aiosqlite.Connection, novel_id: int) -> str:
    """Cheap change stamp over this novel's segment rows for cache version
    tokens (quality_dashboard): every editor write (save / confirm /
    unconfirm / revert / reproject) bumps chapter_segments.updated_at, so
    MAX(updated_at)|COUNT changes whenever the editor touched the novel.
    Replaces the style_edits aggregate that tracked the retired
    edit-paragraph endpoint."""
    cur = await conn.execute(
        "SELECT MAX(updated_at) AS mu, COUNT(*) AS n "
        "FROM chapter_segments WHERE novel_id = ?",
        (novel_id,),
    )
    r = await cur.fetchone()
    return f"{r['mu']}|{r['n']}"


# ---------------------------------------------------------------------------
# Phase 4: worker merge, exact-hash prefill, approved-pairs feed, assist rail
# ---------------------------------------------------------------------------

_MACHINE_KINDS = frozenset({"llm", "llm_refined"})


async def _retain_all_rows_unaligned(
    conn: aiosqlite.Connection,
    chapter_id: int,
    new_paragraphs: list[str],
    reason: str,
) -> list[str]:
    """Merge fallback when the new machine output cannot be reconciled with
    the existing rows (alignment below the gate, or a human row's source
    paragraph vanished). Every row is RETAINED (invariant I3) and demoted to
    aligned=0 (zero confidence against the new body); the chapter is stamped
    'unaligned' so the editor shows the preserved rows read-only with a
    retranslate CTA. The machine output still becomes the displayed body:
    the human text survives in the STORE, recoverable, but cannot be
    positioned in the new body."""
    rev = chapter_rev(join_paragraphs(new_paragraphs))
    await _retain_rows_stamp_unaligned(conn, chapter_id, rev, reason)
    await conn.execute(
        "UPDATE chapter_segments SET aligned = 0, "
        "updated_at = datetime('now') WHERE chapter_id = ?",
        (chapter_id,),
    )
    return new_paragraphs


async def apply_machine_translation(
    conn: aiosqlite.Connection,
    *,
    novel_id: int,
    chapter_id: int,
    new_paragraphs: list[str],
    kind: str,
    src_paras: list[str],
    prefill: dict[int, str] | None = None,
) -> list[str]:
    """The worker-commit merge (translate kind='llm', refine
    kind='llm_refined'): reconcile the new machine paragraphs with the
    existing segment store, PRESERVING human rows, and return the merged
    paragraph list the caller commits as the displayed body.

    Per seg index (keyed to `src_paras`, the effective source paragraphs the
    1:1 pipeline fed the model):

      - Human rows (edited|confirmed): target_text kept VERBATIM; only
        machine_text refreshes with the new AI rendering for that index (so
        the editor can show "the AI suggests differently"); status / origin /
        timestamps untouched (invariant I2). Re-anchored by source_hash via
        `_anchor_human_rows` (position-first, so an unchanged source maps
        positionally). The alignment entry's confidence is honored (F1+B14):
        machine_text refreshes only from a confident slot (else the prior
        rendering is kept), the aligned column takes the entry's flag, and
        an unconfident slot demotes segments_state to 'partial' like any
        machine row would. A human row whose stored target is EMPTY (only
        the pre-fix rebuild produced one) is healed from the slot's fresh
        machine text, origin='reprojected', rather than dropping the
        paragraph out of the merged body.
      - Machine rows: regenerated from the new text (target_text +
        machine_text), origin=`kind`; missing rows insert; a chapter with no
        store at all gets a fresh full build.
      - `prefill` (index -> confirmed rendering from `prefill_confirmed_exact`)
        overrides machine rows only: target_text takes the confirmed
        rendering, origin='tm_exact', status stays 'machine', machine_text
        keeps the fresh AI rendering.

    Mapping: equal counts map positionally; on drift (a fixup welded or
    dropped a paragraph) `tm.full_alignment_path` aligns the MACHINE side,
    demoting uncertain rows to aligned=0. When the alignment fails outright,
    or a human row's source paragraph vanished (stored rows predating
    SEGMENTATION_VERSION, or a source edit), every row is RETAINED and the
    chapter stamps 'unaligned' (see `_retain_all_rows_unaligned`); the
    machine paragraphs are returned unmerged.

    All writes ride the caller's transaction (the worker's success-commit
    transaction, so human rows are re-read AFTER the claim and editor writes
    cannot interleave). Stamps segments_state / segmentation_version /
    segments_rev against the merged body. The caller must commit
    `join_paragraphs(return value)` as the displayed body (invariant I1).
    """
    if kind not in _MACHINE_KINDS:
        raise ValueError(f"unknown machine translation kind {kind!r}")
    human_rows = await _fetch_human_rows(conn, chapter_id)

    if not src_paras or not new_paragraphs:
        if human_rows:
            return await _retain_all_rows_unaligned(
                conn, chapter_id, new_paragraphs,
                "empty source or machine paragraph list",
            )
        await _clear_and_stamp(
            conn, chapter_id, "unaligned",
            chapter_rev(join_paragraphs(new_paragraphs)),
        )
        return new_paragraphs

    if len(new_paragraphs) == len(src_paras):
        entries: list[tuple[str, bool]] = [(t, True) for t in new_paragraphs]
    else:
        path = tm_svc.full_alignment_path(src_paras, new_paragraphs)
        if path is None:
            reason = (
                f"{len(src_paras)} src vs {len(new_paragraphs)} machine "
                "paragraphs below the confidence gate"
            )
            if human_rows:
                return await _retain_all_rows_unaligned(
                    conn, chapter_id, new_paragraphs, reason
                )
            await _clear_and_stamp(
                conn, chapter_id, "unaligned",
                chapter_rev(join_paragraphs(new_paragraphs)),
            )
            logger.info("segments: chapter %d merge unalignable (%s)",
                        chapter_id, reason)
            return new_paragraphs
        entries = path

    new_hashes = [hash16(s) for s in src_paras]
    anchors = (
        _anchor_human_rows(human_rows, new_hashes, src_paras)
        if human_rows else {}
    )
    if anchors is None:
        return await _retain_all_rows_unaligned(
            conn, chapter_id, new_paragraphs,
            "human row's source paragraph vanished",
        )

    merged: list[str] = [t for t, _a in entries]
    aligned_flags: list[bool] = [a for _t, a in entries]
    by_id = {r["id"]: r for r in human_rows}
    prefill = prefill or {}

    # Same two-step re-key as build_segments_from_alignment: drop the machine
    # rows, shift the human rows out of the index space, then land each on
    # its anchored slot.
    await conn.execute(
        "DELETE FROM chapter_segments "
        "WHERE chapter_id = ? AND status = 'machine'",
        (chapter_id,),
    )
    if anchors:
        await conn.execute(
            "UPDATE chapter_segments SET seg_index = seg_index + ? "
            "WHERE chapter_id = ?",
            (_REKEY_OFFSET, chapter_id),
        )
        for row_id, j in anchors.items():
            row = by_id[row_id]
            new_machine, entry_aligned = entries[j]
            # F1+B14 (bug hunt 2026-08-04): honor the alignment entry's
            # confidence for human rows exactly as the machine-row path
            # does. machine_text refreshes ONLY from a confident slot: an
            # unconfident slot's paragraph may render a different source
            # paragraph, and a wrong refresh ships novel-wide as a poisoned
            # (machine_text, target) style pair. Otherwise (unconfident
            # slot, or the aligner had no paragraph for it) the prior
            # stored rendering is kept so revert-to-AI stays meaningful.
            if entry_aligned and new_machine:
                machine_text = new_machine
            else:
                machine_text = row["machine_text"] or ""
            # An EMPTY stored human target is pathological: only the
            # pre-fix rebuild (an aligner "" slot written over an anchored
            # human row) produced one on live DBs. Keeping it verbatim would
            # drop the paragraph from the merged body for good, because
            # join_paragraphs skips empties, so heal the row from this
            # slot's fresh machine text instead. Origin becomes
            # 'reprojected' (the text is not user-authored); status is
            # untouched, so the row stays visible as human work. When the
            # slot is empty too there is nothing to heal with and the row
            # stays as it is.
            heal = not row["target_text"] and bool(new_machine)
            new_target = row["target_text"] or new_machine
            # One of two hard-coded SQL tokens, never user input: 'origin'
            # keeps the stored provenance on the (normal) verbatim path,
            # where new_target is the row's own unchanged target_text.
            origin_expr = "'reprojected'" if heal else "origin"
            await conn.execute(
                "UPDATE chapter_segments SET seg_index = ?, source_text = ?, "
                "source_hash = ?, machine_text = ?, target_text = ?, "
                f"origin = {origin_expr}, aligned = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (j, src_paras[j], new_hashes[j], machine_text, new_target,
                 1 if entry_aligned else 0, row_id),
            )
            merged[j] = new_target
            # State computation no longer force-treats human rows as
            # aligned (B14): an unconfident slot demotes the chapter to
            # 'partial' even when a human row occupies it.
            aligned_flags[j] = entry_aligned
    human_indexes = set(anchors.values())

    inserts: list[tuple] = []
    for i, (text, aligned) in enumerate(entries):
        if i in human_indexes:
            continue
        pre = prefill.get(i)
        if pre:
            inserts.append(
                (novel_id, chapter_id, i, src_paras[i], new_hashes[i],
                 pre, text, "tm_exact", 1)
            )
            merged[i] = pre
            aligned_flags[i] = True
        else:
            inserts.append(
                (novel_id, chapter_id, i, src_paras[i], new_hashes[i],
                 text, text, kind, 1 if aligned else 0)
            )
    await conn.executemany(
        "INSERT INTO chapter_segments "
        "(novel_id, chapter_id, seg_index, source_text, source_hash, "
        " target_text, machine_text, status, origin, aligned) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'machine', ?, ?)",
        inserts,
    )
    state = "ok" if all(aligned_flags) else "partial"
    await conn.execute(
        "UPDATE chapters SET segments_state = ?, segmentation_version = ?, "
        "segments_rev = ? WHERE id = ?",
        (state, SEGMENTATION_VERSION, chapter_rev(join_paragraphs(merged)),
         chapter_id),
    )
    return merged


# IN-clause chunk size (SQLite parameter cap safety; matches queue.py).
_PREFILL_CHUNK = 500

# B7 (bug hunt 2026-08-04): chapters stamped segments_state='unaligned'
# RETAIN their rows so human work stays visible in the editor, but those
# targets correspond to no current chapter text (zero positional
# confidence). Every cross-surface read feed (prefill, prompt pairs,
# exemplars, style pairs, consistency corpus, concordance) excludes rows
# from such chapters via this shared predicate; the editor's own GET keeps
# returning them for display. Requires the chapter_segments table to be
# aliased `cs` in the enclosing query.
_EXCLUDE_UNALIGNED_SQL = (
    "NOT EXISTS (SELECT 1 FROM chapters uc "
    "WHERE uc.id = cs.chapter_id AND uc.segments_state = 'unaligned')"
)


async def prefill_confirmed_exact(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_id: int,
    src_paras: list[str],
) -> dict[int, str]:
    """index -> confirmed rendering for every src paragraph that some OTHER
    chapter of this novel has already CONFIRMED with the exact same source
    text. One indexed query on idx_chapter_segments_novel_hash; the 16-hex
    prefix is verified against full source_text equality so a hash collision
    cannot smuggle in a wrong rendering. Most-recently-confirmed wins.

    NOTE: chapter_segments hashes pre-joined EFFECTIVE paragraphs (see the
    hash16 docstring on the tm_segments divergence), so `src_paras` must be
    the effective_source_paragraphs list, hashed with the same convention.
    Rows from 'unaligned' chapters are excluded (B7): their confirmed
    targets are detached from any current chapter text.
    """
    if not src_paras:
        return {}
    unique_hashes = list({hash16(p) for p in src_paras})
    rows: list = []
    for i in range(0, len(unique_hashes), _PREFILL_CHUNK):
        chunk = unique_hashes[i : i + _PREFILL_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        cur = await conn.execute(
            f"SELECT cs.id, cs.source_hash, cs.source_text, cs.target_text, "
            f"cs.confirmed_at "
            f"FROM chapter_segments cs "
            f"WHERE cs.novel_id = ? AND cs.chapter_id != ? "
            f"  AND cs.status = 'confirmed' "
            f"  AND cs.target_text != '' AND cs.source_hash IN ({placeholders}) "
            f"  AND {_EXCLUDE_UNALIGNED_SQL}",
            [novel_id, chapter_id, *chunk],
        )
        rows.extend(await cur.fetchall())
    if not rows:
        return {}
    # Most-recently-confirmed first; id breaks ties deterministically.
    rows.sort(key=lambda r: ((r["confirmed_at"] or ""), r["id"]), reverse=True)
    by_source: dict[str, str] = {}
    for r in rows:
        if r["source_text"] not in by_source:
            by_source[r["source_text"]] = r["target_text"]
    return {
        i: by_source[p] for i, p in enumerate(src_paras) if p in by_source
    }


async def approved_prompt_pairs(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_id: int,
    src_paras: list[str],
    *,
    max_pairs: int = 30,
    max_chars: int = 8000,
) -> list[tuple[int, str, str]]:
    """(seg_index, source, approved_en) pairs for the APPROVED TRANSLATIONS
    prompt block: this chapter's human rows (anchored to `src_paras` by
    source_hash, position-first) plus cross-chapter exact confirmed matches
    at the remaining indexes. Sorted by index, capped at `max_pairs` entries
    and ~`max_chars` total characters.

    Coherence aid only: the deterministic `apply_machine_translation` merge
    is the enforcement, so a row this skips (stale, over-cap) still survives
    the retranslate verbatim. An 'unaligned' chapter contributes no own
    rows (B7: retained rows have zero positional confidence; the merge
    still re-anchors and lands them verbatim when it can), and the
    cross-chapter half inherits prefill_confirmed_exact's exclusion.
    """
    if not src_paras:
        return []
    new_hashes = [hash16(p) for p in src_paras]
    pairs: dict[int, str] = {}
    used: set[int] = set()
    cur = await conn.execute(
        f"SELECT cs.seg_index, cs.source_hash, cs.target_text "
        f"FROM chapter_segments cs "
        f"WHERE cs.chapter_id = ? AND cs.status != 'machine' "
        f"  AND cs.target_text != '' "
        f"  AND {_EXCLUDE_UNALIGNED_SQL} "
        f"ORDER BY cs.seg_index",
        (chapter_id,),
    )
    for r in await cur.fetchall():
        i = r["seg_index"]
        if (
            0 <= i < len(new_hashes)
            and new_hashes[i] == r["source_hash"]
            and i not in used
        ):
            j = i
        else:
            candidates = [
                k for k, h in enumerate(new_hashes)
                if h == r["source_hash"] and k not in used
            ]
            if not candidates:
                continue  # stale row; the merge owns retention, not the prompt
            j = min(candidates, key=lambda c: abs(c - i))
        used.add(j)
        pairs[j] = r["target_text"]
    exact = await prefill_confirmed_exact(conn, novel_id, chapter_id, src_paras)
    for i, text in exact.items():
        pairs.setdefault(i, text)
    out: list[tuple[int, str, str]] = []
    total = 0
    for i in sorted(pairs):
        entry_chars = len(src_paras[i]) + len(pairs[i])
        if len(out) >= max_pairs or total + entry_chars > max_chars:
            break
        out.append((i, src_paras[i], pairs[i]))
        total += entry_chars
    return out


# ---------------------------------------------------------------------------
# Phase 5: feed-the-AI exemplars + provenance-aware TM read surfaces
# ---------------------------------------------------------------------------

# Per-side truncation for a confirmed exemplar / edited pair, tied to the
# render-time bound of format_style_edits / format_confirmed_exemplars via
# the shared constant (enough to convey voice, bounded prompt, desync-proof).
_EXEMPLAR_SIDE_MAX_CHARS = PROMPT_PAIR_SIDE_MAX_CHARS
# Candidate-pool multiplier: the recency query over-fetches so the source
# dedupe below can still fill `limit` distinct pairs when the most recent
# confirmations repeat a source paragraph.
_EXEMPLAR_POOL_FACTOR = 20


async def fetch_confirmed_exemplar_pairs(
    conn: aiosqlite.Connection,
    novel_id: int,
    exclude_chapter_id: int,
    limit: int,
) -> list[tuple[str, str]]:
    """(source_text, target_text) pairs of the most recently CONFIRMED
    segments novel-wide, excluding the chapter being translated, for the
    APPROVED TRANSLATION EXAMPLES prompt block (CAT Phase 5).

    Recency-selected (ORDER BY confirmed_at DESC; teaches the user's voice,
    not chapter-relevant vocabulary; no relevance filter by design), deduped
    by source text (newest confirmation of a repeated source wins), empty
    targets skipped, both sides truncated to ~400 chars. 'Unaligned'
    chapters' retained rows are excluded (B7). Confirmed-as-is machine rows
    STAY eligible (confirm is endorsement regardless of who wrote the text),
    but origin='reprojected' rows are excluded (F2/B3): their target was
    swapped by a text-authoritative rewrite AFTER the confirm, so the stored
    confirmation no longer covers this text. Read-only."""
    if limit <= 0:
        return []
    cur = await conn.execute(
        f"SELECT cs.source_text, cs.target_text FROM chapter_segments cs "
        f"WHERE cs.novel_id = ? AND cs.chapter_id != ? "
        f"  AND cs.status = 'confirmed' "
        f"  AND cs.origin != 'reprojected' "
        f"  AND cs.target_text != '' "
        f"  AND {_EXCLUDE_UNALIGNED_SQL} "
        f"ORDER BY cs.confirmed_at DESC, cs.id DESC LIMIT ?",
        (novel_id, exclude_chapter_id, limit * _EXEMPLAR_POOL_FACTOR),
    )
    rows = await cur.fetchall()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for r in rows:
        src = (r["source_text"] or "").strip()
        tgt = (r["target_text"] or "").strip()
        if not src or not tgt or src in seen:
            continue
        seen.add(src)
        out.append((
            src[:_EXEMPLAR_SIDE_MAX_CHARS],
            tgt[:_EXEMPLAR_SIDE_MAX_CHARS],
        ))
        if len(out) >= limit:
            break
    return out


async def recent_edited_pairs(
    conn: aiosqlite.Connection,
    novel_id: int,
    exclude_chapter_id: int,
    limit: int,
) -> list[tuple[str, str]]:
    """(machine_text, target_text) before-after pairs of the most recently
    human-edited segments novel-wide, excluding the chapter being translated,
    for the USER STYLE PREFERENCES prompt block (post-pivot restoration of
    the style_edits arm: the ledger's edited|confirmed rows ARE the in-app
    paragraph edits now).

    Qualifying rows: status edited|confirmed AND origin='human' (the same
    provenance gate as `edited_pairs_for_chapter`, F2: tm_exact confirm-as-is
    and 'reprojected' rows carry machine-vs-machine deltas, not user edits)
    with a non-empty stored AI rendering that differs from the human text and
    a non-empty target. Recency-selected by
    COALESCE(edited_at, confirmed_at) DESC (id DESC tiebreak), deduped by
    the machine (before) side so a repeated correction teaches once, both
    sides truncated to the exemplar ~400-char convention. Over-fetches by
    `_EXEMPLAR_POOL_FACTOR` so the dedupe can still fill `limit`.
    'Unaligned' chapters' retained rows are excluded (B7). Read-only."""
    if limit <= 0:
        return []
    cur = await conn.execute(
        f"SELECT cs.machine_text, cs.target_text FROM chapter_segments cs "
        f"WHERE cs.novel_id = ? AND cs.chapter_id != ? "
        f"  AND cs.status IN ('edited', 'confirmed') "
        f"  AND cs.origin = 'human' "
        f"  AND cs.machine_text IS NOT NULL AND cs.machine_text != '' "
        f"  AND cs.machine_text != cs.target_text AND cs.target_text != '' "
        f"  AND {_EXCLUDE_UNALIGNED_SQL} "
        f"ORDER BY COALESCE(cs.edited_at, cs.confirmed_at) DESC, cs.id DESC "
        f"LIMIT ?",
        (novel_id, exclude_chapter_id, limit * _EXEMPLAR_POOL_FACTOR),
    )
    rows = await cur.fetchall()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for r in rows:
        before = (r["machine_text"] or "").strip()
        after = (r["target_text"] or "").strip()
        if not before or not after or before in seen:
            continue
        seen.add(before)
        out.append((
            before[:_EXEMPLAR_SIDE_MAX_CHARS],
            after[:_EXEMPLAR_SIDE_MAX_CHARS],
        ))
        if len(out) >= limit:
            break
    return out


async def chapter_ids_with_segments(
    conn: aiosqlite.Connection, novel_id: int
) -> set[int]:
    """Every chapter id of this novel that has ANY chapter_segments rows.

    The provenance-aware TM surfaces (consistency rail corpus, concordance)
    treat these chapters as segment-covered: their tm_segments rows are
    legacy duplicates and must not re-enter through the fallback path, even
    when a covered chapter contributes no usable segment row (all-empty
    targets on a 'partial' chapter)."""
    cur = await conn.execute(
        "SELECT DISTINCT chapter_id FROM chapter_segments WHERE novel_id = ?",
        (novel_id,),
    )
    return {r["chapter_id"] for r in await cur.fetchall()}


async def corpus_for_consistency(
    conn: aiosqlite.Connection,
    novel_id: int,
    exclude_chapter_id: int,
) -> list[aiosqlite.Row]:
    """Rows for the consistency rail's fuzzy corpus: same-novel OTHER-chapter
    segments with a non-empty target, carrying status so the payload can
    rank confirmed > edited > machine and the UI can badge provenance.
    Columns: source_text, target_text, status, chapter_num. 'Unaligned'
    chapters' retained rows are excluded (B7). Read-only;
    consistency.py owns the folding/scoring on top."""
    cur = await conn.execute(
        f"SELECT cs.source_text, cs.target_text, cs.status, c.chapter_num "
        f"FROM chapter_segments cs JOIN chapters c ON c.id = cs.chapter_id "
        f"WHERE cs.novel_id = ? AND cs.chapter_id != ? "
        f"  AND cs.target_text != '' "
        f"  AND {_EXCLUDE_UNALIGNED_SQL}",
        (novel_id, exclude_chapter_id),
    )
    return list(await cur.fetchall())


# Concordance semantics mirrored from tm.search (INSTR for the Chinese side,
# case-insensitive LIKE for the English side, same 50-hit cap).
_SEARCH_LIMIT = 50


async def search_segments(
    conn: aiosqlite.Connection,
    novel_id: int,
    query: str,
    search_sides: tuple[str, ...] = ("source", "target"),
) -> list[dict]:
    """Concordance search over chapter_segments, mirroring tm.search's
    semantics (verbatim INSTR on the Chinese source, case-insensitive LIKE
    on the English target, capped at 50 hits in reading order). Hits carry
    `status` so the dialogs can render a provenance chip; `paragraph_index`
    is the seg_index (the editor's row key and, for 1:1 chapters, the
    reader's display-paragraph index)."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    conditions: list[str] = []
    params: list = [novel_id]
    if "source" in search_sides:
        conditions.append("INSTR(cs.source_text, ?) > 0")
        params.append(q)
    if "target" in search_sides:
        conditions.append("LOWER(cs.target_text) LIKE LOWER(?)")
        params.append(f"%{q}%")
    if not conditions:
        return []
    sql = (
        "SELECT cs.chapter_id, c.chapter_num, c.title_en, cs.seg_index, "
        "       cs.source_text, cs.target_text, cs.status, "
        "       CASE WHEN INSTR(cs.source_text, ?) > 0 THEN 'source' "
        "            ELSE 'target' END AS matched_side "
        "FROM chapter_segments cs "
        "JOIN chapters c ON c.id = cs.chapter_id "
        f"WHERE cs.novel_id = ? AND cs.target_text != '' "
        f"  AND ({' OR '.join(conditions)}) "
        f"  AND {_EXCLUDE_UNALIGNED_SQL} "
        "ORDER BY c.chapter_num, cs.seg_index "
        "LIMIT ?"
    )
    bound = [q, novel_id] + params[1:] + [_SEARCH_LIMIT]
    cur = await conn.execute(sql, bound)
    return [
        {
            "chapter_id": r["chapter_id"],
            "chapter_num": r["chapter_num"],
            "chapter_title_en": r["title_en"],
            "paragraph_index": r["seg_index"],
            "source_text": r["source_text"],
            "target_text": r["target_text"],
            "matched_side": r["matched_side"],
            "status": r["status"],
        }
        for r in await cur.fetchall()
    ]


async def concordance_search(
    conn: aiosqlite.Connection,
    novel_id: int,
    query: str,
    search_sides: tuple[str, ...] = ("source", "target"),
) -> list[dict]:
    """Provenance-aware concordance (CAT Phase 5): chapter_segments hits
    first (status-carrying, always current), then legacy tm_segments hits
    ONLY for chapters that have no segment rows at all (their status is
    None). Deduped by (chapter, paragraph), merged in reading order, capped
    at the shared 50-hit limit. Lives here rather than tm.py because this
    module is the single chapter_segments owner and already imports tm."""
    seg_hits = await search_segments(conn, novel_id, query, search_sides)
    covered = await chapter_ids_with_segments(conn, novel_id)
    tm_hits = await tm_svc.search(conn, novel_id, query, search_sides)
    merged = list(seg_hits)
    seen = {(h["chapter_id"], h["paragraph_index"]) for h in seg_hits}
    for h in tm_hits:
        if h.chapter_id in covered:
            continue
        key = (h.chapter_id, h.paragraph_index)
        if key in seen:
            continue
        seen.add(key)
        merged.append({
            "chapter_id": h.chapter_id,
            "chapter_num": h.chapter_num,
            "chapter_title_en": h.chapter_title_en,
            "paragraph_index": h.paragraph_index,
            "source_text": h.source_text,
            "target_text": h.target_text,
            "matched_side": h.matched_side,
            "status": None,
        })
    merged.sort(key=lambda h: (h["chapter_num"], h["paragraph_index"]))
    return merged[:_SEARCH_LIMIT]


async def next_chapter_to_edit(
    conn: aiosqlite.Connection,
    novel_id: int,
    after_chapter_num: int,
) -> int | None:
    """The editor's continue card: the next chapter that still needs work,
    meaning it is not yet translated (status != 'done') OR its segment store
    is missing, is stamped 'unaligned' (B7: retained rows are detached from
    the chapter text, so even an all-confirmed retained set needs attention)
    or carries any non-confirmed row. Searches forward from
    `after_chapter_num`, then wraps to the beginning; a fully confirmed
    chapter (done + aligned store + every segment confirmed) is skipped.
    None when every chapter of the novel is fully confirmed."""
    needs_work = (
        "NOT ("
        "  ch.status = 'done'"
        "  AND COALESCE(ch.segments_state, '') != 'unaligned'"
        "  AND EXISTS (SELECT 1 FROM chapter_segments s "
        "              WHERE s.chapter_id = ch.id)"
        "  AND NOT EXISTS (SELECT 1 FROM chapter_segments s "
        "                  WHERE s.chapter_id = ch.id "
        "                    AND s.status != 'confirmed')"
        ")"
    )
    for clause, args in (
        ("ch.chapter_num > ?", (novel_id, after_chapter_num)),
        ("ch.chapter_num < ?", (novel_id, after_chapter_num)),
    ):
        cur = await conn.execute(
            f"SELECT ch.chapter_num FROM chapters ch "
            f"WHERE ch.novel_id = ? AND {clause} AND {needs_work} "
            f"ORDER BY ch.chapter_num LIMIT 1",
            args,
        )
        row = await cur.fetchone()
        if row is not None:
            return row["chapter_num"]
    return None


# Assist-rail knobs. Same conventions as services/consistency.py's edit-mode
# rail (canonical_zh folding, length-band prefilter, rapidfuzz ratio), with a
# looser 0.80 threshold: the rail suggests, the consistency rail flags drift.
_ASSIST_CAP = 5
_ASSIST_FUZZY_THRESHOLD = 0.80
_ASSIST_MIN_SOURCE_LEN = 6
_ASSIST_MAX_CANDIDATES = 100
_STATUS_RANK = {"confirmed": 0, "edited": 1, "machine": 2}


async def segment_assist(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_num: int,
    seg_index: int,
) -> dict | None:
    """Assist-rail feed for one segment: exact TM matches (same-novel
    chapter_segments rows sharing this source_hash, full-text verified,
    confirmed first), fuzzy TM matches (rapidfuzz over other chapters'
    segment sources, threshold 0.80, tiny sources skipped), and the row's
    stored machine rendering (the "AI suggests" dialog body). Read-only.
    Returns None when the chapter or segment is missing (route -> 404)."""
    cur = await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    ch = await cur.fetchone()
    if ch is None:
        return None
    chapter_id = ch["id"]
    cur = await conn.execute(
        "SELECT source_text, source_hash, target_text, machine_text "
        "FROM chapter_segments WHERE chapter_id = ? AND seg_index = ?",
        (chapter_id, seg_index),
    )
    seg = await cur.fetchone()
    if seg is None:
        return None

    # Exact tier: same source_hash in OTHER chapters, full-text verified.
    cur = await conn.execute(
        "SELECT cs.seg_index, cs.source_text, cs.target_text, cs.status, "
        "       cs.confirmed_at, cs.updated_at, cs.id, c.chapter_num "
        "FROM chapter_segments cs JOIN chapters c ON c.id = cs.chapter_id "
        "WHERE cs.novel_id = ? AND cs.source_hash = ? "
        "  AND cs.chapter_id != ? AND cs.target_text != ''",
        (novel_id, seg["source_hash"], chapter_id),
    )
    exact_rows = [
        r for r in await cur.fetchall()
        if r["source_text"] == seg["source_text"]
    ]
    # Newest first, then a stable re-sort into status bands, so inside each
    # band (confirmed, edited, machine) recency still wins.
    exact_rows.sort(
        key=lambda r: ((r["confirmed_at"] or r["updated_at"] or ""), r["id"]),
        reverse=True,
    )
    exact_rows.sort(key=lambda r: _STATUS_RANK.get(r["status"], 3))
    tm_exact = [
        {
            "chapter_num": r["chapter_num"],
            "seg_index": r["seg_index"],
            "target_text": r["target_text"],
            "status": r["status"],
            "confirmed_at": r["confirmed_at"],
        }
        for r in exact_rows[:_ASSIST_CAP]
    ]

    tm_fuzzy = await _assist_fuzzy(conn, novel_id, chapter_id, seg)
    return {
        "tm_exact": tm_exact,
        "tm_fuzzy": tm_fuzzy,
        "machine_text": seg["machine_text"] or None,
    }


async def _assist_fuzzy(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_id: int,
    seg,
) -> list[dict]:
    """Fuzzy tier of `segment_assist`: near-duplicate sources elsewhere in
    the novel, scored over script-folded Han (canonical_zh), length-band
    prefiltered, capped. Skips the exact-hash rows (the exact tier owns
    them) and trivially short sources (they match everything).

    Cost bounds for large novels: a generous SQL LENGTH band (0.7x..1.6x of
    the raw source length; deliberately wider than the canonical 0.85..1.15
    band so folding slack can never exclude a real candidate) keeps the row
    fetch proportional to plausible matches, and the fold+score runs in
    `run_in_threadpool` (quality_dashboard precedent) so an uncached assist
    fetch cannot stutter the event loop mid-translate."""
    own_source = seg["source_text"]
    own_canon = canonical_zh(own_source)
    if len(own_canon) < _ASSIST_MIN_SOURCE_LEN:
        return []
    raw_len = len(own_source)
    cur = await conn.execute(
        "SELECT cs.source_text, cs.source_hash, cs.target_text, cs.status, "
        "       c.chapter_num "
        "FROM chapter_segments cs JOIN chapters c ON c.id = cs.chapter_id "
        "WHERE cs.novel_id = ? AND cs.chapter_id != ? AND cs.target_text != '' "
        "  AND LENGTH(cs.source_text) BETWEEN ? AND ?",
        (novel_id, chapter_id, int(raw_len * 0.7), int(raw_len * 1.6) + 1),
    )
    rows = await cur.fetchall()
    if not rows:
        return []
    return await run_in_threadpool(
        _score_assist_fuzzy, rows, own_canon, seg["source_hash"]
    )


def _score_assist_fuzzy(rows, own_canon: str, own_hash: str) -> list[dict]:
    """Pure CPU half of `_assist_fuzzy` (runs off the event loop): fold,
    dedupe by folded source keeping the best-status rendering, canonical
    length band, candidate cap, rapidfuzz scoring."""
    by_canon: dict[str, dict] = {}
    for r in rows:
        if r["source_hash"] == own_hash:
            continue
        canon = canonical_zh(r["source_text"] or "")
        if len(canon) < _ASSIST_MIN_SOURCE_LEN:
            continue
        lo, hi = len(own_canon) * 0.85, len(own_canon) * 1.15
        if not (lo <= len(canon) <= hi):
            continue
        entry = {
            "chapter_num": r["chapter_num"],
            "source_text": r["source_text"],
            "target_text": r["target_text"],
            "rank": _STATUS_RANK.get(r["status"], 3),
        }
        prev = by_canon.get(canon)
        if prev is None or (entry["rank"], entry["chapter_num"]) < (
            prev["rank"], prev["chapter_num"]
        ):
            by_canon[canon] = entry
    if not by_canon:
        return []
    candidates = list(by_canon.keys())
    if len(candidates) > _ASSIST_MAX_CANDIDATES:
        candidates.sort(key=lambda c: abs(len(c) - len(own_canon)))
        candidates = candidates[:_ASSIST_MAX_CANDIDATES]
    scored = process.extract(
        own_canon, candidates,
        scorer=fuzz.ratio,
        score_cutoff=_ASSIST_FUZZY_THRESHOLD * 100.0,
        limit=_ASSIST_CAP,
    )
    out = []
    for choice, score, _key in scored:
        e = by_canon[choice]
        out.append({
            "score": round(score / 100.0, 3),
            "chapter_num": e["chapter_num"],
            "source_text": e["source_text"],
            "target_text": e["target_text"],
        })
    return out
