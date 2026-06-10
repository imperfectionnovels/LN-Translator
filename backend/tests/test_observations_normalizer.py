"""Direct unit tests for the observations normalizer
(backend/services/observations.py).

The detect_* observers and the QA-dashboard HTTP endpoints are covered
elsewhere (test_phase3_observers.py, test_observations_routes.py), but the
worker-string-to-kind mapping (_kind_for), the list normalizer
(normalize_observer_outputs), the severity tiering (severity_tier_for), and
the disabled-observers parser (parse_disabled_observers) were only exercised
transitively. These pin them directly so a change to a prefix string or the
fail-open contract is caught at the unit level.
"""

from __future__ import annotations

import pytest

from backend.services.observations import (
    _kind_for,
    normalize_observer_outputs,
    parse_disabled_observers,
    severity_tier_for,
)


@pytest.mark.parametrize(
    "message, expected_kind",
    [
        ("missing locked glossary term '昂霄' → 'Soaring Firmament'", "missing_glossary_term"),
        ("missing title glossary term '青云' → 'Azure Cloud'", "missing_title_glossary_term"),
        ("mt-texture tics: 'couldn't help but'", "mt_texture"),
        ("Double possessive on a name: 'Li Ming's's'", "double_possessive"),
        ("Mid-sentence paragraph break after 'and then'", "mid_sentence_paragraph_break"),
        ("Intensifier inflation on 'Heaven': 'extremely Heaven'", "intensifier_inflation"),
        ("Predicate loss near '元婴': dropped verb", "glossary_predicate_loss"),
    ],
)
def test_kind_for_known_prefixes(message: str, expected_kind: str) -> None:
    """Each representative observer-output prefix maps to its structured
    kind. These are the exact prefixes the queue worker emits."""
    assert _kind_for(message) == expected_kind


def test_kind_for_unknown_prefix_falls_back_to_observation() -> None:
    """A message that matches no prefix gets the generic 'observation' kind,
    so storage stays well-defined even if a new observer is added without
    updating the prefix table."""
    assert _kind_for("some brand-new observer we haven't mapped yet") == "observation"


def test_normalize_maps_kinds_and_sets_warn_severity() -> None:
    """normalize_observer_outputs turns each raw string into one record,
    deriving the kind from the prefix, stamping severity='warn', and leaving
    paragraph_index None (v1 chapter-level)."""
    raw = [
        "missing locked glossary term '昂霄' → 'Soaring Firmament'",
        "mt-texture tics: 'couldn't help but'",
    ]
    out = normalize_observer_outputs(raw)
    assert [o.kind for o in out] == ["missing_glossary_term", "mt_texture"]
    assert all(o.severity == "warn" for o in out)
    assert all(o.paragraph_index is None for o in out)
    # Excerpt is the stripped original message.
    assert out[0].excerpt == "missing locked glossary term '昂霄' → 'Soaring Firmament'"


def test_normalize_skips_blank_and_whitespace_messages() -> None:
    """Empty / whitespace-only entries are dropped, not turned into rows."""
    out = normalize_observer_outputs(["", "   ", "Predicate loss near '元婴': dropped verb"])
    assert len(out) == 1
    assert out[0].kind == "glossary_predicate_loss"


def test_severity_tier_for_semantic_vs_stylistic() -> None:
    """Semantic kinds (possible meaning loss) tier 'semantic'; stylistic and
    unknown kinds tier 'stylistic' (the quieter default)."""
    assert severity_tier_for("missing_glossary_term") == "semantic"
    assert severity_tier_for("glossary_predicate_loss") == "semantic"
    assert severity_tier_for("mt_texture") == "stylistic"
    assert severity_tier_for("double_possessive") == "stylistic"
    # Unknown kind defaults to the less-alarming tier.
    assert severity_tier_for("observation") == "stylistic"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, set()),
        ("", set()),
        ('["mt_texture", "double_possessive"]', {"mt_texture", "double_possessive"}),
        ("[]", set()),
        # Fail-open on malformed JSON.
        ("not json at all", set()),
        ("{not: valid}", set()),
        # Non-list JSON is ignored (fail-open).
        ('"mt_texture"', set()),
        ('{"mt_texture": true}', set()),
        # Non-string list members are filtered out.
        ('["mt_texture", 5, null]', {"mt_texture"}),
    ],
)
def test_parse_disabled_observers(raw, expected) -> None:
    """The shared mute parser fails open to the empty set on NULL / empty /
    malformed / non-list input, and filters non-string members."""
    assert parse_disabled_observers(raw) == expected
