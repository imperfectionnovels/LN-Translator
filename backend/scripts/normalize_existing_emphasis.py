"""Strip stray (unpaired) Markdown emphasis from already-stored chapters.

The translator occasionally drops one half of a `**bold**` / `*italic*` pair.
The reader renders Markdown with `marked`, which cannot match an unpaired
delimiter and shows it as a literal asterisk (a "stray symbol", e.g. a
`Sword Heart Illumination.**` paragraph in chapter 372). `enforce_balanced_emphasis`
is now part of the live pipeline so new translations are clean; this script
back-fills the fix onto chapters that were translated before it existed.

    # show what would change, change nothing:
    python -m backend.scripts.normalize_existing_emphasis --dry-run
    # restrict to one novel:
    python -m backend.scripts.normalize_existing_emphasis --novel 2 --dry-run
    # apply:
    python -m backend.scripts.normalize_existing_emphasis

It applies the SAME deterministic `enforce_balanced_emphasis` fixup to each
chapter's `translated_text` and `refined_text` (whichever is non-empty) and only
UPDATEs rows the fixup actually changed. No LLM call. It uses whatever DB the
ambient config points at (dev `data/novels.db` unless `LN_TRANSLATOR_DATA` is
set), so point `LN_TRANSLATOR_DATA` at the live `%APPDATA%\\LN-Translator\\novels.db`
to clean the reader's database, or leave it unset to clean the dev copy.
"""

from __future__ import annotations

import argparse
import asyncio

from backend.db import open_conn
from backend.services.text_fixups import enforce_balanced_emphasis

_COLUMNS = ("translated_text", "refined_text")


def _changed_paragraphs(before: str, after: str) -> list[tuple[str, str]]:
    """Paragraph-level (before, after) pairs that differ, for the dry-run log."""
    b = (before or "").split("\n\n")
    a = (after or "").split("\n\n")
    pairs: list[tuple[str, str]] = []
    for ob, oa in zip(b, a):
        if ob != oa:
            pairs.append((ob, oa))
    return pairs


async def _run(novel_id: int | None, dry_run: bool) -> None:
    where = "WHERE novel_id = ?" if novel_id is not None else ""
    params: tuple = (novel_id,) if novel_id is not None else ()

    async with open_conn() as conn:
        rows = await (
            await conn.execute(
                f"SELECT id, novel_id, chapter_num, translated_text, refined_text "
                f"FROM chapters {where} ORDER BY novel_id, chapter_num",
                params,
            )
        ).fetchall()

        total_rows_changed = 0
        total_delims_removed = 0

        for row in rows:
            updates: dict[str, str] = {}
            row_delims = 0
            for col in _COLUMNS:
                original = row[col]
                if not original or "*" not in original:
                    continue
                cleaned, count = enforce_balanced_emphasis(original)
                if count:
                    updates[col] = cleaned
                    row_delims += count

            if not updates:
                continue

            total_rows_changed += 1
            total_delims_removed += row_delims
            tag = f"novel {row['novel_id']} ch {row['chapter_num']}"
            print(f"\n=== {tag}: removed {row_delims} stray delimiter(s) ===")
            for col, cleaned in updates.items():
                for ob, oa in _changed_paragraphs(row[col], cleaned):
                    print(f"  [{col}]")
                    print(f"    - {ob!r}")
                    print(f"    + {oa!r}")

            if not dry_run:
                set_clause = ", ".join(f"{c} = ?" for c in updates)
                await conn.execute(
                    f"UPDATE chapters SET {set_clause} WHERE id = ?",
                    (*updates.values(), row["id"]),
                )

        if not dry_run:
            await conn.commit()

    verb = "would change" if dry_run else "changed"
    print(
        f"\n{verb} {total_rows_changed} chapter row(s); "
        f"{total_delims_removed} stray delimiter(s) {'to remove' if dry_run else 'removed'}."
    )
    if dry_run:
        print("(dry run — nothing written. Re-run without --dry-run to apply.)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--novel", type=int, default=None, help="restrict to one novel id")
    ap.add_argument(
        "--dry-run", action="store_true", help="report changes without writing"
    )
    args = ap.parse_args()
    asyncio.run(_run(args.novel, args.dry_run))


if __name__ == "__main__":
    main()
