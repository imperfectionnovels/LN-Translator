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

# Universally-generic cultivation/common nouns that must NEVER be force-Title-
# Cased, regardless of which glossary category they landed in or what casing the
# extractor stored. This is the casing-root backstop: the 301-316 audit found
# generics force-capitalized because they sat in trusted categories (鬼/妖/怪 as
# `character`, 识海/神魂 as Title-Case `other`). Per-novel concept terms that a
# given novel wants lowercase (e.g. 果位) stay on the per-row `lowercase` note;
# this set is only the vocabulary that is generic in EVERY xianxia novel, kept
# deliberately conservative so it never swallows a named concept the project
# treats as proper (Fruition Attainment). xianxia.md already states these render
# sentence-case. ASCII, all-lowercase.
GENERIC_LOWERCASE = frozenset({
    "qi", "karma",
    "spiritual power", "spiritual energy",
    "sea of consciousness",
    "divine sense", "divine soul", "divine ability",
    "ghost", "demon", "monster", "fiend",
    "mortal", "avatar",
    # Second batch (317-323 audit): bare common nouns the extractor kept minting
    # Title-Cased, then the model copied into prose. Each is generic in every
    # xianxia novel; the per-occurrence compound guards in the down-caser protect
    # the named forms (Spirit Treasure, Five Thunders Talisman, True Dragon
    # Bloodline, Six Paths of Reincarnation, Heart Demon Fruit) because the
    # membership test is on the FULL term_en string, never a substring.
    "treasure", "talisman", "reincarnation", "bloodline",
    "heart demon", "heart demons",
})

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
    if en.lower() in GENERIC_LOWERCASE:
        # Universally-generic common noun (qi, divine sense, ghost): never
        # force-cased, whatever its category or stored casing. Casing-root
        # backstop so generics cannot be pinned Title-Case for any novel.
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


def is_half_applied_lowercase_hatch(g: GlossaryEntry) -> bool:
    """True when a locked row carries a clean `lowercase` directive in its notes
    but its `term_en` still contains an uppercase letter.

    This is the half-applied escape hatch and the recurrence root behind the
    317-323 casing defect. The casing fix needs BOTH the note AND a lowercased
    `term_en`: by deliberate design (see `_build_lowercase_targets` and
    `test_lowercase_skips_mixed_case_term_en`) the down-caser opts in on
    `term_en` casing, so a stray `lowercase` note never blanket-down-cases a
    proper name. When only the note is set, `enforce_lowercase_locked_terms`
    silently no-ops and the Title-Cased form leaks into prose through the
    glossary block the model copies. Glossary tooling (the audit + the
    learn-from-edits ingest) uses this to flag the inconsistency for repair
    instead of letting it ship. Dual-use rows (a `capitalize` / `proper` caveat,
    or a slash / parenthetical alternative) are intentionally context-cased and
    are NOT hatches, so they return False."""
    if not g.locked:
        return False
    en = (g.term_en or "").strip()
    if not en or "/" in en or "(" in en:
        return False
    notes = (g.notes or "").lower()
    if "lowercase" not in notes:
        return False
    if "proper" in notes or "capitalize" in notes:
        return False
    return en != en.lower()


# Categories that are unambiguously proper-noun-shaped in xianxia prose (the
# same set is_atomic_case_locked_term trusts). A term in one of these must
# never be stored all-lowercase — a named technique like 知見障 stored as
# "cognitive barrier" then gets that lowercase pinned into prose by
# enforce_locked_term_casing. `other` / `idiom` are deliberately excluded:
# `other` mixes Title-Case named concepts (Fruition Attainment) with generic
# vocabulary that policy keeps lowercase (spiritual power, sea of consciousness,
# treasure), so it is left to the translator's emitted casing.
_NAMED_CATEGORIES = ("character", "place", "technique", "item")


def _proper_title_case(en: str) -> str:
    """Title-case a named term: capitalize each word (and each hyphen part),
    leaving interior function words lowercase. Used to repair an all-lowercase
    auto-extracted proper noun, not to re-case a term that already carries
    deliberate casing."""

    def cap(part: str) -> str:
        return part[:1].upper() + part[1:] if part else part

    def cap_word(word: str) -> str:
        return "-".join(cap(p) for p in word.split("-"))

    words = en.split()
    out: list[str] = []
    for i, w in enumerate(words):
        if i > 0 and w.lower() in _TITLE_FUNCTION_WORDS:
            out.append(w.lower())
        else:
            out.append(cap_word(w))
    return " ".join(out)


def _normalize_extracted_casing(en: str, category: str) -> str:
    """Normalize a freshly auto-extracted English term to its canonical casing.

    Policy: techniques, items, formations, treasures, cultivation ranks,
    titles, Heavenly Stems / Earthly Branches and other named cultivation
    concepts read as proper-noun-like in xianxia prose ("Foundation
    Establishment", "Blood Transformation Divine Light"), so we keep whatever
    casing the translator emitted. Two corrections:

    1. The `_GENERIC_RANK_RE` safety net forces lowercase on a term that is
       *entirely* a generic rank/grade/tier descriptor ("Second-Rank", "late
       stage"), which reads wrong pasted mid-sentence.
    2. A named-category term (character/place/technique/item) that arrives
       *all-lowercase* is proper-cased. These categories are proper nouns by
       construction, and a lowercase one would be pinned into prose by
       enforce_locked_term_casing once locked. This only fixes case, never the
       translation; a term that already has any uppercase is left untouched.

    Idioms (`idiom` category) are extracted in lowercase per the translator
    prompt; `other` is left to the emitted casing — nothing here re-cases them."""
    if en.lower() in GENERIC_LOWERCASE:
        # Universally-generic noun: store lowercase from the start, so it is
        # never locked Title-Case and then pinned into prose.
        return en.lower()
    if _GENERIC_RANK_RE.match(en):
        return en.lower()
    if (
        category in _NAMED_CATEGORIES
        and en
        and not any(c.isupper() for c in en)
        and any(c.isalpha() for c in en)
    ):
        return _proper_title_case(en)
    return en
