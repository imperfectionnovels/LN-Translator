"""One-shot loader: parses data/glossary.md and inserts rows into the
glossary_entries table for a chosen novel_id.

Run: python -m backend.scripts.load_glossary_md <novel_id>
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

from backend.config import DB_PATH, PROJECT_ROOT

GLOSSARY_MD = PROJECT_ROOT / "data" / "glossary.md"

CJK_RE = re.compile(r"[㐀-鿿]")
H2_RE = re.compile(r"^##\s+([A-Z])\.\s+(.+?)\s*$")
H3_RE = re.compile(r"^###\s+(.+?)\s*$")

VALID_CATEGORIES = {"character", "technique", "item", "place", "other", "idiom"}


def category_for(section_letter: str, section_title: str, subsection: str) -> str:
    sub = (subsection or "").lower()
    sec = (section_title or "").lower()

    # Subsection-level overrides (most specific).
    if any(k in sub for k in ("character", "personnel", "people")):
        return "character"
    if any(
        k in sub
        for k in (
            "beasts",
            "demons",
            "ghosts",
            "monsters",
            "entities",
            "beings",
            "mythological",
        )
    ):
        return "character"
    if any(
        k in sub
        for k in (
            "treasure",
            "artifact",
            "pill",
            "medicine",
            "herb",
            "ingredient",
            "equipment",
            "currency",
            "resource",
            "material",
            "soldier",
            "weapon",
            "talisman",
            "fundamentals",
            "formation treasure",
            "item",
        )
    ):
        return "item"
    if any(
        k in sub
        for k in (
            "technique",
            "spell",
            "art",
            "scripture",
            "ability",
            "abilities",
            "mysteries",
            "attack",
            "defens",
            "formation",
        )
    ):
        return "technique"
    if any(
        k in sub
        for k in (
            "sect",
            "location",
            "realm",
            "dimension",
            "site",
            "feature",
            "building",
            "facility",
            "palace",
            "underworld",
            "court",
        )
    ):
        return "place"
    if any(
        k in sub
        for k in (
            "title",
            "address",
            "rank",
            "hierarchy",
            "honorific",
            "leadership",
        )
    ):
        return "other"

    # Idioms section (§L "Idioms, Set Phrases & Slang") — default its
    # entries to "idiom" so the idiom guardrails (services/glossary.py,
    # text_fixups.py / text_observers.py, recase_glossary.py) actually
    # fire. The "Slang & Colloquialisms" subsection within §L is not
    # chengyu and falls through to the §L-letter fallback ("other").
    if ("idiom" in sec or "chengyu" in sec) and not (
        "slang" in sub or "colloquial" in sub
    ):
        return "idiom"
    # §L's "Core Entries" subsection is the chengyu archive whose section-
    # title hint ("Core Entries") doesn't contain "idiom" or "chengyu" —
    # match the subsection text directly. Without this branch, ~150
    # chengyu entries load as `other` and the idiom guardrails miss them.
    if "core entries" in sub or "idiom" in sub or "chengyu" in sub:
        return "idiom"

    # Top-level section fallback.
    if section_letter == "A":
        return "character"
    if section_letter == "B":
        return "place"
    if section_letter == "E":
        return "technique"
    if section_letter == "F":
        return "technique"
    if section_letter == "G":
        return "item"
    if section_letter == "H":
        return "item"
    # C, D, I, J, K, L → other by default
    return "other"


def parse_row(line: str) -> tuple[str, str, str] | None:
    """Parse a single table row. Returns (zh, en, notes) or None if not a data row."""
    if not line.startswith("|"):
        return None
    cells = [c.strip() for c in line.split("|")]
    # Split adds empty strings at both ends for well-formed rows.
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    if len(cells) < 2:
        return None
    # Separator row: only dashes / empty.
    if all(c in ("", "-") or set(c) <= {"-"} for c in cells):
        return None
    zh, en, *rest = cells
    if not zh or not en:
        return None
    # Skip header rows: zh must contain CJK characters.
    if not CJK_RE.search(zh):
        return None
    notes = " | ".join(r for r in rest if r)
    return zh, en, notes


def parse_file(path: Path) -> list[tuple[str, str, str, str]]:
    """Returns list of (zh, en, category, notes)."""
    out: list[tuple[str, str, str, str]] = []
    section_letter = ""
    section_title = ""
    subsection = ""
    in_backlog = False

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        m2 = H2_RE.match(line)
        if m2:
            section_letter, section_title = m2.group(1), m2.group(2)
            subsection = ""
            in_backlog = False
            continue
        if line.startswith("## "):
            # Non-letter ## like "## How to Use" or "## Backlog".
            in_backlog = "backlog" in line.lower()
            section_letter = ""
            section_title = line[3:].strip()
            subsection = ""
            continue
        m3 = H3_RE.match(line)
        if m3:
            subsection = m3.group(1)
            continue
        if in_backlog or not section_letter:
            continue
        row = parse_row(line)
        if row is None:
            continue
        zh, en, notes = row
        cat = category_for(section_letter, section_title, subsection)
        if cat not in VALID_CATEGORIES:
            cat = "other"
        out.append((zh, en, cat, notes))
    return out


def load(novel_id: int) -> None:
    if not GLOSSARY_MD.exists():
        raise SystemExit(f"glossary.md not found at {GLOSSARY_MD}")
    entries = parse_file(GLOSSARY_MD)
    print(f"Parsed {len(entries)} rows from glossary.md")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # Confirm novel exists.
    row = cur.execute(
        "SELECT id, title FROM novels WHERE id = ?", (novel_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"novel_id {novel_id} not found in DB")
    print(f"Loading into novel id={row[0]} title={row[1]!r}")

    before = cur.execute(
        "SELECT COUNT(*) FROM glossary_entries WHERE novel_id = ?", (novel_id,)
    ).fetchone()[0]

    inserted = 0
    skipped_dup = 0
    for zh, en, cat, notes in entries:
        try:
            cur.execute(
                "INSERT INTO glossary_entries "
                "(novel_id, term_zh, term_en, category, notes, auto_detected, locked) "
                "VALUES (?, ?, ?, ?, ?, 0, 1)",
                (novel_id, zh, en, cat, notes or None),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped_dup += 1

    conn.commit()
    after = cur.execute(
        "SELECT COUNT(*) FROM glossary_entries WHERE novel_id = ?", (novel_id,)
    ).fetchone()[0]
    conn.close()

    print(
        f"Inserted {inserted} new, skipped {skipped_dup} duplicates. "
        f"Glossary went {before} -> {after}."
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m backend.scripts.load_glossary_md <novel_id>")
    load(int(sys.argv[1]))
