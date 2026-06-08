"""Re-run translation-memory paragraph alignment over stored chapters.

The TM (`tm_segments`) is normally repopulated by the queue worker on each
successful translate, so it only ever reflects the aligner that was in
effect at translate time. After the aligner changes (e.g. the length-based
rewrite that replaced the naive positional zip), existing rows stay stale
until each chapter is retranslated. This script re-aligns in place from the
already stored `original_text` / `translated_text`, with no LLM calls, so a
whole novel's TM is corrected at once.

Database selection follows the app. By default it uses the dev DB
(`data/novels.db`). To target the packaged app's database, point
`LN_TRANSLATOR_DATA` at its data root, e.g. on Windows:

    set LN_TRANSLATOR_DATA=%APPDATA%\\LN-Translator
    python -m backend.scripts.realign_tm --novel 2

Best run with the app closed so the script does not contend with the
worker for SQLite's single writer.
"""

from __future__ import annotations

import argparse
import asyncio

from backend.db import open_conn
from backend.services import tm as tm_svc


async def _realign(novel_id: int | None) -> None:
    where = "status = 'done' AND translated_text IS NOT NULL"
    params: tuple = ()
    if novel_id is not None:
        where += " AND novel_id = ?"
        params = (novel_id,)

    async with open_conn() as conn:
        cur = await conn.execute(
            f"SELECT id, novel_id FROM chapters WHERE {where} "
            "ORDER BY novel_id, chapter_num",
            params,
        )
        targets = [(r["id"], r["novel_id"]) for r in await cur.fetchall()]

    processed = skipped = pairs_total = 0
    async with open_conn() as conn:
        for cid, nid in targets:
            cur = await conn.execute(
                "SELECT original_text, translated_text FROM chapters WHERE id = ?",
                (cid,),
            )
            ch = await cur.fetchone()
            n = await tm_svc.replace_chapter_segments(
                conn, nid, cid, ch["original_text"], ch["translated_text"]
            )
            processed += 1
            pairs_total += n
            if n == 0:
                skipped += 1
            if processed % 100 == 0:
                await conn.commit()
                print(f"  ...{processed}/{len(targets)} chapters")
        await conn.commit()

    print(
        f"realign done: {processed} chapters, {pairs_total} aligned pairs "
        f"written, {skipped} chapters skipped (alignment below confidence "
        f"threshold)"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-align tm_segments from stored chapter text."
    )
    ap.add_argument(
        "--novel",
        type=int,
        default=None,
        help="novel id to realign; omit to realign every novel",
    )
    args = ap.parse_args()
    asyncio.run(_realign(args.novel))


if __name__ == "__main__":
    main()
