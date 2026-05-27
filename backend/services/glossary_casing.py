"""Glossary casing rules — the foundational layer.

What counts as a generic rank descriptor vs. a proper-noun-shaped term;
which locked entries should have their casing mechanically enforced; how
freshly-extracted English forms get normalized at insert time. Pure
functions / regexes with no DB or filter dependencies, so this module sits
at the bottom of the glossary subsystem: `glossary_filters.py` and
`glossary.py` (the runtime DB module) both import from here.
"""

from __future__ import annotations

import re

from backend.models import GlossaryEntry

# A freshly extracted English term that is *entirely* a rank/grade/tier
# descriptor ("Second-Rank", "late stage") is a generic common noun, not a
# proper noun. The translator tends to title-case it at extraction time, and
# that casing then gets pasted into prose mid-sentence where it reads wrong
# ("a Second-Rank true art"). Lowercase these on insert. Multi-word proper
# nouns (realm / sect / place / technique names) never match this pattern, so
# they are left untouched — the translator prompt rule handles their in-prose
# capitalization. This is the conservative safety net for terms the LLM still
# mis-cases despite that rule.
_GENERIC_RANK_RE = re.compile(
    r"^(?:early|mid|middle|late|peak|initial|low|lower|high|higher|"
    r"top|upper|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"\d+(?:st|nd|rd|th)?)"
    r"[-\s]"
    r"(?:rank|grade|tier|stage|class|level|layer)s?$",
    re.IGNORECASE,
)


# English-function words that can legitimately appear inside a Title-Case proper
# noun ("Sea of Light", "Lord of the Three Realms"). Used by the atomic-term
# classifier: a multi-word `term_en` whose only capitalized words are function
# words isn't a proper noun — but a multi-word term with at least one
# non-function Title-Case word is. Closed set, ASCII-only.
_TITLE_FUNCTION_WORDS = frozenset(
    {"of", "the", "and", "or", "for", "in", "on", "at", "to", "a", "an", "by"}
)


def is_atomic_case_locked_term(g: GlossaryEntry) -> bool:
    """Classify whether a locked glossary entry is a hard atomic proper term
    whose casing should be mechanically enforced in translator output.

    Hard atomic terms are character / place / technique / item names and a
    structural subset of `other` (Stem-Branch compounds, named cultivation
    concepts like `Fruition Attainment`). The casing post-fix gates on this
    classifier so soft glossary rows — slash alternatives, parenthetical
    metadata, or entries whose notes deliberately say `lowercase` — are
    never force-cased.

    Conservative: returns True only when ALL conditions hold. Anything
    ambiguous (an `other` row that's a generic noun, an `idiom`, a row
    whose notes hint lowercase) returns False — the entry still appears in
    the prompt's glossary block; only the deterministic enforcement layer
    is gated.
    """
    if not g.locked:
        return False
    if g.category == "idiom":
        # Idioms render lowercase by policy; never force-case.
        return False
    if g.category not in ("character", "place", "technique", "item", "other"):
        return False
    en = (g.term_en or "").strip()
    if not en:
        return False
    # Slash alternatives are soft by convention (Karma / Karmic Threads).
    if "/" in en or "∕" in en or "／" in en:
        return False
    # Parenthetical metadata embedded in term_en (Demonic Path (philosophy)).
    if "(" in en:
        return False
    notes = (g.notes or "").lower()
    if "lowercase" in notes:
        # Explicit user note overrides everything (Spiritual Power notes:
        # 'lowercase'; Killing Karma notes: 'lowercase').
        return False
    if _GENERIC_RANK_RE.match(en):
        # Whole-string generic rank descriptor — common noun, not proper.
        return False
    if g.category in ("character", "place", "technique", "item"):
        # These categories are unambiguously proper-noun-shaped in xianxia
        # prose. Trust the category; trust the user's casing.
        return True
    # category == "other" — apply structural check. Atomic when EITHER:
    #   (a) term contains a hyphen (Stem-Branch shape: Chen-Earth, Si-Fire), or
    #   (b) multi-word with at least one non-function-word that is Title Case
    #       (Fruition Attainment, Karma Tribulation, Three Powers).
    if "-" in en:
        return True
    words = en.split()
    if len(words) < 2:
        return False
    for w in words:
        # Strip surrounding punctuation for the case test ("'s", commas, etc.).
        bare = w.strip(".,;:!?'\"()[]")
        if not bare:
            continue
        if bare.lower() in _TITLE_FUNCTION_WORDS:
            continue
        if bare[0].isupper():
            return True
    return False


def _normalize_extracted_casing(en: str, category: str) -> str:
    """Lowercase a freshly auto-extracted English term to its mid-sentence form.

    Policy: techniques, items, formations, treasures, cultivation ranks,
    titles, Heavenly Stems / Earthly Branches and other named cultivation
    concepts read as proper-noun-like in xianxia prose ("Foundation
    Establishment", "Blood Transformation Divine Light"), so we keep whatever
    casing the translator emitted. The only forced-lowercase case is the
    `_GENERIC_RANK_RE` safety net — a term that is *entirely* a generic
    rank/grade/tier descriptor ("Second-Rank", "late stage"), which the
    translator sometimes title-cases at extraction time and which then reads
    wrong pasted mid-sentence. Idioms (`idiom` category) are extracted in
    lowercase per the translator prompt; nothing here re-cases them."""
    return en.lower() if _GENERIC_RANK_RE.match(en) else en
