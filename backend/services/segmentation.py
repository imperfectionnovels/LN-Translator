"""Deterministic source segmentation for the 1:1 paragraph contract (CAT Phase 1).

`effective_source_paragraphs` computes THE canonical paragraph list for a
chapter's source text: the leading chapter-heading line is dropped (the title
travels on the prompt's CHAPTER TITLE line), the text splits on blank lines,
and a paragraph that ends mid-sentence is joined onto its successor BEFORE the
model ever sees the text. The translator prompt is built from this list, and
the model must return exactly one output paragraph per entry, so paragraph
position becomes a stable key for the Phase 2 segment store.

`split_target_paragraphs` is the counterpart used to validate a translated or
refined body against that contract: a plain blank-line split.

The join heuristic is FROZEN once shipped: stored segmentations are keyed by
paragraph position, so changing what counts as a terminal ending would re-key
every stored row. Any change here must bump SEGMENTATION_VERSION (Phase 2
persists it per chapter).

Pure text logic: no DB access, no LLM access.
"""

from __future__ import annotations

# The blank-line splitter and the conservative chapter-heading detector are
# tm.py's; importing them keeps this module and the Phase 2 aligner from ever
# drifting apart on what a "paragraph" or a "heading" is.
from backend.services.tm import _drop_leading_heading, _split_paragraphs

# Bumped on ANY change to the terminal-ending classification or the join rule.
SEGMENTATION_VERSION = 1

# Sentence-terminal punctuation. A paragraph whose last character is one of
# these ends a sentence, so the source's paragraph break there is real.
# CJK: fullwidth stop, bang, question mark, ellipsis, semicolon, colon. ASCII:
# .!?;: (an ASCII "..." ellipsis ends in '.'). The wave dashes close
# exclamatory webnovel beats and count as terminal.
_TERMINAL_PUNCT = frozenset("。！？…；：.!?;:～~")

# Closing quotes and CJK lenticular brackets that wrap a complete unit
# (dialogue, inner thought, a system status line). Terminal on their own:
# dialogue frequently ends with no punctuation inside the quote, and a
# bracketed system line is complete by construction.
_CLOSING_QUOTES = frozenset("」』】”’\"'»")

# Ordinary closing brackets. Terminal only when the wrapped text itself ends a
# sentence (peel the trailing closer run, then require terminal punctuation):
# a trailing author aside ending in a bang ends the sentence; a mid-sentence
# parenthetical does not.
_CLOSING_BRACKETS = frozenset("）)〕]］}｝〉》")


def _is_cjk_char(ch: str) -> bool:
    """True for CJK ideographs, CJK punctuation, and fullwidth forms: the
    characters after which Chinese sets no space."""
    cp = ord(ch)
    return (
        0x2E80 <= cp <= 0x9FFF      # radicals, CJK punctuation, ideographs
        or 0xF900 <= cp <= 0xFAFF   # compatibility ideographs
        or 0xFE30 <= cp <= 0xFE4F   # CJK compatibility forms
        or 0xFF00 <= cp <= 0xFFEF   # fullwidth forms
        or 0x20000 <= cp <= 0x2FA1F # extension B and beyond
    )


def _ends_terminal(paragraph: str) -> bool:
    """True when `paragraph` ends a sentence, so its trailing break is real.

    Deliberate bias: when unsure, treating an ending as non-terminal joins two
    complete paragraphs into one (cosmetic); treating a mid-sentence break as
    terminal leaves the fragment for the model to renumber (worse). The set is
    still kept conservative because false joins compound down a chain.
    """
    p = paragraph.rstrip()
    if not p:
        return True
    # A pure divider line (asterisks, rules, equals signs) carries no letters,
    # digits, or ideographs; it is a complete unit, never a sentence fragment.
    if not any(ch.isalnum() for ch in p):
        return True
    last = p[-1]
    if last in _TERMINAL_PUNCT or last in _CLOSING_QUOTES:
        return True
    if last in _CLOSING_BRACKETS:
        i = len(p) - 1
        while i >= 0 and (p[i] in _CLOSING_BRACKETS or p[i] in _CLOSING_QUOTES):
            i -= 1
        return i >= 0 and p[i] in _TERMINAL_PUNCT
    return False


def _join_separator(prev: str) -> str:
    """Separator for a mid-sentence join: nothing at a CJK joint (Chinese sets
    no space after a trailing comma or ideograph), a single space for Latin."""
    last = prev.rstrip()[-1]
    return "" if _is_cjk_char(last) else " "


def effective_source_paragraphs(text: str) -> list[str]:
    """Canonical source-paragraph list for the 1:1 translation contract.

    1. Drop a leading chapter-heading paragraph (same conservative detector
       the TM aligner uses); the title travels on the prompt's title line.
    2. Split on blank lines (CRLF or LF), stripping whitespace and empties.
    3. Join a paragraph that does not end a sentence onto its successor,
       chaining until a terminal ending is reached. This moves the
       "join mid-sentence breaks" job out of the prompt into deterministic
       code, before the model ever sees the text.

    Idempotent: running it over its own joined output is stable.
    """
    if not text or not text.strip():
        return []
    paras = _drop_leading_heading(_split_paragraphs(text))
    out: list[str] = []
    for p in paras:
        if out and not _ends_terminal(out[-1]):
            out[-1] = f"{out[-1]}{_join_separator(out[-1])}{p}"
        else:
            out.append(p)
    return out


def split_target_paragraphs(body: str) -> list[str]:
    """Blank-line split of a translated or refined body: the validation
    counterpart of `effective_source_paragraphs`. Whitespace-stripped,
    empties dropped, CRLF tolerated."""
    return _split_paragraphs(body)
