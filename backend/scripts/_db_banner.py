"""Shared DB-target banner + write-confirmation for the dev / learn-from-edits
scripts (ingest_edited_chapter, diff_against_edit, retranslate_chapter,
ab_style_edits).

The recurring "two-database gotcha": these scripts act on whatever DB the
ambient config resolves to, which is the repo's dev `data/novels.db` unless
LN_TRANSLATOR_DATA / DB_PATH point the data root at the packaged app's live
`%APPDATA%\\LN-Translator` store. Running one against the wrong DB silently is
the failure mode. Make the target explicit at startup, and gate mutating runs
behind a typed confirmation, so the DB acted on is never implicit.

ASCII-only output (no box-drawing or dashes) so it encodes cleanly on a
cp1252 Windows console, and printed to stderr so a redirected stdout report
stays clean.
"""

from __future__ import annotations

import os
import sys

from backend.config import DB_PATH, PROJECT_ROOT

_DEV_DB = PROJECT_ROOT / "data" / "novels.db"


def _resolution() -> str:
    """How the ambient config arrived at DB_PATH, for the banner."""
    if os.getenv("DB_PATH"):
        return "DB_PATH env override"
    if os.getenv("LN_TRANSLATOR_DATA"):
        return "LN_TRANSLATOR_DATA override"
    return "ambient default"


def _is_dev_db() -> bool:
    """True when DB_PATH is the repo's own data/novels.db (the safe dev copy).
    Anything else is an override pointing elsewhere, e.g. the live store."""
    try:
        return DB_PATH.resolve() == _DEV_DB.resolve()
    except OSError:
        return str(DB_PATH) == str(_DEV_DB)


def print_db_banner(*, mutates: bool = False) -> None:
    """Print the resolved DB target and how it resolved, to stderr."""
    where = "repo dev DB" if _is_dev_db() else "NOT the repo dev DB (override/live)"
    exists = "yes" if DB_PATH.exists() else "NO (would be created)"
    bar = "=" * 64
    out = sys.stderr
    print(bar, file=out)
    print(f"  DB target : {DB_PATH}", file=out)
    print(f"  resolved  : {_resolution()}  ->  {where}", file=out)
    print(f"  exists    : {exists}", file=out)
    print(f"  mode      : {'WRITE (this run mutates the DB above)' if mutates else 'read-only'}", file=out)
    print(bar, file=out)


def confirm_db(action: str, *, assume_yes: bool = False) -> bool:
    """Confirm a mutating action against the banner's DB. Returns True to
    proceed. `assume_yes` (a --yes flag) bypasses the prompt for scripted runs."""
    if assume_yes:
        print(f"--yes: proceeding to {action}.", file=sys.stderr)
        return True
    resp = input(f"Type 'yes' to {action} in the DB above: ").strip().lower()
    if resp != "yes":
        print("Not confirmed; nothing written.", file=sys.stderr)
        return False
    return True
