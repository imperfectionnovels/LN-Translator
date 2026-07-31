"""Segment store service (CAT Phases 2+3). The ONLY module that reads or
writes `chapter_segments`.

A segment row is one effective source paragraph of a translated chapter
(`segmentation.effective_source_paragraphs` over `original_text`) paired with
the paragraph of the DISPLAYED body that renders it. The displayed body (the
chapter text column the reader shows: refined when refinement is done, else
the draft) stays canonical; segments are a projection of it plus durable
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

Async, aiosqlite. No route logic; callers own the commit.
"""

from __future__ import annotations

import hashlib
import logging

import aiosqlite

from backend.services import tm as tm_svc
from backend.services.segmentation import (
    SEGMENTATION_VERSION,
    effective_source_paragraphs,
    split_target_paragraphs,
)

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


def _hash16(text: str) -> str:
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

    'refined' iff refinement_status == 'done' AND refined_text is non-empty;
    'draft' with translated_text otherwise. Mirrors the reader's
    `_displayedEnglish`; consistency.py imports this so the two stacks
    cannot drift. Text is "" (never None) when the chapter has no English.
    """
    if (row["refinement_status"] or "none") == "done" and row["refined_text"]:
        return ("refined", row["refined_text"])
    return ("draft", row["translated_text"] or "")


def chapter_rev(text: str) -> str:
    """Revision token of a displayed body: 16-hex sha256. The editor sends it
    back on writes (Phase 3) as the stale-tab guard."""
    return _hash16(text)


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
        "SELECT id, seg_index, source_hash, status FROM chapter_segments "
        "WHERE chapter_id = ? AND status != 'machine' ORDER BY seg_index",
        (chapter_id,),
    )
    return list(await cur.fetchall())


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
    human_rows: list, new_hashes: list[str]
) -> dict[int, int] | None:
    """Map each human row id to its seg_index in the NEW source list.

    Anchor by source_hash; when the source list is unchanged the row's own
    seg_index still carries the same hash, so position is the natural first
    candidate. Duplicate source paragraphs resolve to the nearest unclaimed
    index. Returns None when any human row cannot be anchored (its source
    paragraph vanished): the caller must retain all rows instead of
    rebuilding, because a rebuild would have no slot for the human work.
    """
    used: set[int] = set()
    anchors: dict[int, int] = {}
    for r in human_rows:
        i = r["seg_index"]
        if (
            0 <= i < len(new_hashes)
            and new_hashes[i] == r["source_hash"]
            and i not in used
        ):
            anchors[r["id"]] = i
            used.add(i)
            continue
        candidates = [
            j for j, h in enumerate(new_hashes)
            if h == r["source_hash"] and j not in used
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
        positionally); status / origin / machine_text / edited_at /
        confirmed_at ride along. target_text takes the NEW body's paragraph
        for that slot: rebuilds are text-authoritative (the body already
        changed out of band), so the body wins on text and the row keeps its
        human status, exactly like `reproject_from_body`.
      - When the alignment fails (below the <50% gate, empty split, or a
        human row's source paragraph vanished), every row is RETAINED
        untouched and the chapter is stamped 'unaligned' (rows kept). Only
        human-row-free chapters keep the Phase 2 zero-row behavior.

    All writes ride the caller's transaction. Returns the state written:
    'ok' | 'partial' | 'unaligned'.
    """
    chapter_id = chapter_row["id"]
    novel_id = chapter_row["novel_id"]
    _variant, body = displayed_body(chapter_row)
    rev = chapter_rev(body)
    src = effective_source_paragraphs(chapter_row["original_text"] or "")
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

    new_hashes = [_hash16(s) for s in src]
    anchors = _anchor_human_rows(human_rows, new_hashes) if human_rows else {}
    if anchors is None:
        return await _retain_rows_stamp_unaligned(
            conn, chapter_id, rev, "human row's source paragraph vanished"
        )

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
            await conn.execute(
                "UPDATE chapter_segments SET seg_index = ?, source_text = ?, "
                "source_hash = ?, target_text = ?, aligned = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (j, src[j], new_hashes[j], text, 1 if aligned else 0, row_id),
            )
    human_indexes = set(anchors.values())
    await conn.executemany(
        "INSERT INTO chapter_segments "
        "(novel_id, chapter_id, seg_index, source_text, source_hash, "
        " target_text, machine_text, status, origin, aligned) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'machine', 'aligned_backfill', ?)",
        [
            (novel_id, chapter_id, i, src[i], new_hashes[i],
             text, text, 1 if aligned else 0)
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
        "SELECT seg_index, source_text, source_hash, target_text, status, "
        "       origin, aligned, confirmed_at "
        "FROM chapter_segments WHERE chapter_id = ? ORDER BY seg_index",
        (chapter_id,),
    )
    return list(await cur.fetchall())


def _payload(row, variant: str, body: str, state, seg_rows) -> dict:
    segments = [
        {
            "index": r["seg_index"],
            "source_text": r["source_text"],
            "target_text": r["target_text"],
            "source_hash": r["source_hash"],
            "target_hash": _hash16(r["target_text"]),
            "status": r["status"],
            "origin": r["origin"],
            "aligned": bool(r["aligned"]),
            "confirmed_at": r["confirmed_at"],
        }
        for r in seg_rows
    ]
    confirmed = sum(1 for s in segments if s["status"] == "confirmed")
    next_unconfirmed = next(
        (s["index"] for s in segments if s["status"] != "confirmed"), None
    )
    return {
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
        "target_hash": _hash16(r["target_text"]),
        "status": r["status"],
        "origin": r["origin"],
        "aligned": bool(r["aligned"]),
        "confirmed_at": r["confirmed_at"],
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
):
    """Shared write guards. Returns (chapter_row, variant, body).

    Raises SegmentNotFoundError (missing chapter), SegmentStaleError
    'chapter_translating' (not status='done' yet: pending, translating, or
    errored), 'stale_chapter' (client rev does not match the displayed body,
    or the chapter is 'unaligned' so segment writes cannot rematerialize the
    body; the client recovery for both is the same re-GET).
    """
    cur = await conn.execute(_CHAPTER_SELECT, (novel_id, chapter_num))
    row = await cur.fetchone()
    if row is None:
        raise SegmentNotFoundError("chapter not found")
    if row["status"] != "done":
        raise SegmentStaleError(
            "chapter_translating",
            f"chapter is not editable while its translation status is "
            f"'{row['status']}'. Wait for it to finish, then reload.",
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
    chapter_rev: str,
    before_target_hash: str,
) -> dict:
    """The segment state machine. Actions:

      - save:             write after_text, status -> 'edited' (a confirmed
                          row demotes), origin -> 'human', aligned -> 1.
      - confirm:          status -> 'confirmed', no text change. Idempotent
                          on an already-confirmed row.
      - save_and_confirm: both in one write (status -> 'confirmed').
      - unconfirm:        confirmed -> 'edited' (other states are a 400).
      - revert_machine:   edited|confirmed -> 'machine', target_text :=
                          machine_text (400 when machine_text is NULL or the
                          row is already machine).

    Guards (409 via SegmentStaleError): chapter must be status='done';
    `chapter_rev` must match the displayed body's current rev; the row's
    current target hash must match `before_target_hash`. Empty after_text on
    a save is a 400 (a paragraph cannot be emptied; revert_machine is the
    escape hatch).

    After any text-changing action the displayed body column is regenerated
    from the targets in the SAME transaction (invariant I1); the caller owns
    the commit. Returns {segment, chapter_rev, segments_state, progress,
    next_unconfirmed_index}.
    """
    if action not in _ACTIONS:
        raise SegmentActionError(f"unknown action {action!r}")
    row, variant, body = await _load_editable_chapter(
        conn, novel_id, chapter_num, chapter_rev
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
    if _hash16(seg["target_text"]) != before_target_hash:
        raise SegmentStaleError(
            "stale_segment",
            "this segment changed since the page loaded. Reload the "
            "segments and retry.",
        )

    status = seg["status"]
    text_changing = False
    if action in ("save", "save_and_confirm"):
        new_target = (after_text or "").strip()
        if not new_target:
            raise SegmentActionError(
                "a paragraph cannot be emptied. Use revert to AI instead."
            )
        new_status = "edited" if action == "save" else "confirmed"
        text_changing = True
        await conn.execute(
            "UPDATE chapter_segments SET target_text = ?, status = ?, "
            "origin = 'human', aligned = 1, edited_at = datetime('now'), "
            "confirmed_at = CASE WHEN ? = 'confirmed' "
            "THEN datetime('now') ELSE NULL END, "
            "updated_at = datetime('now') WHERE id = ?",
            (new_target, new_status, new_status, seg["id"]),
        )
    elif action == "confirm":
        if not seg["target_text"]:
            raise SegmentActionError(
                "cannot confirm a segment with no paragraph. Write its "
                "text first."
            )
        if status != "confirmed":
            await conn.execute(
                "UPDATE chapter_segments SET status = 'confirmed', "
                "aligned = 1, confirmed_at = datetime('now'), "
                "updated_at = datetime('now') WHERE id = ?",
                (seg["id"],),
            )
    elif action == "unconfirm":
        if status != "confirmed":
            raise SegmentActionError(
                "only a confirmed segment can be unconfirmed."
            )
        await conn.execute(
            "UPDATE chapter_segments SET status = 'edited', "
            "confirmed_at = NULL, updated_at = datetime('now') WHERE id = ?",
            (seg["id"],),
        )
    else:  # revert_machine
        if status == "machine":
            raise SegmentActionError(
                "segment already shows the AI translation."
            )
        if not seg["machine_text"]:
            # NULL or "" alike: an empty machine_text means the aligner had
            # no paragraph for this slot, so a revert would empty it (and on
            # a refined chapter whose only paragraph it was, flip the
            # displayed body back to the draft under a rev stamped against
            # ""). There is nothing to revert TO; refuse.
            raise SegmentActionError(
                "no AI translation is stored for this segment."
            )
        text_changing = True
        # Origin returns to 'aligned_backfill' (the machine text's producer
        # in this phase; the Phase 4 worker merge re-stamps real provenance
        # when it refreshes machine rows).
        await conn.execute(
            "UPDATE chapter_segments SET target_text = machine_text, "
            "status = 'machine', origin = 'aligned_backfill', "
            "edited_at = NULL, confirmed_at = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (seg["id"],),
        )

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
        "chapter_rev": _hash16(new_body),
        "segments_state": state_row["segments_state"],
        "progress": progress,
        "next_unconfirmed_index": next_unconfirmed,
    }


async def confirm_all(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_num: int,
    *,
    chapter_rev: str,
    statuses: list[str] | None = None,
) -> dict:
    """Confirm every segment currently in one of `statuses` (default
    ['machine', 'edited']). Rev-guarded like update_segment; empty-target
    rows are skipped (an unwritten paragraph cannot be confirmed). No text
    changes, so no rematerialization. Returns {confirmed, chapter_rev,
    segments_state, progress, next_unconfirmed_index}."""
    wanted = statuses if statuses is not None else ["machine", "edited"]
    if not wanted or any(s not in ("machine", "edited") for s in wanted):
        raise SegmentActionError(
            "statuses must be a non-empty subset of ['machine', 'edited']."
        )
    row, _variant, body = await _load_editable_chapter(
        conn, novel_id, chapter_num, chapter_rev
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
        "chapter_rev": chapter_rev,
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
    find-replace across confirmed segments must not un-confirm them.
    machine_text refreshes only for machine rows. On count drift the
    preservation-aware `build_segments_from_alignment` takes over.

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
    paras = split_target_paragraphs(body)
    non_empty = [r for r in seg_rows if r["target_text"]]
    simple = all(
        len(split_target_paragraphs(r["target_text"])) <= 1 for r in non_empty
    )
    if simple and paras and len(paras) == len(non_empty):
        for r, new_text in zip(non_empty, paras):
            if r["target_text"] == new_text:
                continue
            await conn.execute(
                "UPDATE chapter_segments SET target_text = ?, "
                "machine_text = CASE WHEN status = 'machine' THEN ? "
                "ELSE machine_text END, "
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


async def seg_index_for_display_paragraph(
    conn: aiosqlite.Connection,
    chapter_id: int,
    paragraph_index: int,
    expected_paragraphs: int,
) -> tuple[int, str] | None:
    """Map a reader display-paragraph index (position in the body's raw
    "\\n\\n" split) onto a seg_index for the edit-paragraph reroute.

    Empty-target rows do not render as paragraphs (the I1 join skips them),
    so the mapping walks the non-empty rows in seg_index order. It is only
    well defined when the non-empty row count equals the display paragraph
    count AND no stored target spans multiple paragraphs; otherwise None,
    and the caller falls back to the legacy direct-text path. Returns
    (seg_index, stored_target_text) so the caller can verify the stored
    target matches the paragraph the client saw.
    """
    seg_rows = await _fetch_segment_rows(conn, chapter_id)
    non_empty = [r for r in seg_rows if r["target_text"]]
    if len(non_empty) != expected_paragraphs:
        return None
    if any("\n\n" in r["target_text"] for r in non_empty):
        return None
    if not (0 <= paragraph_index < len(non_empty)):
        return None
    r = non_empty[paragraph_index]
    return r["seg_index"], r["target_text"]
