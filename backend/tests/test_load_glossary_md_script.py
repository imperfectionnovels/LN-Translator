"""Direct unit tests for backend/scripts/load_glossary_md.py.

Pins the pure parsing logic of the glossary.md preset loader: the markdown
table-row parser, the section/subsection -> category router, and the full
file parser that threads section state across lines. No DB, no main().
"""

from __future__ import annotations

import pathlib
import sys

# Add backend/scripts to sys.path then plain-import the target so the coverage
# detector credits backend/scripts/load_glossary_md.py via a static import.
sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "scripts")
)
import load_glossary_md  # noqa: E402  (static import -> module-credit mechanic)

# ---------------------------------------------------------------------------
# parse_row
# ---------------------------------------------------------------------------

def test_parse_row_well_formed_with_notes():
    row = load_glossary_md.parse_row("| 灵气 | spirit qi | a resource | cultivation |")
    assert row is not None
    zh, en, notes = row
    assert zh == "灵气"
    assert en == "spirit qi"
    # extra cells beyond zh/en are joined with " | " into notes
    assert notes == "a resource | cultivation"


def test_parse_row_two_columns_no_notes():
    row = load_glossary_md.parse_row("| 剑心 | Sword Heart |")
    assert row == ("剑心", "Sword Heart", "")
    # notes is the empty string, not None, when there are no extra cells
    assert row[2] == ""


def test_parse_row_rejects_non_table_and_separator_and_header():
    # A line not starting with "|" is not a data row.
    assert load_glossary_md.parse_row("just some prose") is None
    # A separator row (only dashes) is skipped.
    assert load_glossary_md.parse_row("| --- | --- | --- |") is None
    # A header row whose first cell has no CJK is skipped.
    assert load_glossary_md.parse_row("| Chinese | English | Notes |") is None


def test_parse_row_rejects_empty_cells_and_too_few_columns():
    # zh present but en blank -> rejected.
    assert load_glossary_md.parse_row("| 灵气 |  |") is None
    # blank zh -> rejected.
    assert load_glossary_md.parse_row("|  | english |") is None
    # only one usable column -> rejected (len(cells) < 2).
    assert load_glossary_md.parse_row("| 灵气 |") is None


# ---------------------------------------------------------------------------
# category_for
# ---------------------------------------------------------------------------

def test_category_for_subsection_overrides_win_over_section_letter():
    # Subsection keyword "characters" forces "character" even under a non-A
    # section letter whose fallback would be "place".
    assert load_glossary_md.category_for("B", "Places", "Major Characters") == "character"
    # "treasure" subsection -> item, regardless of letter.
    assert load_glossary_md.category_for("A", "People", "Treasures") == "item"
    # "technique" subsection -> technique.
    assert load_glossary_md.category_for("C", "Misc", "Combat Techniques") == "technique"
    # "sect" subsection -> place.
    assert load_glossary_md.category_for("A", "People", "Sects & Clans") == "place"
    # "rank"/"title" subsection -> other.
    assert load_glossary_md.category_for("A", "People", "Ranks and Titles") == "other"


def test_category_for_idiom_section_and_core_entries_subsection():
    # §L idiom/chengyu section default -> idiom...
    assert load_glossary_md.category_for("L", "Idioms & Chengyu", "") == "idiom"
    # ...but the "Slang & Colloquialisms" subsection is NOT chengyu and falls
    # through to the §L-letter fallback ("other").
    assert (
        load_glossary_md.category_for("L", "Idioms & Chengyu", "Slang & Colloquialisms")
        == "other"
    )
    # The "Core Entries" subsection is matched directly as the chengyu archive.
    assert load_glossary_md.category_for("L", "Set Phrases", "Core Entries") == "idiom"


def test_category_for_top_level_section_letter_fallbacks():
    # No subsection / generic title -> the letter fallback table applies.
    assert load_glossary_md.category_for("A", "Cast", "") == "character"
    assert load_glossary_md.category_for("B", "Geography", "") == "place"
    assert load_glossary_md.category_for("E", "Arts", "") == "technique"
    assert load_glossary_md.category_for("G", "Goods", "") == "item"
    # An unmapped letter (C/D/I/J/K) falls through to "other".
    assert load_glossary_md.category_for("C", "Concepts", "") == "other"


# ---------------------------------------------------------------------------
# parse_file (threads section/subsection state across lines)
# ---------------------------------------------------------------------------

def _write(tmp_path, body: str) -> pathlib.Path:
    p = tmp_path / "glossary.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_parse_file_threads_section_state_and_categories(tmp_path):
    md = "\n".join(
        [
            "# Glossary",
            "",
            "## How to Use",
            "| ignored | because no letter section yet |",
            "",
            "## A. Characters",
            "| 张三 | Zhang San | protagonist |",
            "",
            "### Treasures",
            "| 玄铁剑 | Black Iron Sword |",
            "",
            "## L. Idioms & Chengyu",
            "| 画蛇添足 | gild the lily |",
        ]
    )
    rows = load_glossary_md.parse_file(_write(tmp_path, md))
    # The pre-section row under "## How to Use" is dropped (no section_letter).
    assert ("张三", "Zhang San", "character", "protagonist") in rows
    # Subsection "Treasures" overrides the section-A default to "item".
    assert ("玄铁剑", "Black Iron Sword", "item", "") in rows
    # The chengyu row under §L resolves to "idiom".
    assert ("画蛇添足", "gild the lily", "idiom", "") in rows
    # Exactly the three valid data rows, nothing from the How-to-Use block.
    assert len(rows) == 3


def test_parse_file_skips_backlog_section_and_header_rows(tmp_path):
    md = "\n".join(
        [
            "## A. Characters",
            "| Chinese | English | Notes |",
            "| --- | --- | --- |",
            "| 李四 | Li Si |",
            "",
            "## Backlog",
            "| 废弃 | discarded | should not load |",
        ]
    )
    rows = load_glossary_md.parse_file(_write(tmp_path, md))
    # Header + separator rows are filtered; only the real CJK data row survives.
    assert rows == [("李四", "Li Si", "character", "")]
    # The Backlog section is skipped entirely (in_backlog gate).
    assert all(zh != "废弃" for zh, *_ in rows)


def test_parse_file_empty_input_yields_no_rows(tmp_path):
    assert load_glossary_md.parse_file(_write(tmp_path, "")) == []


def test_valid_categories_constant_covers_router_outputs():
    # Every category the router can emit must be in the whitelist, otherwise
    # parse_file would silently coerce it to "other".
    produced = {
        load_glossary_md.category_for("A", "Cast", ""),
        load_glossary_md.category_for("B", "Geo", ""),
        load_glossary_md.category_for("E", "Arts", ""),
        load_glossary_md.category_for("G", "Goods", ""),
        load_glossary_md.category_for("L", "Idioms", ""),
        load_glossary_md.category_for("C", "Misc", ""),
    }
    assert produced <= load_glossary_md.VALID_CATEGORIES
    assert "idiom" in load_glossary_md.VALID_CATEGORIES
    assert "character" in load_glossary_md.VALID_CATEGORIES
