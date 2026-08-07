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

The segmentation heuristic is FROZEN once shipped: stored segmentations are
keyed by paragraph position, so changing what counts as a paragraph, a
heading, or a terminal ending would re-key every stored row. THIS module owns
the canonical `_split_paragraphs`, `_drop_leading_heading`, and their regexes
(`_PARAGRAPH_BREAK_RE`, `_HEADING_RE`); tm.py re-exports them for its aligner
and the consistency tooling, so the two stacks cannot drift. Any behavior
change to anything in this module requires a SEGMENTATION_VERSION bump
(Phase 2 persists it per chapter).

Pure text logic: no DB access, no LLM access.
"""

from __future__ import annotations

import re

# The author-update-marker strip is part of the canonical chapter recipe
# (`chapter_source_paragraphs` below). Dependency direction: parser.py is
# pure text with zero service imports, so segmentation -> parser is safe and
# keeps the marker regexes single-sourced in parser; the reverse direction
# would cycle through tm.py's re-exports of this module.
from backend.services.parser import strip_heading_update_marker

# Bumped on ANY change to the paragraph split, the heading detector, the
# terminal-ending classification, the join rule, OR the retro-alignment model
# in tm.py that pairs stored bodies against these paragraphs (the stamp's job
# is "rebuild stored stores whose derivation is stale", and the pairing is
# part of the derivation).
# v2 (2026-07-31): the canonical recipe now composes the author-update-marker
# strip (chapter_source_paragraphs). Before the unification the worker merges
# stripped markers but the lazy backfill did not, so a first line surviving
# this module's tight _HEADING_RE while matching parser's broader
# _TITLE_PREFIX_RE with a marker produced DIFFERENT seg-0 sources per writer
# and the anchor's full-text check sent the chapter retain-all unaligned.
# v3 (2026-08-06): tm._dp_moves gained 2:1/1:2 group moves and last-resort
# drop costs (the pre-pivot client aligner's cost model). Source recipe
# unchanged; the bump re-pairs legacy pre-contract chapters whose stores were
# built by the drop-happy model (confident mispairs through dialogue runs).
SEGMENTATION_VERSION = 3

# Paragraph break: blank line under either CRLF or LF line endings.
_PARAGRAPH_BREAK_RE = re.compile(r"(?:\r?\n){2,}")

# CJK chapter-heading pattern dropped from the source before segmentation.
# Conservative on purpose: only a first paragraph shaped like 第N章/回/节
# counts (mirrors `_PRINTED_NUMBER_RE` in parser.py). Widening this re-keys
# every stored segmentation, so a change here is a SEGMENTATION_VERSION bump.
_HEADING_RE = re.compile(
    r"^[ \t]*第[\d零〇一二三四五六七八九十百千万两]+[ \t]*[章回节][^\n]*$",
)


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank lines (CRLF or LF), strip whitespace, drop empties."""
    if not text:
        return []
    return [p.strip() for p in _PARAGRAPH_BREAK_RE.split(text) if p.strip()]


def _drop_leading_heading(paragraphs: list[str]) -> list[str]:
    """If the first paragraph is a Chinese chapter heading, drop it.

    Keeps source and target paragraph indices in step after the title
    extraction. Idempotent: returns the input unchanged when no heading
    is on the first line."""
    if paragraphs and _HEADING_RE.match(paragraphs[0]):
        return paragraphs[1:]
    return paragraphs

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


def chapter_source_paragraphs(text: str) -> list[str]:
    """THE canonical source-paragraph list for a stored chapter:
    author-update-marker strip (parser.strip_heading_update_marker, so a
    surviving heading line loses its （第N更！）/ 求月票 tail) composed with
    `effective_source_paragraphs`.

    Every chapter_segments writer and reader MUST derive source paragraphs
    through this function: the queue's translate and refine merges, the
    segments service's lazy backfill / self-heal / reproject rebuilds, and
    any Phase 5+ addition. Deriving from effective_source_paragraphs
    directly re-opens the split recipe divergence that SEGMENTATION_VERSION
    2 closed (seg-0 source differing between writers over a cosmetic
    marker, failing the anchor's full-text check).
    """
    return effective_source_paragraphs(strip_heading_update_marker(text or ""))
