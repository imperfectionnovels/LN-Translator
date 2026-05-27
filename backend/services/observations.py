"""Normalizer between the raw detect_* observer outputs and the
chapter_observations storage shape.

The QA dashboard (Initiative 1) persists deterministic observer hits as
discrete rows so the reader can render them per-chapter, with dismissal,
and so the library can show an aggregate badge.

Observer functions in `text_observers.py` return `list[str]` — for some
detectors each list item is one flagged span, for others it's one combined
advisory message covering several spans. The normalizer absorbs that
diversity into a uniform `NormalizedObservation` record. Paragraph indexing
is best-effort: v1 leaves it NULL for every observer (the reader sidebar
hides the jump affordance when it's NULL) and a future revision will
extend individual observers to expose match offsets.

The normalizer is intentionally NOT responsible for calling observers — the
queue worker does that and hands the raw outputs in. Keeping the call sites
in the worker keeps the existing chapter-level log line intact (single
source for the 'logged, not retried' telemetry) while this layer owns
storage shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class NormalizedObservation:
    """One row destined for chapter_observations.

    `kind` is the observer name (or one of the two implicit kinds —
    'translation_degraded', 'glossary_merge_error') so the frontend can
    style per-kind. `paragraph_index` is None until an observer is
    extended to expose match offsets; the reader sidebar treats None as
    chapter-level.
    """
    kind: str
    severity: str  # 'warn' | 'info'
    paragraph_index: int | None
    excerpt: str


# F26 (2026-05-25): observation severity tiering. Semantic observers
# (missing locked glossary terms, malformed compounds, predicate loss,
# implicit translation_degraded + glossary_merge_error) are signals of
# possible meaning loss; the user usually wants to act on them. Stylistic
# observers (MT-texture tics, double possessives, mid-sentence breaks,
# intensifier inflation, locked-idiom grammar) are surface-prose
# advisories — often false positives and lower priority. The library
# badge splits into '⚠ N semantic / ⓘ N stylistic' so high-priority
# issues don't get drowned in stylistic noise.
SEMANTIC_KINDS = frozenset({
    "missing_glossary_term",
    "missing_title_glossary_term",
    "malformed_compound",
    "glossary_predicate_loss",
    "translation_degraded",
    "glossary_merge_error",
    "tm_inconsistency",
})
STYLISTIC_KINDS = frozenset({
    "mt_texture",
    "double_possessive",
    "locked_idiom_grammar",
    "mid_sentence_paragraph_break",
    "intensifier_inflation",
})


def severity_tier_for(kind: str) -> str:
    """Return 'semantic' or 'stylistic' for the given observation kind.
    Unknown kinds default to 'stylistic' (less alarming) — a new
    observer added without updating these tables surfaces as a quieter
    badge rather than spuriously alarming."""
    if kind in SEMANTIC_KINDS:
        return "semantic"
    return "stylistic"


# Map from the queue worker's observation-string prefix to a structured
# kind name. The worker builds strings like "missing locked glossary term
# '昂霄' → 'Soaring Firmament'" or "malformed compound 'early Foundation
# Establishment clan'" — we recover the kind from the prefix.
_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("missing locked glossary term", "missing_glossary_term"),
    ("missing title glossary term", "missing_title_glossary_term"),
    ("malformed compound", "malformed_compound"),
    ("mt-texture tics:", "mt_texture"),
    ("Double possessive on a name:", "double_possessive"),
    ("Locked idiom grammar issue:", "locked_idiom_grammar"),
    ("Mid-sentence paragraph break", "mid_sentence_paragraph_break"),
    ("Intensifier inflation", "intensifier_inflation"),
    ("Predicate loss near", "glossary_predicate_loss"),
)


def _kind_for(message: str) -> str:
    """Match a raw observer-output string to a kind label.

    Falls back to 'observation' when no prefix matches — keeps storage
    well-defined even if a new observer is added without updating the
    prefix table."""
    for prefix, kind in _KIND_PREFIXES:
        if message.startswith(prefix):
            return kind
    return "observation"


def normalize_observer_outputs(
    raw_messages: Iterable[str],
) -> list[NormalizedObservation]:
    """Convert the queue worker's flat list of observation strings into
    storage-ready records. One string → one record.

    Severity is 'warn' for every detect_* output (the observers only fire
    on actionable issues; nothing they emit is purely informational).
    Paragraph index is None for v1 — a future revision can extend the
    individual observers to return match offsets and feed them through
    this layer."""
    out: list[NormalizedObservation] = []
    for msg in raw_messages:
        if not msg or not msg.strip():
            continue
        out.append(
            NormalizedObservation(
                kind=_kind_for(msg),
                severity="warn",
                paragraph_index=None,
                excerpt=msg.strip(),
            )
        )
    return out


def implicit_observation_translation_degraded() -> NormalizedObservation:
    """Synthesize the implicit 'translation_degraded' observation.

    The queue worker calls this when result.degraded is True so the panel
    can render the fallback-path warning uniformly with detect_* hits."""
    return NormalizedObservation(
        kind="translation_degraded",
        severity="warn",
        paragraph_index=None,
        excerpt=(
            "Translation came from the plain-text fallback path — the "
            "translator's structured envelope failed to parse twice and "
            "the body was salvaged as raw text. Re-translation may "
            "produce a cleaner result."
        ),
    )


def implicit_observation_tm_inconsistency(
    source_text: str,
    paragraph_index: int,
    renderings: list[str],
) -> NormalizedObservation:
    """Initiative 5 — synthesize a TM-inconsistency observation.

    Fired by the queue worker when this chapter's freshly-populated TM
    rows include a source paragraph whose previously-stored renderings
    don't all match this chapter's. The user can decide whether to
    standardize via Initiative 4's find/replace.

    The excerpt summarizes the renderings inline so the panel can render
    without a second fetch. Truncated when the rendering set is long;
    the dedicated /tm/inconsistencies endpoint has the full data when
    the user wants to drill in."""
    short_source = source_text.strip()
    if len(short_source) > 80:
        short_source = short_source[:77] + "…"
    # Show up to 3 distinct renderings inline. More than that is rare
    # enough that the truncation note ("+N more") doesn't hurt.
    distinct = []
    for r in renderings:
        if r not in distinct:
            distinct.append(r)
    shown = distinct[:3]
    more = len(distinct) - len(shown)
    rendered_list = "; ".join(f'"{r[:60]}{"…" if len(r) > 60 else ""}"' for r in shown)
    if more > 0:
        rendered_list += f" (+{more} more)"
    return NormalizedObservation(
        kind="tm_inconsistency",
        severity="warn",
        paragraph_index=paragraph_index,
        excerpt=(
            f"TM inconsistency: source paragraph \"{short_source}\" has "
            f"been translated {len(distinct)} different ways across this "
            f"novel — {rendered_list}. Open the concordance to compare."
        ),
    )


def implicit_observation_glossary_merge_error(error_msg: str) -> NormalizedObservation:
    """Synthesize the implicit 'glossary_merge_error' observation.

    Written by the queue worker after the post-translation glossary merge
    raises — the translation itself is committed, but new-term auto-merge
    did not run, so the chapter may be missing the glossary entries the
    translator produced."""
    return NormalizedObservation(
        kind="glossary_merge_error",
        severity="warn",
        paragraph_index=None,
        excerpt=(
            f"Glossary auto-merge failed after the chapter committed: "
            f"{error_msg.strip() or 'unknown error'}. The translation is "
            f"safe; new glossary terms from this chapter were not added."
        ),
    )
