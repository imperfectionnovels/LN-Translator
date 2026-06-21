"""Re-apply the current glossary's deterministic term-casing to a novel's
already-committed chapters, with NO LLM. Dry-run by default (reports what
would change, writes nothing); --apply writes after dumping a before-image.

Why this exists
---------------
`enforce_locked_term_casing` runs at translate time against the glossary as
it stood then. A term locked or re-cased later never propagates back to
earlier chapters (the project's deliberate forward-only stance), so the back
catalog drifts from the current glossary. Example on novel 2: the locked
"Dao of Dual Cultivation" is in zero current chapters, while the lowercase
"Dao of dual cultivation" sits in eight recent ones (ch1037-1449). This pass
closes that gap deterministically, no model call.

Scope and safety
----------------
Casing only, via the three glossary-driven, length-preserving fixups the
queue already runs: `enforce_locked_term_casing`,
`enforce_lowercase_locked_terms`, `enforce_stem_branch_casing`. It never
substitutes a different word and never inserts an absent term, so it cannot
introduce the term-substitution damage class. The only change is the casing
of glossary terms already present in the text. `--apply` dumps a full
before-image JSON first, so every change is reversible.

Run hygiene first: if a defect entry (e.g. a short common word locked to a
niche rendering) is atomic-cased, fix it via the fix-glossary skill before
applying here, so this pass never stamps a bad casing corpus-wide.

DB selection follows the app (see backend.scripts._db_banner): dev
data/novels.db by default; set LN_TRANSLATOR_DATA to target the live store.
Use --apply only with the app closed (SQLite has a single writer).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import string
import sys
from collections import Counter
from dataclasses import dataclass, field

from backend.config import USER_DATA_ROOT
from backend.db import open_conn
from backend.scripts._db_banner import confirm_db, print_db_banner
from backend.services import glossary as glossary_svc
from backend.services.text_fixups import (
    enforce_locked_term_casing,
    enforce_lowercase_locked_terms,
    enforce_stem_branch_casing,
)

_WORD_CHARS = set(string.ascii_letters + "'’’-")


def apply_casing_chain(text: str, glossary) -> tuple[str, int]:
    """Run the three glossary-driven casing fixups in the queue's order.

    All three preserve length, so the result is char-aligned with the input
    (the diff sampler relies on this). Returns (new_text, total_replacements).
    """
    total = 0
    out, n = enforce_locked_term_casing(text, glossary)
    total += n
    out, n = enforce_lowercase_locked_terms(out, glossary)
    total += n
    out, n = enforce_stem_branch_casing(out)
    total += n
    return out, total


def _word_pair(old: str, new: str, i: int, j: int) -> tuple[str, str]:
    """Expand a [i, j) diff run to its surrounding word on both sides.

    old and new are the same length (casing-only), so the same indices apply
    to both. Returns (old_word, new_word) for fix-frequency aggregation."""
    a = i
    while a > 0 and old[a - 1] in _WORD_CHARS:
        a -= 1
    b = j
    while b < len(old) and old[b] in _WORD_CHARS:
        b += 1
    return old[a:b], new[a:b]


def diff_runs(old: str, new: str, max_runs: int = 50) -> list[tuple[int, int]]:
    """[start, end) index runs where old and new differ (same-length inputs)."""
    if len(old) != len(new):
        return []
    runs: list[tuple[int, int]] = []
    i, n = 0, len(old)
    while i < n and len(runs) < max_runs:
        if old[i] != new[i]:
            j = i
            while j < n and old[j] != new[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def _window(text: str, i: int, j: int, pad: int = 22) -> str:
    a = max(0, i - pad)
    b = min(len(text), j + pad)
    return text[a:b].replace("\n", " ")


@dataclass
class Report:
    chapters_scanned: int = 0
    chapters_changed: int = 0
    total_replacements: int = 0
    upcase: int = 0
    downcase: int = 0
    fixes: Counter = field(default_factory=Counter)  # (old_word -> new_word)
    samples: list = field(default_factory=list)  # (chapter_num, field, old_win, new_win)


def analyze_change(report: Report, chapter_num: int, field_name: str, old: str, new: str) -> None:
    """Record one changed text field into the report (no writes)."""
    runs = diff_runs(old, new)
    for (i, j) in runs:
        ow, nw = _word_pair(old, new, i, j)
        report.fixes[(ow, nw)] += 1
        # Classify direction by uppercase-letter count of the changed word.
        if sum(c.isupper() for c in nw) > sum(c.isupper() for c in ow):
            report.upcase += 1
        else:
            report.downcase += 1
    if runs and len(report.samples) < 30:
        i, j = runs[0]
        report.samples.append(
            (chapter_num, field_name, _window(old, i, j), _window(new, i, j))
        )


async def _load(novel_id: int):
    async with open_conn() as conn:
        glossary = await glossary_svc.list_for_novel(conn, novel_id)
        cur = await conn.execute(
            "SELECT id, chapter_num, translated_text, refined_text "
            "FROM chapters WHERE novel_id = ? AND status = 'done' "
            "ORDER BY chapter_num",
            (novel_id,),
        )
        rows = await cur.fetchall()
    chapters = [
        {
            "id": r["id"],
            "chapter_num": r["chapter_num"],
            "translated_text": r["translated_text"],
            "refined_text": r["refined_text"],
        }
        for r in rows
    ]
    return glossary, chapters


def _print_report(report: Report) -> None:
    print("=" * 64)
    print(
        f"  re-apply casing preview: {report.chapters_changed}/"
        f"{report.chapters_scanned} chapters would change, "
        f"{report.total_replacements} casing replacements"
    )
    print(f"  direction: {report.upcase} up-cased, {report.downcase} down-cased")
    print("=" * 64)
    if report.fixes:
        print("\nTop recurring casing fixes (old -> new : count):")
        for (ow, nw), c in report.fixes.most_common(25):
            print(f"    {c:>4}  {ow!r} -> {nw!r}")
    if report.samples:
        print("\nSample changes (chapter / field / before / after):")
        for ch, fld, ow, nw in report.samples[:15]:
            print(f"    ch{ch} [{fld}]")
            print(f"        - {ow}")
            print(f"        + {nw}")


async def _run(novel_id: int, apply: bool, assume_yes: bool) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print_db_banner(mutates=apply)

    glossary, chapters = await _load(novel_id)
    report = Report()
    pending: list[tuple[int, str, str]] = []  # (chapter_id, field, new_text)
    backup: dict[str, dict] = {}

    for ch in chapters:
        report.chapters_scanned += 1
        changed_here = False
        for field_name in ("translated_text", "refined_text"):
            old = ch[field_name]
            if not old:
                continue
            new, _count = apply_casing_chain(old, glossary)
            if new == old:
                continue
            changed_here = True
            report.total_replacements += sum(j - i for (i, j) in diff_runs(old, new))
            analyze_change(report, ch["chapter_num"], field_name, old, new)
            pending.append((ch["id"], field_name, new))
            backup.setdefault(str(ch["id"]), {})[field_name] = old
        if changed_here:
            report.chapters_changed += 1

    _print_report(report)

    if not apply:
        print("\nDRY RUN: nothing written. Re-run with --apply to write.")
        return

    if not pending:
        print("\nNothing to apply.")
        return

    if not confirm_db(
        f"re-case {len(pending)} field(s) across {report.chapters_changed} chapters",
        assume_yes=assume_yes,
    ):
        return

    backup_path = USER_DATA_ROOT / f"reapply_casing_backup_novel{novel_id}.json"
    with open(backup_path, "w", encoding="utf-8") as fh:
        json.dump(backup, fh, ensure_ascii=False)
    print(f"before-image written: {backup_path}")

    async with open_conn() as conn:
        for chapter_id, field_name, new in pending:
            await conn.execute(
                f"UPDATE chapters SET {field_name} = ? WHERE id = ?",
                (new, chapter_id),
            )
        await conn.commit()
    print(f"applied: {len(pending)} field(s) updated.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-apply current glossary term-casing to a novel's chapters."
    )
    ap.add_argument("--novel", type=int, required=True, help="novel id")
    ap.add_argument(
        "--apply", action="store_true", help="write changes (default: dry run)"
    )
    ap.add_argument(
        "--yes", action="store_true", help="skip the apply confirmation prompt"
    )
    args = ap.parse_args()
    asyncio.run(_run(args.novel, args.apply, args.yes))


if __name__ == "__main__":
    main()
