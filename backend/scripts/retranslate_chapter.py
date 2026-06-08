"""Force-retranslate one chapter and print the result, for validating a prompt
or glossary change against a known chapter.

This is the reusable form of the ad-hoc validation done for the base.md
compose-first reframe: change a prompt / glossary, re-run a fixture chapter
through the REAL translator and the full deterministic pipeline, then diff the
output against the user's edit by eye or with `difflib`.

    python -m backend.scripts.retranslate_chapter <novel_id> <chapter_num>

It sets force_retranslate (so the cache is bypassed and the LLM actually re-runs
with the new prompt), runs the same `queue._translate_chapter_in_db` worker the
app uses, and prints the new title + body. It uses whatever DB the ambient
config points at (dev `data/novels.db` unless `LN_TRANSLATOR_DATA` is set), so
point it at the dev DB to avoid disturbing the live reader.

NOTE: this makes a real provider call and burns the Claude subscription window
(the default provider is the in-process Claude Agent SDK). Run it deliberately,
one chapter at a time.
"""

from __future__ import annotations

import argparse
import asyncio

from backend.db import open_conn
from backend.scripts._db_banner import confirm_db, print_db_banner
from backend.services import queue


async def _run(novel_id: int, chapter_num: int) -> None:
    async with open_conn() as conn:
        row = await (
            await conn.execute(
                "SELECT id FROM chapters WHERE novel_id=? AND chapter_num=?",
                (novel_id, chapter_num),
            )
        ).fetchone()
        if row is None:
            raise SystemExit(f"no chapter {chapter_num} for novel {novel_id}")
        chapter_id = row["id"]
        await conn.execute(
            "UPDATE chapters SET status='pending', translate_queued=1, "
            "force_retranslate=1 WHERE id=?",
            (chapter_id,),
        )
        await conn.commit()

    async with open_conn() as conn:
        await queue._translate_chapter_in_db(conn, novel_id, chapter_id)

    async with open_conn() as conn:
        r = await (
            await conn.execute(
                "SELECT status, title_en, translated_text FROM chapters WHERE id=?",
                (chapter_id,),
            )
        ).fetchone()
    print(f"[status={r['status']}] {r['title_en']}\n")
    print(r["translated_text"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("novel_id", type=int)
    ap.add_argument("chapter_num", type=int)
    ap.add_argument("--yes", action="store_true",
                    help="skip the DB-write confirmation prompt")
    args = ap.parse_args()
    print_db_banner(mutates=True)
    if not confirm_db(
        f"force-retranslate novel {args.novel_id} chapter {args.chapter_num}",
        assume_yes=args.yes,
    ):
        raise SystemExit(1)
    asyncio.run(_run(args.novel_id, args.chapter_num))


if __name__ == "__main__":
    main()
