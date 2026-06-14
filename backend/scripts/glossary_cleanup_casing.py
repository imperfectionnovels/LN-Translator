"""Corpus-wide cleanup of already-stored glossary casing pollution.

The casing layer (`glossary_casing`) stops NEW generic abstracts from being
stored Title-Cased, but nothing ever re-evaluates entries already in the DB
(confirmed: no automatic demotion path exists). A novel translated before a
`GENERIC_LOWERCASE` term was added still carries the Title-Cased row, which keeps
training the register through the prompt glossary block — and, when the row is
locked into a named category, keeps getting force-cased into prose by
`enforce_locked_term_casing`.

This maintenance tool down-cases `term_en` for every stored entry whose
`lower(term_en)` is in `GENERIC_LOWERCASE` (the single source of truth for
"generic in every novel"), across ALL novels. Once the stored form is lowercase
and in that set, both legs are neutralised: `is_atomic_case_locked_term` drops it
(no prose force-casing) and `format_glossary` injects it lowercase.

Safety, mirroring `ingest_edited_chapter --apply`:
- DRY-RUN by default — prints what would change, writes nothing. Pass `--apply`.
- AUTO rows (locked=0) are down-cased on `--apply`.
- LOCKED rows are user decisions: they are LISTED for review and left untouched
  unless you also pass `--include-locked`.
- `--novel N` narrows to one novel; default is the whole corpus.

Read-only unless `--apply` is given.

    python -m backend.scripts.glossary_cleanup_casing                 # dry run, corpus
    python -m backend.scripts.glossary_cleanup_casing --apply
    python -m backend.scripts.glossary_cleanup_casing --apply --include-locked
    python -m backend.scripts.glossary_cleanup_casing --novel 7 --apply
"""

from __future__ import annotations

import argparse
import asyncio

import aiosqlite

from backend.db import open_conn
from backend.services.glossary_casing import GENERIC_LOWERCASE


async def _run(novel: int | None, apply: bool, include_locked: bool) -> None:
    sql = (
        "SELECT g.id, g.novel_id, g.term_zh, g.term_en, g.category, g.locked, "
        "       n.title AS novel_title "
        "FROM glossary_entries g JOIN novels n ON n.id = g.novel_id"
    )
    params: tuple = ()
    if novel is not None:
        sql += " WHERE g.novel_id = ?"
        params = (novel,)
    sql += " ORDER BY g.novel_id, g.term_en"

    async with open_conn() as conn:
        rows = await (await conn.execute(sql, params)).fetchall()

        to_fix: list[aiosqlite.Row] = []
        locked_review: list[aiosqlite.Row] = []
        for r in rows:
            en = (r["term_en"] or "").strip()
            if not en or en == en.lower():
                continue  # already lowercase / empty
            if en.lower() not in GENERIC_LOWERCASE:
                continue  # not a universally-generic term — leave it
            if r["locked"] and not include_locked:
                locked_review.append(r)
            else:
                to_fix.append(r)

        verb = "down-casing" if apply else "would down-case"
        print(f"{verb} {len(to_fix)} entr{'y' if len(to_fix) == 1 else 'ies'} "
              f"across {len({r['novel_id'] for r in to_fix})} novel(s):")
        for r in to_fix:
            lock = " [locked]" if r["locked"] else ""
            print(f"  novel {r['novel_id']} ({r['novel_title']}): "
                  f"{r['term_zh']}  {r['term_en']!r} -> {r['term_en'].lower()!r}"
                  f"  ({r['category']}){lock}")

        if locked_review:
            print(f"\n{len(locked_review)} LOCKED entr"
                  f"{'y' if len(locked_review) == 1 else 'ies'} match but are left "
                  f"untouched (re-run with --include-locked to fix):")
            for r in locked_review:
                print(f"  novel {r['novel_id']} ({r['novel_title']}): "
                      f"{r['term_zh']}  {r['term_en']!r}  ({r['category']})")

        if not apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
            return
        if not to_fix:
            print("\nnothing to write.")
            return

        await conn.executemany(
            "UPDATE glossary_entries SET term_en = ? WHERE id = ?",
            [(r["term_en"].lower(), r["id"]) for r in to_fix],
        )
        await conn.commit()
        print(f"\napplied: {len(to_fix)} entr"
              f"{'y' if len(to_fix) == 1 else 'ies'} down-cased.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--novel", type=int, default=None,
                    help="narrow to one novel id (default: whole corpus)")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry run)")
    ap.add_argument("--include-locked", action="store_true",
                    help="also down-case locked rows (default: list for review)")
    args = ap.parse_args()
    asyncio.run(_run(args.novel, args.apply, args.include_locked))


if __name__ == "__main__":
    main()
