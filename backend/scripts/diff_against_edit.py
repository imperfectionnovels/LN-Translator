"""Measure how close a chapter's translation is to the user's hand-edit, per
paragraph. This operationalizes the graduation rule: run it before and after a
prompt change to see whether the change moved the output toward the ground-truth
edit, and cite the per-paragraph pairs in the commit.

    python -m backend.scripts.diff_against_edit --novel-id N --chapter C \
        --edited-file PATH [--retranslate] [--source draft|refined]

Without --retranslate it scores the STORED body (translated_text / refined_text)
against the edit, which is free and instant: a baseline read. With --retranslate
it force-retranslates the chapter through the REAL translator + full pipeline
first (this burns the Claude subscription window), so the score reflects the
CURRENT prompt/glossary. The scoring mirrors ab_style_edits: for each paragraph
the user edited, find the candidate paragraph closest to the ORIGINAL draft, then
report its difflib similarity to the user's EDIT. Higher mean = closer to ground
truth.

It uses the ambient config DB (dev unless LN_TRANSLATOR_DATA points at the live
store). --retranslate mutates the chapter row, so point it at a dev copy unless
you intend to refresh the live chapter.
"""

from __future__ import annotations

import argparse
import asyncio

from backend.db import open_conn
from backend.scripts.ab_style_edits import _clip, _score
from backend.scripts.ingest_edited_chapter import _align_pairs, _split_paras


async def _retranslate(novel_id: int, chapter_id: int) -> None:
    from backend.services import queue
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status='pending', translate_queued=1, "
            "force_retranslate=1 WHERE id=?",
            (chapter_id,),
        )
        await conn.commit()
    async with open_conn() as conn:
        await queue._translate_chapter_in_db(conn, novel_id, chapter_id)


async def run(novel_id, chapter_num, edited_text, source, retranslate) -> int:
    async with open_conn() as conn:
        ch = await (await conn.execute(
            "SELECT id, translated_text, refined_text FROM chapters "
            "WHERE novel_id=? AND chapter_num=?",
            (novel_id, chapter_num),
        )).fetchone()
        if ch is None:
            print(f"ERROR: novel {novel_id} has no chapter {chapter_num}.")
            return 2
        chapter_id = ch["id"]
        stored = ch["refined_text"] if source == "refined" else ch["translated_text"]
        if not stored:
            print(f"ERROR: chapter {chapter_num} has no {source} text.")
            return 2

    # The ground-truth deltas: paragraphs the user actually changed vs the
    # stored draft. We score the candidate against these after_texts.
    pairs, _, _ = _align_pairs(_split_paras(stored), _split_paras(edited_text))
    if not pairs:
        print("No changed paragraphs between the stored draft and the edit; "
              "nothing to score.")
        return 0

    if retranslate:
        print(f"Force-retranslating novel {novel_id} chapter {chapter_num} "
              "(burns the subscription window)...", flush=True)
        await _retranslate(novel_id, chapter_id)
        async with open_conn() as conn:
            r = await (await conn.execute(
                "SELECT translated_text, refined_text FROM chapters WHERE id=?",
                (chapter_id,),
            )).fetchone()
        candidate = (r["refined_text"] if source == "refined" else r["translated_text"]) or ""
    else:
        candidate = stored

    cand_paras = _split_paras(candidate)
    rows = []
    for i, (before, after) in enumerate(pairs, 1):
        ratio, best = _score(cand_paras, before, after)
        rows.append((i, ratio, before, after, best))

    mean = sum(r[1] for r in rows) / len(rows)
    label = "RETRANSLATED" if retranslate else f"STORED ({source})"
    print(f"\ncloseness of {label} output to the edit  (novel {novel_id} ch {chapter_num})")
    print(f"{'para':>4}  {'ratio':>6}")
    print("-" * 16)
    for i, ratio, *_ in rows:
        print(f"{i:>4}  {ratio:>6.3f}")
    print("-" * 16)
    print(f"mean  {mean:>6.3f}   (over {len(rows)} edited paragraphs)")
    print("\nPer-paragraph (cite these in any commit that acts on the result):")
    for i, ratio, before, after, best in rows:
        print(f"\n[{i}] ratio={ratio:.3f}")
        print(f"  edit:      {_clip(after)}")
        print(f"  candidate: {_clip(best)}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--novel-id", type=int, required=True)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--edited-file", required=True, help="UTF-8 ground-truth edit")
    ap.add_argument("--source", choices=["draft", "refined"], default="draft")
    ap.add_argument("--retranslate", action="store_true",
                    help="force a fresh translation first (burns the subscription window)")
    args = ap.parse_args()
    with open(args.edited_file, encoding="utf-8") as fh:
        edited = fh.read()
    raise SystemExit(asyncio.run(
        run(args.novel_id, args.chapter, edited, args.source, args.retranslate)
    ))


if __name__ == "__main__":
    main()
