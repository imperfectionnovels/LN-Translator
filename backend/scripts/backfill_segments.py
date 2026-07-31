"""Bulk-build the CAT segment store for already-translated chapters.

Segments are normally built lazily, on the first `GET .../segments` when the
editor opens a chapter. This script runs the same service
(`services/segments.py::build_segments_from_alignment`) over every done
chapter up front, so a whole back catalog is browsable in the editor without
paying the per-open alignment cost, and prints the resulting
ok / partial / unaligned distribution (the 1:1-health census).

Database selection follows the app. By default it uses the dev DB
(`data/novels.db`). To target the packaged app's database, point
`LN_TRANSLATOR_DATA` at its data root, e.g. on Windows:

    set LN_TRANSLATOR_DATA=%APPDATA%\\LN-Translator
    python -m backend.scripts.backfill_segments --novel 2

Best run with the app closed so the script does not contend with the worker
for SQLite's single writer. Safe to re-run: rebuilding a chapter whose rows
are all status='machine' (the only status this phase writes) is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from backend.db import open_conn
from backend.scripts._db_banner import confirm_db, print_db_banner
from backend.services import segments as segments_svc


async def _backfill(novel_id: int | None) -> None:
    where = "status = 'done' AND translated_text IS NOT NULL"
    params: tuple = ()
    if novel_id is not None:
        where += " AND novel_id = ?"
        params = (novel_id,)

    async with open_conn() as conn:
        cur = await conn.execute(
            f"SELECT id FROM chapters WHERE {where} "
            "ORDER BY novel_id, chapter_num",
            params,
        )
        targets = [r["id"] for r in await cur.fetchall()]

    states: Counter[str] = Counter()
    processed = 0
    async with open_conn() as conn:
        for cid in targets:
            cur = await conn.execute(
                "SELECT id, novel_id, original_text, translated_text, "
                "       refined_text, refinement_status "
                "FROM chapters WHERE id = ?",
                (cid,),
            )
            ch = await cur.fetchone()
            state = await segments_svc.build_segments_from_alignment(conn, ch)
            states[state] += 1
            processed += 1
            if processed % 100 == 0:
                await conn.commit()
                print(f"  ...{processed}/{len(targets)} chapters")
        await conn.commit()

    print(
        f"backfill done: {processed} chapters "
        f"(ok={states['ok']}, partial={states['partial']}, "
        f"unaligned={states['unaligned']})"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build chapter_segments for every done chapter."
    )
    ap.add_argument(
        "--novel",
        type=int,
        default=None,
        help="novel id to backfill; omit to backfill every novel",
    )
    ap.add_argument(
        "--yes",
        action="store_true",
        help="skip the write confirmation (scripted runs)",
    )
    args = ap.parse_args()
    print_db_banner(mutates=True)
    if not confirm_db("backfill chapter segments", assume_yes=args.yes):
        return
    asyncio.run(_backfill(args.novel))


if __name__ == "__main__":
    main()
