"""One-shot seed loader for the cross-novel "house lexicon".

Inserts a small set of culturally-loaded loan-concepts into the GLOBAL glossary
(`global_glossary_entries`) so the translator stops flattening them to a generic
English gloss across every novel. A global entry overrides base.md's frozen-idiom
flatten rule for all novels with zero prompt growth: 面子 stays "face" (not
"standing"), 招安 is "win over" (not "pacify", which loses the turned-to-serve-you
sense).

Idempotent: `INSERT ... ON CONFLICT(term_zh) DO NOTHING` never clobbers a user
edit or re-inserts a duplicate. Run once after merge:

    python -m backend.scripts.load_house_lexicon
"""

from __future__ import annotations

import sqlite3

from backend.config import DB_PATH

# (term_zh, term_en, category, notes). Category "idiom" keeps the rendering
# lowercase per the casing policy (only the `idiom` category is lowercase).
HOUSE_LEXICON: list[tuple[str, str, str, str]] = [
    (
        "面子",
        "face",
        "idiom",
        'social face / prestige the wuxiaworld register keeps as "face"; '
        'not "standing" / "prestige", which flatten the loan-concept',
    ),
    (
        "气运",
        "fortune",
        "idiom",
        'luck-as-destiny; "fortune", or "luck" where it reads better',
    ),
    (
        "缘分",
        "fate / affinity",
        "idiom",
        "the karmic tie that draws people together; render whichever side fits",
    ),
    (
        "招安",
        "win over",
        "idiom",
        "co-opt an enemy / rebel into one's own service; not \"pacify\", "
        "which loses the turned-to-serve-you sense",
    ),
]


def load(db_path=None) -> tuple[int, int]:
    """Insert the house-lexicon rows into `global_glossary_entries`.

    Idempotent via `ON CONFLICT(term_zh) DO NOTHING` — re-running never clobbers
    a user edit. Returns ``(inserted, skipped)``."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        inserted = 0
        skipped = 0
        for term_zh, term_en, category, notes in HOUSE_LEXICON:
            before = conn.total_changes
            cur.execute(
                "INSERT INTO global_glossary_entries "
                "(term_zh, term_en, category, notes) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(term_zh) DO NOTHING",
                (term_zh, term_en, category, notes or None),
            )
            if conn.total_changes > before:
                inserted += 1
            else:
                skipped += 1
        conn.commit()
    finally:
        conn.close()
    return inserted, skipped


if __name__ == "__main__":
    ins, skp = load()
    print(f"inserted {ins} / skipped {skp}")
