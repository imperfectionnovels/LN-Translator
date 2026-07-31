"""Segment store service (CAT Phase 2). The ONLY module that reads or
writes `chapter_segments`.

A segment row is one effective source paragraph of a translated chapter
(`segmentation.effective_source_paragraphs` over `original_text`) paired with
the paragraph of the DISPLAYED body that renders it. The displayed body (the
chapter text column the reader shows: refined when refinement is done, else
the draft) stays canonical; segments are a projection of it plus durable
status/provenance state, so `join(target_text ORDER BY seg_index)` always
reproduces the displayed paragraphs.

Phase 2 scope: lazy backfill + read. `get_segments` builds (or rebuilds) the
rows on demand from the COMMITTED chapter text. It must never assume a
paragraph count from the 1:1 pipeline: post-validation fixups (the
mid-sentence comma weld, leading-title strip) can change the committed body's
paragraph count relative to what the translator validated, so the split is
always recomputed from the stored text (docs/decisions.md, 2026-07-30).

Rebuild is wholesale DELETE+INSERT this phase: every row is status='machine',
so nothing user-authored can be lost. Phase 3 adds status preservation at the
`_preserve_human_rows` seam marked inside `build_segments_from_alignment`.

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


def _hash16(text: str) -> str:
    """16-hex sha256 prefix, the same convention as tm.py's source_hash."""
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
    conn: aiosqlite.Connection, chapter_id: int, state: str
) -> None:
    await conn.execute(
        "DELETE FROM chapter_segments WHERE chapter_id = ?", (chapter_id,)
    )
    await conn.execute(
        "UPDATE chapters SET segments_state = ?, segmentation_version = ? "
        "WHERE id = ?",
        (state, SEGMENTATION_VERSION, chapter_id),
    )


async def build_segments_from_alignment(
    conn: aiosqlite.Connection, chapter_row
) -> str:
    """(Re)build one chapter's segment rows from its committed text.

    Splits `effective_source_paragraphs(original_text)` against
    `split_target_paragraphs(displayed body)`. Equal counts map positionally
    (the 1:1 contract held: every row aligned=1). Unequal counts go through
    `tm.full_alignment_path`; a None (below the confidence gate) deletes any
    rows and stamps 'unaligned'.

    All writes ride the caller's transaction (DELETE+INSERT+stamp commit or
    roll back together). Returns the state written: 'ok' | 'partial' |
    'unaligned'.

    Phase 3 seam (_preserve_human_rows): status preservation on rebuild goes
    HERE, snapshot rows with status != 'machine' before the DELETE and merge
    their target_text/status back onto matching seg_index/source_hash rows.
    This phase every row is 'machine' by construction, so the wholesale
    DELETE+INSERT below loses nothing.
    """
    chapter_id = chapter_row["id"]
    novel_id = chapter_row["novel_id"]
    _variant, body = displayed_body(chapter_row)
    src = effective_source_paragraphs(chapter_row["original_text"] or "")
    tgt = split_target_paragraphs(body)

    if not src or not tgt:
        await _clear_and_stamp(conn, chapter_id, "unaligned")
        return "unaligned"

    if len(src) == len(tgt):
        # The 1:1 contract held (or the counts happen to agree): position is
        # the key, every pair is a confident anchor.
        entries: list[tuple[str, bool]] = [(t, True) for t in tgt]
    else:
        path = tm_svc.full_alignment_path(src, tgt)
        if path is None:
            await _clear_and_stamp(conn, chapter_id, "unaligned")
            logger.info(
                "segments: chapter %d unalignable (%d src vs %d tgt "
                "paragraphs below the confidence gate)",
                chapter_id, len(src), len(tgt),
            )
            return "unaligned"
        entries = path

    await conn.execute(
        "DELETE FROM chapter_segments WHERE chapter_id = ?", (chapter_id,)
    )
    await conn.executemany(
        "INSERT INTO chapter_segments "
        "(novel_id, chapter_id, seg_index, source_text, source_hash, "
        " target_text, machine_text, status, origin, aligned) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'machine', 'aligned_backfill', ?)",
        [
            (novel_id, chapter_id, i, src[i], _hash16(src[i]),
             text, text, 1 if aligned else 0)
            for i, (text, aligned) in enumerate(entries)
        ],
    )
    state = "ok" if all(aligned for _t, aligned in entries) else "partial"
    await conn.execute(
        "UPDATE chapters SET segments_state = ?, segmentation_version = ? "
        "WHERE id = ?",
        (state, SEGMENTATION_VERSION, chapter_id),
    )
    return state


_CHAPTER_SELECT = (
    "SELECT id, novel_id, chapter_num, title_zh, title_en, original_text, "
    "       translated_text, refined_text, refinement_status, status, "
    "       segments_state, segmentation_version "
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
    landing). The caller owns the commit.
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
    needs_rebuild = (
        not seg_rows
        or row["segmentation_version"] != SEGMENTATION_VERSION
        or not _segments_match_body([r["target_text"] for r in seg_rows], body)
    )
    if needs_rebuild:
        state = await build_segments_from_alignment(conn, row)
        seg_rows = (
            await _fetch_segment_rows(conn, row["id"])
            if state != "unaligned" else []
        )
    return _payload(row, variant, body, state, seg_rows)
