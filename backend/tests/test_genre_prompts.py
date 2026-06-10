"""Tests for the genre-aware system-instruction builder.

Covers:
- base.md + per-genre overlay + per-genre examples compose into the right shape.
- Different genres produce different instructions (no collision).
- Custom style brief is appended (not replacement) when set.
- LRU cache returns the same string for identical inputs.
- Unknown genre falls back through the resolver without crashing.
- Worked examples are genre-specific.

Assertions are structural (the composed string contains the actual prompt-file
contents, in order), never pinned to prompt phrasing — the .md files are
edited constantly and a copy-edit must not break the suite.
"""

from __future__ import annotations

from backend.config import DEFAULT_GENRE
from backend.genres import GENRES, resolve_genre
from backend.services.translators.base import (
    _BASE_PROMPT_PATH,
    build_system_instruction,
    get_genre_overlay,
    get_worked_examples,
)

# ----- registry coverage -----

def test_all_registered_genres_have_overlay_files():
    """Every entry in backend/genres.py::GENRES must have its overlay file
    on disk. Forgetting to ship a file would cause runtime failures for
    novels set to that genre."""
    for key in GENRES:
        overlay = get_genre_overlay(key)
        assert overlay.strip(), f"genre {key!r} has empty overlay"


def test_all_registered_genres_have_examples_files():
    for key in GENRES:
        examples = get_worked_examples(key)
        assert examples.strip(), f"genre {key!r} has empty examples"


# ----- builder composition -----

def test_build_includes_base_and_overlay_and_examples():
    """The composed instruction must contain all three source files verbatim,
    in base → overlay → examples order, so we know the layering actually ran.
    Containment of the real file contents (not pinned phrases) keeps this
    green across prompt copy-edits."""
    instruction = build_system_instruction("xianxia")
    base = _BASE_PROMPT_PATH.read_text(encoding="utf-8")
    overlay = get_genre_overlay("xianxia")
    examples = get_worked_examples("xianxia")
    assert base in instruction
    assert overlay in instruction
    assert examples in instruction
    assert (
        instruction.index(base)
        < instruction.index(overlay)
        < instruction.index(examples)
    )


def test_build_xianxia_vs_generic_differ():
    """Different genres must produce different system instructions —
    otherwise per-novel routing is silently uniform."""
    xianxia = build_system_instruction("xianxia")
    generic = build_system_instruction("generic")
    assert xianxia != generic
    # The xianxia overlay ships in the xianxia composition only.
    xianxia_overlay = get_genre_overlay("xianxia")
    assert xianxia_overlay in xianxia
    assert xianxia_overlay not in generic


def test_build_each_genre_unique_against_generic():
    """Each genre's overlay must add genre-specific content. If a genre's
    final instruction equals 'generic', the overlay isn't loading."""
    generic = build_system_instruction("generic")
    for key in GENRES:
        if key == "generic":
            continue
        composed = build_system_instruction(key)
        assert composed != generic, (
            f"genre {key!r} composed instruction is identical to generic — "
            f"overlay file probably empty or not being loaded"
        )


def test_null_genre_falls_back_to_default():
    """A NULL genre input must resolve via DEFAULT_GENRE config (xianxia
    by default). The translator should never see an unresolvable genre."""
    null_resolved = build_system_instruction(None)
    xianxia = build_system_instruction("xianxia")
    # With DEFAULT_GENRE=xianxia these should compose identically.
    assert null_resolved == xianxia


def test_unknown_genre_falls_back_gracefully():
    """A bad genre key (e.g. an old DB value the user removed from the
    registry) must not crash — it should resolve through DEFAULT_GENRE.
    Defense in depth against DB / registry drift."""
    instruction = build_system_instruction("nonexistent_genre_xyz")
    assert instruction == build_system_instruction(DEFAULT_GENRE)


# ----- custom brief append behavior -----

def test_custom_brief_appended_not_replacing():
    """User-locked decision (2026-05-23): custom_style_brief APPENDS after
    the genre overlay; it does not replace it. The genre still bleeds
    through; the brief is layered on top as additional guidance."""
    no_brief = build_system_instruction("xianxia")
    with_brief = build_system_instruction("xianxia", "Make all dialogue sound sarcastic.")
    # Brief text must appear in the composed instruction.
    assert "Make all dialogue sound sarcastic" in with_brief
    # The xianxia overlay must STILL be present (not replaced), and the brief
    # is appended AFTER it.
    overlay = get_genre_overlay("xianxia")
    assert overlay in with_brief
    assert with_brief.index(overlay) < with_brief.index(
        "Make all dialogue sound sarcastic"
    )
    # The two instructions must differ — the brief is real input.
    assert no_brief != with_brief


def test_empty_custom_brief_treated_as_none():
    """Empty string or whitespace-only brief must NOT add the brief section,
    so the cache key matches the no-brief case. Otherwise empty briefs
    would cause silent cache misses."""
    no_brief = build_system_instruction("xianxia", None)
    assert build_system_instruction("xianxia", "") == no_brief
    # Whitespace-only must also normalize away — explicit test because the
    # earlier `if custom_brief` truthy check let "   " through, adding the
    # brief marker section with no actual content.
    assert build_system_instruction("xianxia", "   ") == no_brief
    assert build_system_instruction("xianxia", "\n\n\t") == no_brief
    assert build_system_instruction("xianxia", "  ") == no_brief


# ----- cache behavior -----

def test_repeated_call_returns_identical_string():
    """LRU cache must return the same composed string on repeat calls.
    If composition is non-deterministic, the LLM cache will miss every
    time and the user pays for every translation twice."""
    a = build_system_instruction("xianxia", "consistent brief")
    b = build_system_instruction("xianxia", "consistent brief")
    assert a == b
    # Identity-on-repeat is the strongest sign the cache hit (the cached
    # value is returned, not just a recomputed equal value).
    assert a is b


def test_different_brief_hashes_yield_different_results():
    """Two different briefs must produce two different cached entries."""
    a = build_system_instruction("xianxia", "brief one")
    b = build_system_instruction("xianxia", "brief two")
    assert a != b


# ----- genre resolver -----

def test_resolve_genre_unknown_falls_to_default():
    assert resolve_genre("definitely_not_a_genre", "xianxia") == "xianxia"


def test_resolve_genre_unknown_default_falls_to_generic():
    # If both the input and the default are unknown, the resolver must
    # land on generic as the last-resort safety net.
    assert resolve_genre("unknown1", "unknown2") == "generic"


def test_resolve_genre_known_input_wins():
    assert resolve_genre("xianxia", "generic") == "xianxia"


def test_resolve_genre_null_input_uses_default():
    assert resolve_genre(None, "wuxia") == "wuxia"
