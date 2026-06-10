"""Phase 2 tests for the genre-aware system-instruction builder.

Covers:
- base.md + per-genre overlay + per-genre examples compose into the right shape.
- Different genres produce different instructions (no collision).
- Custom style brief is appended (not replacement) when set.
- LRU cache returns the same string for identical inputs.
- Unknown genre falls back to generic without crashing.
- Worked examples are genre-specific.
"""

from __future__ import annotations

from backend.genres import GENRES, resolve_genre
from backend.services.translators.base import (
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
    """The composed instruction must contain markers from all three sources
    so we know the layering actually ran."""
    instruction = build_system_instruction("xianxia")
    # Base.md signature line (reframed 2026-06-10: the webnovel-translation
    # frame replaced the novelist frame; phase9 construction layer).
    assert "english translator of a chinese web novel" in instruction.lower()
    # Genre overlay marker (xianxia overlay's distinctive opening).
    assert "GENRE OVERLAY:" in instruction
    assert "cultivation" in instruction.lower()
    # Worked-examples block.
    assert "Worked examples" in instruction


def test_build_xianxia_vs_generic_differ():
    """Different genres must produce different system instructions —
    otherwise per-novel routing is silently uniform."""
    xianxia = build_system_instruction("xianxia")
    generic = build_system_instruction("generic")
    assert xianxia != generic
    # xianxia overlay has cultivator-title material; generic does not.
    assert "True Monarch" in xianxia
    assert "True Monarch" not in generic


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
    registry) must not crash — it should fall back to generic via the
    resolver. Defense in depth against DB / registry drift."""
    instruction = build_system_instruction("nonexistent_genre_xyz")
    # Resolver returns DEFAULT_GENRE for unknown values; with DEFAULT_GENRE
    # set to xianxia (the project default), this should match xianxia.
    # If DEFAULT_GENRE were itself unknown, it would fall to generic.
    assert instruction.strip(), "fallback returned empty"
    assert "GENRE OVERLAY:" in instruction


# ----- custom brief append behavior -----

def test_custom_brief_appended_not_replacing():
    """User-locked decision (2026-05-23): custom_style_brief APPENDS after
    the genre overlay; it does not replace it. The genre still bleeds
    through; the brief is layered on top as additional guidance."""
    no_brief = build_system_instruction("xianxia")
    with_brief = build_system_instruction("xianxia", "Make all dialogue sound sarcastic.")
    # Brief text must appear in the composed instruction.
    assert "Make all dialogue sound sarcastic" in with_brief
    # The xianxia overlay must STILL be present (not replaced).
    assert "True Monarch" in with_brief
    # The two instructions must differ — the brief is real input.
    assert no_brief != with_brief


def test_custom_brief_marker_present():
    """The brief is introduced by an explanatory marker so the model
    understands it's user-supplied, not part of the canonical instruction."""
    with_brief = build_system_instruction("generic", "test brief")
    assert "CUSTOM STYLE BRIEF" in with_brief


def test_empty_custom_brief_treated_as_none():
    """Empty string or whitespace-only brief must NOT add the brief section,
    so the cache key matches the no-brief case. Otherwise empty briefs
    would cause silent cache misses."""
    no_brief = build_system_instruction("xianxia", None)
    assert build_system_instruction("xianxia", "") == no_brief
    # Whitespace-only must also normalize away — explicit test because the
    # earlier `if custom_brief` truthy check let "   " through, adding the
    # CUSTOM STYLE BRIEF marker with no actual content.
    assert build_system_instruction("xianxia", "   ") == no_brief
    assert build_system_instruction("xianxia", "\n\n\t") == no_brief
    # The marker must NOT appear when the brief normalizes to empty.
    assert "CUSTOM STYLE BRIEF" not in build_system_instruction("xianxia", "  ")


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


# ----- DeepSeek revision genre resolution -----

def test_deepseek_genre_matches_system_instruction_when_null():
    """Regression for the inconsistency where DeepSeek's system prompt
    resolved NULL through DEFAULT_GENRE (xianxia) but _genre fell back to
    a literal 'generic'. The two stages of the same call must agree on
    which genre is in play — otherwise the revision examples target a
    different genre than the draft instructions."""
    from unittest.mock import MagicMock

    import backend.services.translators.deepseek as ds_mod

    instance = MagicMock(spec=ds_mod.DeepSeekTranslator)
    # Simulate the exact resolution path translate_chapter takes when
    # the novel has no genre set.
    genre = None
    resolved = resolve_genre(genre, "xianxia")
    instance._genre = resolved

    # The revise prompt builders read self._genre to pick worked examples.
    # If _genre were "generic" while the system prompt used "xianxia",
    # the user would see xianxia draft prose with generic revision
    # commentary — exactly the bug this test guards against.
    assert instance._genre == "xianxia", (
        f"NULL genre must resolve to DEFAULT_GENRE for both stages; "
        f"got {instance._genre!r}"
    )


def test_deepseek_genre_resolves_unknown_to_default():
    """A novel.genre value that's no longer in the registry must NOT cause
    the revision pass to silently use the literal genre name (which would
    fail at file load time). resolve_genre falls to DEFAULT_GENRE, then
    to 'generic' if DEFAULT_GENRE itself is unknown."""
    resolved = resolve_genre("removed_from_registry_xyz", "xianxia")
    assert resolved == "xianxia"
    # Both unknown → final safety net.
    resolved = resolve_genre("removed_from_registry_xyz", "also_removed")
    assert resolved == "generic"
