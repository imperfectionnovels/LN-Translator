"""Glossary candidate filtering, aliasing, and chapter relevance.

Pure functions on glossary data — no DB, no LLM, no I/O. Sits between
`glossary_casing.py` (the foundational rules) and `glossary.py` (the runtime
DB ops). Three concerns live here:

- **Alias / variant handling**: a glossary row that carries a slash
  separator (`筑基 / 築基`) expands into multiple (zh, en) pairs;
  `canonical_zh` folds Han variants so simplified and traditional spellings
  compare equal; `dedupe_against_locked` suppresses auto-detected duplicates
  already covered by a locked alias row.

- **Auto-extraction admission**: `filter_glossary_candidates` decides which
  freshly extracted `NewTerm`s are worth adding to the glossary at all (in a
  `【...】` system-interface span, or recurs ≥2× in the chapter body —
  one-offs stay out).

- **Per-chapter relevance**: `filter_glossary_for_chapter` narrows the
  glossary to terms that actually appear in the chapter so the prompt's
  glossary block stays small. `detect_candidate_terms` is the pre-flight
  saturation check — CN n-grams that look like proper nouns the glossary
  doesn't know about yet.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from zhconv import convert as _zh_convert

from backend.models import GlossaryEntry, NewTerm
from backend.services.glossary_casing import _GENERIC_RANK_RE

logger = logging.getLogger(__name__)

# zhconv loads its simplified/traditional dict lazily from zhcdict.json
# the first time convert() is called. In a misbuilt frozen bundle the
# .py imports succeed but the JSON is missing, and the worker would
# blow up mid-translate (the glossary-merge step calls canonical_zh).
# Track the failure once and degrade gracefully — translation still
# completes, just without simplified/traditional folding for duplicate
# detection. main._probe_bundled_runtime_data surfaces the underlying
# problem at boot.
_ZHCONV_DISABLED = False


def _safe_zh_convert(s: str, variant: str) -> str:
    """Wrap zhconv.convert so a missing dict can't crash the worker."""
    global _ZHCONV_DISABLED
    if _ZHCONV_DISABLED:
        return s
    try:
        return _zh_convert(s, variant)
    except FileNotFoundError:
        _ZHCONV_DISABLED = True
        logger.error(
            "zhconv data file missing — falling back to unfolded text. "
            "Simplified/traditional duplicates will not merge until the "
            "bundle is rebuilt with collect_data_files('zhconv')."
        )
        return s
    except Exception:
        # Any other zhconv failure is unexpected; surface it once but
        # keep the worker alive.
        logger.exception("zhconv.convert raised — falling back to unfolded text")
        _ZHCONV_DISABLED = True
        return s

# 【】 are Chinese square brackets used in xianxia raws to mark system-interface
# blocks (status panes, skill announcements, etc.). Restricting auto-glossary
# additions to terms appearing inside these spans keeps narrative one-offs out
# of the glossary. Inner class excludes both bracket chars so we never span
# across adjacent 【】 blocks.
_BRACKETED_RE = re.compile(r"【([^【】]+)】")

# Invisible characters that str.strip() does NOT remove but that silently split
# an otherwise-identical term into a separate glossary row (BOM, zero-width
# space/joiners, word joiner).
_INVISIBLE_CHARS = ("﻿", "​", "‌", "‍", "⁠")

# CJK title / quote / square-bracket glyphs are typographic wrappers around a
# term (《天剑诀》, 「索唤」, 【系统】). The source raws use the bare term, so a
# curated entry stored wrapped (the user's convention for technique/scripture
# names) must fold to the bare form for matching, or the literal-string key
# splits it into a separate row and the wrapped entry never matches the source.
# Folded for comparison only; the stored term_zh is never rewritten. ASCII /
# fullwidth parentheses （） () are DELIBERATELY excluded: entries like
# `护法 (幡)`, `练气 (碧阳)` use parens as human disambiguation annotations, and
# stripping them would cross-merge two distinct curated rows.
_WRAP_BRACKETS = ("《", "》", "〈", "〉", "「", "」", "『", "』", "〔", "〕", "【", "】")


def _normalize_line_endings(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def canonical_zh(s: str) -> str:
    """Script-fold a Chinese string for duplicate detection.

    Strips BOM / zero-width characters, drops wrapping CJK title/quote/square
    brackets (《》〈〉「」『』〔〕【】), NFC-normalizes, and folds traditional Han
    to simplified, so 索喚 (traditional) and 索唤 (simplified), and 《上皓金盞玉光》
    and bare 上皓金盏玉光, compare equal. ASCII/fullwidth parentheses are
    preserved (they carry disambiguation annotations, not typographic wrapping).
    Used ONLY for comparison: the stored `term_zh` is never rewritten, so the
    glossary keeps displaying exactly what the user or the translator produced.

    The glossary's UNIQUE(novel_id, term_zh) constraint keys on the literal
    Chinese string, so without this fold a simplified/traditional or
    bracketed/bare pair lands as two unrelated rows for one term.
    """
    s = s or ""
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    for ch in _WRAP_BRACKETS:
        s = s.replace(ch, "")
    s = unicodedata.normalize("NFC", s).strip()
    return _safe_zh_convert(s, "zh-hans")


# Glossary presets pack variant spellings into one row, slash-separated:
# `筑基 / 築基` (one term, two scripts), `小周天 / 大周天` (two related terms).
# The separator appears as ASCII "/", division slash U+2215, or fullwidth
# solidus U+FF0F, with or without surrounding spaces.
_ALIAS_SEP_RE = re.compile(r"\s*[/∕／]\s*")


def split_aliases(term_zh: str, term_en: str) -> list[tuple[str, str]]:
    """Expand a slash-delimited glossary row into (zh, en) alias pairs.

      `筑基 / 築基` → `Foundation Establishment`        → both zh → that English
      `小周天 / 大周天` → `Lesser / Greater Heaven …`   → paired positionally
      `天剑诀` → `Sky Sword Art`                        → one pair, unchanged

    When the zh and en sides have equal part counts, parts pair positionally;
    when the English side is a single value, every zh variant maps to it;
    otherwise every zh variant maps to the whole English string. The English
    side is only ever split when `term_zh` itself carries a slash, so an
    English value that happens to contain "/" is left intact for a single-term
    row."""
    zh = (term_zh or "").strip()
    en = (term_en or "").strip()
    zh_parts = [p for p in _ALIAS_SEP_RE.split(zh) if p.strip()]
    if not zh_parts:
        return []
    if len(zh_parts) == 1:
        return [(zh_parts[0], en)]
    en_parts = [p for p in _ALIAS_SEP_RE.split(en) if p.strip()]
    if len(zh_parts) == len(en_parts):
        return list(zip(zh_parts, en_parts))
    return [(p, en) for p in zh_parts]


def _locked_alias_canon(entries: list[GlossaryEntry]) -> set[str]:
    """Canonical zh forms of every alias variant carried by a locked entry."""
    out: set[str] = set()
    for g in entries:
        if not g.locked:
            continue
        for zh, _ in split_aliases(g.term_zh or "", g.term_en or ""):
            cz = canonical_zh(zh)
            if cz:
                out.add(cz)
    return out


def dedupe_against_locked(entries: list[GlossaryEntry]) -> list[GlossaryEntry]:
    """Drop unlocked (auto-detected) entries whose term is already covered by a
    locked entry's alias set.

    Without alias awareness a locked `筑基 / 築基` row and an auto-detected
    `筑基` row coexist — the locked row's literal `term_zh` never canonicalizes
    to plain `筑基`, so the duplicate is never suppressed and both reach the
    prompt. This removes the stale unlocked duplicate at prompt-assembly time;
    locked entries are always kept."""
    locked_variants = _locked_alias_canon(entries)
    if not locked_variants:
        return entries
    out: list[GlossaryEntry] = []
    for g in entries:
        if not g.locked:
            variants = {
                canonical_zh(zh)
                for zh, _ in split_aliases(g.term_zh or "", g.term_en or "")
            }
            variants.discard("")
            if variants and variants <= locked_variants:
                continue
        out.append(g)
    return out


# A non-bracketed term must recur at least this many times in the chapter to
# be trusted as real vocabulary rather than a narrative one-off.
_MIN_RECURRENCE = 2


def filter_glossary_candidates(
    chapter_zh: str, terms: list[NewTerm]
) -> list[NewTerm]:
    """Decide which extracted new_terms are worth adding to the glossary.

    A term is admitted when EITHER:
      - its `zh` appears inside a 【...】 system-interface span (the original
        rule — system-pane terms are always glossary-worthy), OR
      - its `zh` recurs at least `_MIN_RECURRENCE` times in the chapter body.

    The recurrence gate replaces the old 【】-only restriction. It lets
    recurring narrative vocabulary (越级, 偷袭, realm / sect names that never
    appear inside a system pane) into the glossary so later chapters render it
    consistently, while still keeping true one-offs out — recurring CN content
    words are almost never accidental (the same heuristic powers
    detect_candidate_terms). Unlike the old filter this does NOT return [] for
    a bracket-free chapter: such chapters can still contribute recurring terms.

    Line endings are normalized on both the haystack and each term so a CRLF-
    encoded raw doesn't silently drop terms whose `zh` was captured as LF by
    the LLM (or vice versa). The parser already normalizes to LF on intake,
    but `original_text` may be reached via paths that bypass the parser."""
    chapter_zh = _normalize_line_endings(chapter_zh or "")
    if not chapter_zh:
        return []
    haystack = canonical_zh(chapter_zh)
    # Test each 【】 span individually. Joining them with "\n" would let a
    # multi-line NewTerm.zh straddle adjacent 【】 blocks.
    spans_canon = [canonical_zh(s) for s in _BRACKETED_RE.findall(chapter_zh)]

    kept: list[NewTerm] = []
    for t in terms:
        if not t.zh:
            continue
        zh_n = canonical_zh(_normalize_line_endings(t.zh))
        if not zh_n:
            continue
        # One-character terms are never auto-admitted: a single Han char is
        # almost always a substring of unrelated words, and
        # filter_glossary_for_chapter already refuses to inject a 1-char `zh`
        # downstream, so admitting one is pure table bloat. A genuinely needed
        # 1-char term can still be added by hand (locked).
        if len(zh_n) <= 1:
            continue
        bracketed = any(zh_n in s for s in spans_canon)
        if bracketed or haystack.count(zh_n) >= _MIN_RECURRENCE:
            kept.append(t)
    return kept


def filter_glossary_for_chapter(
    glossary: list[GlossaryEntry],
    *haystacks: str,
) -> list[GlossaryEntry]:
    """Keep only glossary entries whose `term_zh` OR `term_en` appears as a
    substring in any of the provided haystack strings.

    Single-character glossary terms (剑, 道, etc.) are matched only against
    term_en — substring containment on a 1-char CN string matches almost
    every chapter and pollutes the prompt. Such entries are usually a
    mistake; the UI should discourage them.

    A slash-delimited row matches when ANY of its alias variants appears: a
    locked `筑基 / 築基` entry is kept for a chapter containing plain `筑基`.

    An entry with empty `term_zh` and `term_en` is dropped. An empty
    `haystacks` tuple — or all-empty contents — returns []."""
    if not glossary:
        return []
    joined = "\n".join(h for h in haystacks if h)
    if not joined:
        return []
    # Canonical-fold the haystack so a glossary term whose `term_zh` is in a
    # different Han script than the chapter raws (e.g. a locked traditional
    # entry against simplified raws) is still matched.
    joined_canon = canonical_zh(joined)
    # Lower-case haystack for the generic-rank case-insensitive branch below.
    # Built once per call instead of per term so 500-entry glossaries don't
    # repeat the lower() over a multi-KB body.
    joined_lower = joined.lower()
    kept: list[GlossaryEntry] = []
    for g in glossary:
        for zh, en in split_aliases(g.term_zh or "", g.term_en or ""):
            if zh and len(zh) >= 2 and canonical_zh(zh) in joined_canon:
                kept.append(g)
                break
            if en and en in joined:
                kept.append(g)
                break
            # Generic rank/grade/tier descriptors (`Second-Rank`, `late
            # stage`) are deliberately lowercased mid-sentence by the
            # translator system prompt — so a locked entry stored
            # title-cased reads as `second-rank` in prose. The plain
            # case-sensitive substring above misses these and drops the
            # entry from the downstream prompt. Match case-insensitively
            # for that specific shape.
            if en and _GENERIC_RANK_RE.match(en) and en.lower() in joined_lower:
                kept.append(g)
                break
    return kept


def detect_candidate_terms(chapter_zh: str, existing_zh: set[str]) -> list[dict]:
    """Pre-flight glossary saturation check.

    Scan a raw CN chapter for likely proper nouns that are NOT already in
    the glossary. "Likely proper noun" here is conservative: a run of 2-6
    CJK characters that appears at least twice in the chapter and is not
    a substring or superstring of an existing glossary term.

    Returns a list of `{term: ..., count: ...}` dicts sorted by frequency.
    The threshold (>=2 occurrences) filters out narrative one-offs without
    needing an LLM call — narrative CN almost never repeats a content word
    by accident, while character / sect / technique names recur naturally.

    Used before queueing a translation so the user can pre-seed glossary
    entries instead of getting them auto-extracted only from 【...】 blocks
    (which miss anything outside system-interface spans).

    NOT a substitute for the translator's own new_terms extraction — that
    still runs. This is a "did you mean to add these first?" heuristic."""
    if not chapter_zh:
        return []
    # CJK Unified Ideographs range only — `[一-鿿]+`. Run-length 2-8 is the
    # practical sweet spot for CN names: most realm / sect / technique names
    # live in this band.
    matches = re.findall(r"[一-鿿]{2,8}", chapter_zh)
    counts: dict[str, int] = {}
    for m in matches:
        counts[m] = counts.get(m, 0) + 1
    candidates: list[tuple[str, int]] = []
    for term, n in counts.items():
        if n < 2:
            continue
        # Skip terms that overlap an existing glossary entry. Substring OR
        # superstring counts as overlap so we don't surface 天剑 when 天剑诀
        # is already locked, and vice versa.
        if any(term in e or e in term for e in existing_zh):
            continue
        candidates.append((term, n))
    candidates.sort(key=lambda x: (-x[1], x[0]))
    return [{"term": t, "count": c} for t, c in candidates[:30]]
