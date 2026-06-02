"""Deterministic text fixups — enforce_* transforms that run on every commit.

The translator system prompt forbids em-dashes (except cut-off speech), leaving
non-system `【…】` brackets in the prose, lowercasing Stem-Phase compounds, and
the other surface defects this module rewrites. LLMs frequently ignore these
rules, so this module is the deterministic second pass that fires unconditionally
in the queue worker (`_translate_chapter_in_db` in `services/queue.py`).

Every function is pure (no I/O, no DB), idempotent (re-running on already-clean
text is a no-op), and `(rewritten_text, count)` for symmetry.

Companion module: `text_observers.py` holds the `detect_*` log-only observers.
Shared internals live there — the detect side defines them because
detection is the primary concept.
"""

from __future__ import annotations

import re

from backend.models import GlossaryEntry
from backend.services.glossary import is_atomic_case_locked_term
from backend.services.glossary_casing import GENERIC_LOWERCASE

# ---------------------------------------------------------------------------
# Em-dash enforcement
# ---------------------------------------------------------------------------

# All three CJK-friendly long-dash glyphs the LLM occasionally emits. U+2014
# is the canonical em-dash; U+2013 (en-dash) shows up when models lean on
# Markdown habits; U+2015 (horizontal bar) is rarer but in scope. The CJK
# double `——` is two U+2014 characters, so the same character class catches
# each half — we just collapse adjacent runs before deciding what to do.
_DASH_CHARS = "—–―"
_DASH_RUN_RE = re.compile(f"[{_DASH_CHARS}]+")

# Cut-off-speech detection. The skill allows an em-dash immediately followed
# by a closing quote glyph — that's the canonical end-of-utterance pattern
# (`Lü, you shameless—"`). We accept ASCII `"`, smart `"`, and the CJK
# `」` / `』` corner brackets. Optional trailing whitespace tolerated.
_CUTOFF_FOLLOW = set('"”“」』')


def _is_cutoff(text: str, run_end: int) -> bool:
    """`text[run_end]` is the first char AFTER a dash run. Return True if
    that char (or the first non-space char) is a closing-quote glyph."""
    i = run_end
    while i < len(text) and text[i] == " ":
        i += 1
    return i < len(text) and text[i] in _CUTOFF_FOLLOW


def _pick_replacement(text: str, run_start: int, run_end: int) -> str:
    """Choose `,` or `.` based on what comes after the dash run.

    Heuristic: if the next non-space char is uppercase, treat the dash as a
    clause / sentence break and replace with `.` plus a space. Otherwise
    replace with `,` plus a space (mid-clause). Conservative — we don't try
    to be clever about parenthetical insertions; the skill forbids them
    outright."""
    j = run_end
    while j < len(text) and text[j] == " ":
        j += 1
    if j < len(text) and text[j].isupper():
        return ". "
    return ", "


def enforce_em_dash(text: str) -> tuple[str, int]:
    """Replace every disallowed em-dash with comma-space or period-space.

    Returns (rewritten_text, count)."""
    count = 0
    matches = list(_DASH_RUN_RE.finditer(text))
    out = text
    for m in reversed(matches):
        start, end = m.start(), m.end()
        if _is_cutoff(out, end):
            continue
        repl = _pick_replacement(out, start, end)
        after = out[end:]
        if after.startswith(" "):
            after = after[1:]
        before = out[:start]
        if before.endswith(" "):
            before = before[:-1]
        out = before + repl + after
        count += 1
    return out, count


# ---------------------------------------------------------------------------
# Bracket enforcement
# ---------------------------------------------------------------------------

_BRACKET_SPAN_RE = re.compile(r"【([^【】]*)】")

# A bracketed span is treated as narrative (and stripped) if its inner text
# contains any of these characters. The skill's positive examples of system
# blocks are short status-pane / announcement strings — they don't contain
# CN sentence-final punctuation, dialogue quotes, or explicit narrative
# continuation. False negatives just leave stray brackets; false positives
# corrupt a UI line, which is worse — so the trigger set is conservative.
_NARRATIVE_TRIGGER_CHARS = set('。！？!?"“”「『」』')

# Length cap, applied ONLY to unstructured standalone spans (no bold wrapper,
# no Field:Value colon) as a last-resort narrative signal. Structured panes
# (`**【…】**` or `【Field: Value】`) are kept at any length — skill / status
# descriptions are legitimately long, so length there is NOT a narrative signal.
_SYSTEM_INNER_MAX = 80


def _build_glossary_term_set(
    glossary: list[GlossaryEntry] | None,
) -> frozenset[str]:
    """Both term_en and term_zh, stripped. Used to identify bracketed spans
    that wrap a glossary name in narrative — those are emphasis brackets the
    skill says to strip, not UI labels."""
    if not glossary:
        return frozenset()
    terms: set[str] = set()
    for g in glossary:
        if g.term_en:
            terms.add(g.term_en.strip())
        if g.term_zh:
            terms.add(g.term_zh.strip())
    return frozenset(terms)


def _is_inline_span(text: str, run_start: int, run_end: int) -> bool:
    """True iff the paragraph containing the bracketed span also contains
    non-whitespace prose outside the span. Paragraphs split on blank lines.
    Immediately adjacent `**` bold wrappers don't count as prose — a
    `**【…】**` standalone callout is still its-own-paragraph."""
    p_start = text.rfind("\n\n", 0, run_start)
    p_start = 0 if p_start == -1 else p_start + 2
    p_end = text.find("\n\n", run_end)
    if p_end == -1:
        p_end = len(text)
    before = text[p_start:run_start]
    after = text[run_end:p_end]
    if before.endswith("**"):
        before = before[:-2]
    if after.startswith("**"):
        after = after[2:]
    return bool(before.strip()) or bool(after.strip())


def _looks_narrative(inner: str, glossary_terms: frozenset[str]) -> bool:
    if len(inner) > _SYSTEM_INNER_MAX:
        return True
    if any(c in _NARRATIVE_TRIGGER_CHARS for c in inner):
        return True
    # A bracketed glossary term (technique / place / character / item name)
    # is emphasis, not a UI label. The translator wraps these because the CN
    # raw does — but the skill explicitly lists "emphasis" in the strip
    # category. Match on the trimmed inner so `【 Hall of Yama 】` still hits.
    return inner.strip() in glossary_terms


def _should_strip_brackets(
    inner: str,
    glossary_terms: frozenset[str],
    text: str,
    run_start: int,
    run_end: int,
) -> bool:
    """Decide whether a `【…】` span is narrative emphasis (strip) or a system
    pane (keep).

    Order matters, and the bias is conservative: deleting a real UI pane is
    unrecoverable, while leaving a stray bracket is not. So a span is stripped
    only on positive evidence of emphasis.

    1. Inline span (other prose shares the paragraph) — emphasis, strip.
    2. Standalone span whose inner text is exactly a glossary name — a name
       used as a callout, strip (even when bold-wrapped).
    3. Standalone span that is bold-wrapped (`**【…】**`) OR carries a
       `Field: Value` colon — a system pane, KEEP regardless of length. Skill /
       status descriptions are legitimately long and contain sentence
       punctuation, so neither length nor CN punctuation is evidence of
       narrative for a structured standalone pane. (This is the fix: the old
       length / CN-punctuation triggers deleted long skill panes.)
    4. Standalone, unstructured (no bold, no colon, not a glossary name) — fall
       back to the legacy narrative heuristics for genuinely mis-bracketed prose.
    """
    if _is_inline_span(text, run_start, run_end):
        return True
    if inner.strip() in glossary_terms:
        return True
    bold = (
        text[run_start - 2 : run_start] == "**"
        and text[run_end : run_end + 2] == "**"
    )
    if bold or ":" in inner or "：" in inner:
        return False
    return _looks_narrative(inner, glossary_terms)


def enforce_brackets(
    text: str,
    glossary: list[GlossaryEntry] | None = None,
) -> tuple[str, int]:
    """Strip `【` / `】` characters from narrative-emphasis bracket spans, while
    leaving genuine system-interface panes intact.

    See `_should_strip_brackets` for the decision. The headline guarantee: a
    standalone `**【Label: value】**` pane (or any standalone bracket span with a
    `Field: Value` colon) is kept verbatim at ANY length — a long skill / status
    description is a pane, not mis-bracketed narrative. Only inline emphasis and
    standalone bare-glossary-name callouts are stripped.

    Adjacent `**` bold wrappers are stripped with the brackets — once the
    inner text is plain narrative prose, the bold formatting that was meant
    for a system block no longer applies.

    Returns (rewritten_text, count)."""
    glossary_terms = _build_glossary_term_set(glossary)
    count = 0
    matches = list(_BRACKET_SPAN_RE.finditer(text))
    out = text
    for m in reversed(matches):
        inner = m.group(1)
        if not _should_strip_brackets(inner, glossary_terms, out, m.start(), m.end()):
            continue
        strip_start = m.start()
        strip_end = m.end()
        if out[strip_start - 2 : strip_start] == "**":
            strip_start -= 2
        if out[strip_end : strip_end + 2] == "**":
            strip_end += 2
        out = out[:strip_start] + inner + out[strip_end:]
        count += 1
    return out, count


# ---------------------------------------------------------------------------
# Heavenly Stem / Earthly Branch × Five-Phase casing enforcement
# ---------------------------------------------------------------------------

# House style: every Stem-Phase or Branch-Phase compound renders Title Case on
# BOTH halves, every time (`Geng-Metal`, `Chen-Earth`, `Si-Fire`). The
# translator prompt enumerates these, but the model still drifts to
# `Chen-earth` / `Geng-metal` / `Si-fire`. Deterministic backstop.
_STEMS = "Jia|Yi|Bing|Ding|Wu|Ji|Geng|Xin|Ren|Gui"
_BRANCHES = "Zi|Chou|Yin|Mao|Chen|Si|Wu|Wei|Shen|You|Xu|Hai"
_PHASES = "fire|water|wood|metal|earth"

_STEM_BRANCH_BAD_RE = re.compile(
    r"\b(" + _STEMS + r"|" + _BRANCHES + r")-(" + _PHASES + r")\b"
)


def enforce_stem_branch_casing(text: str) -> tuple[str, int]:
    """Rewrite `Si-fire` → `Si-Fire`, `Chen-earth` → `Chen-Earth`, etc.

    Operates on every Heavenly-Stem-or-Earthly-Branch + Five-Phase compound
    whose phase half appears lowercase. Closed alternation set; idempotent."""
    if not text:
        return text, 0

    count = 0

    def _repl(m: re.Match) -> str:
        nonlocal count
        count += 1
        stem_or_branch = m.group(1)
        phase = m.group(2)
        return f"{stem_or_branch}-{phase.capitalize()}"

    out = _STEM_BRANCH_BAD_RE.sub(_repl, text)
    return out, count


# ---------------------------------------------------------------------------
# Locked-term casing enforcement (glossary-driven)
# ---------------------------------------------------------------------------

# Code fence / system-pane spans that are off-limits for in-prose casing
# rewrites. Code fences are matched as standard triple-backtick blocks.
# System-pane standalone bold blocks (`**【…】**` on a line by themselves) are
# also skipped — those are deliberately verbatim system text. Italic spans
# (`*…*`) are NOT skipped: written-work titles legitimately appear inside
# italics, and lowercased titles inside italics are exactly the failure case
# the post-fix is meant to catch.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_SYSTEM_PANE_RE = re.compile(r"^\s*\*\*【[^【】]+】\*\*\s*$", re.MULTILINE)


def _collect_protected_spans(text: str) -> list[tuple[int, int]]:
    """Inclusive [start, end) ranges where casing must not be rewritten."""
    spans: list[tuple[int, int]] = []
    for m in _CODE_FENCE_RE.finditer(text):
        spans.append((m.start(), m.end()))
    for m in _SYSTEM_PANE_RE.finditer(text):
        spans.append((m.start(), m.end()))
    spans.sort()
    return spans


def _in_protected_span(idx: int, spans: list[tuple[int, int]]) -> bool:
    for s, e in spans:
        if s <= idx < e:
            return True
        if idx < s:
            return False
    return False


def _build_atomic_targets(
    glossary: list[GlossaryEntry] | None,
) -> list[tuple[str, str]]:
    """Distinct (term_en, target_en) pairs to enforce. Cross-entry safety:
    if ANY locked entry sharing a `term_en` has `lowercase` in its notes, the
    entire English form is treated as soft and dropped from the enforcement
    set."""
    if not glossary:
        return []
    soft_en: set[str] = set()
    atomic_en: dict[str, str] = {}
    for g in glossary:
        if not g.locked:
            continue
        en = (g.term_en or "").strip()
        if not en:
            continue
        notes = (g.notes or "").lower()
        if "lowercase" in notes:
            soft_en.add(en)
            continue
        if is_atomic_case_locked_term(g):
            atomic_en.setdefault(en, en)
    return [(k, v) for k, v in atomic_en.items() if k not in soft_en]


def enforce_locked_term_casing(
    text: str, glossary: list[GlossaryEntry] | None
) -> tuple[str, int]:
    """Normalize casing of atomic locked glossary terms to their canonical form.

    For each locked entry where `is_atomic_case_locked_term(g)` returns True
    (and no sibling row marks the same `term_en` as soft via a `lowercase`
    note), replace whole-word case-insensitive matches in `text` with the
    canonical `term_en`. Skips code fences and standalone `**【…】**` system-
    pane lines; does NOT skip italic spans.

    Whole-word boundaries on both sides. Idempotent. Returns (text, count)."""
    if not text:
        return text, 0
    targets = _build_atomic_targets(glossary)
    if not targets:
        return text, 0

    protected = _collect_protected_spans(text)

    # Longest target first so a longer multi-word term ("True Person Sea's
    # Roar") is matched before a shorter subset ("Sea's Roar") that happens to
    # be a separate glossary entry — otherwise the shorter one wins by greedy
    # order and the longer rewrite can't happen because the boundaries shift.
    targets.sort(key=lambda t: -len(t[0]))

    count = 0
    out = text

    for canonical, _ in targets:
        pat = re.compile(
            r"(?<![A-Za-z0-9_'’])"
            + re.escape(canonical)
            + r"(?![A-Za-z0-9_'’])",
            re.IGNORECASE,
        )
        matches = list(pat.finditer(out))
        for m in reversed(matches):
            start = m.start()
            if _in_protected_span(start, protected):
                continue
            current = m.group(0)
            if current == canonical:
                continue
            out = out[:start] + canonical + out[m.end():]
            count += 1

    return out, count


# ---------------------------------------------------------------------------
# Locked-term DOWN-casing enforcement (the missing other direction)
# ---------------------------------------------------------------------------

# `enforce_locked_term_casing` above only ever pushes casing UP toward a
# Title-Case canonical, and `_build_atomic_targets` deliberately DROPS every
# entry whose notes say `lowercase`. That left generic cultivation nouns the
# model capitalized on its own (`Avatar`, `Divine Sense`, `Sea of
# Consciousness`) with no deterministic way back down. This transform is that
# missing direction: it force-lowercases occurrences of locked rows explicitly
# marked `lowercase`, with guards so it never clobbers a sentence-initial word
# or a forward proper-noun compound (`Ghost Mountain`).

# Chars that may sit between a sentence boundary and the first word: leading
# whitespace plus opening quote / emphasis / bracket glyphs.
_SENTENCE_OPENERS = set(" \t\"'“”‘’*([{")


def _is_sentence_initial(text: str, idx: int) -> bool:
    """True iff the word starting at `idx` sits at a sentence/line head.

    Walks left over opener glyphs (quotes, `*`, brackets) and spaces. A head is
    text-start, a newline, or a `.!?` run that is NOT an ellipsis (`..`)."""
    j = idx - 1
    while j >= 0 and text[j] in _SENTENCE_OPENERS:
        j -= 1
    if j < 0:
        return True
    c = text[j]
    if c == "\n":
        return True
    if c in ".!?":
        k = j
        while k >= 0 and text[k] in ".!?":
            k -= 1
        run = text[k + 1 : j + 1]
        return ".." not in run
    return False


def _next_nonspace_is_upper(text: str, end: int) -> bool:
    """True iff the next non-space char after `end` is an uppercase letter —
    the forward signal of a Title-Case proper-noun compound (`Ghost Mountain`)."""
    j = end
    while j < len(text) and text[j] == " ":
        j += 1
    return j < len(text) and text[j].isupper()


# Capitalized words that may precede a generic without forming a proper-noun
# compound, so "His Avatar" still down-cases while "Innate Divine Ability" and
# "Ghost Mountain" do not.
_FUNCTION_WORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "his", "her", "hers",
    "its", "their", "theirs", "my", "mine", "your", "yours", "our", "ours",
    "one", "no", "each", "every", "some", "any", "all", "both", "another",
    "such", "he", "she", "it", "they", "we", "you", "i", "of", "and", "or",
    "but", "nor", "with", "without", "from", "into", "to", "in", "on", "at",
    "by", "as",
})


def _preceding_word(text: str, start: int) -> str:
    """The alphabetic word immediately before `start` (one space tolerated)."""
    j = start - 1
    while j >= 0 and text[j] == " ":
        j -= 1
    end = j + 1
    while j >= 0 and (text[j].isalpha() or text[j] in "'’"):
        j -= 1
    return text[j + 1 : end]


def _build_lowercase_targets(
    glossary: list[GlossaryEntry] | None,
) -> list[str]:
    """Lowercase canonical forms to force down. A locked row qualifies only when
    its `term_en` is ALREADY all-lowercase (an explicit opt-in, so a named term
    like 虛瞑之地 -> "the Void" is never touched), its notes say `lowercase`, it
    is not a slash / parenthetical metadata row, and it carries no `proper`
    caveat (e.g. 虚空 "capitalize when proper place"). Named-compound uses are
    handled by the per-occurrence guards, not by excluding the term here. The
    shared `GENERIC_LOWERCASE` lexicon is always included, so universally-generic
    vocabulary is down-cased even when the novel has no row for it."""
    targets: set[str] = set(GENERIC_LOWERCASE)
    for g in glossary or []:
        if not g.locked:
            continue
        en = (g.term_en or "").strip()
        if not en or "/" in en or "(" in en:
            continue
        if en != en.lower():
            continue
        notes = (g.notes or "").lower()
        if "lowercase" not in notes:
            continue
        if "proper" in notes:
            continue
        targets.add(en)
    return sorted(targets, key=lambda t: -len(t))


def enforce_lowercase_locked_terms(
    text: str, glossary: list[GlossaryEntry] | None
) -> tuple[str, int]:
    """Force locked `lowercase`-noted glossary terms down to lowercase.

    Whole-word case-insensitive matches are rewritten to the lowercase
    canonical, EXCEPT when the occurrence is sentence-initial (correctly
    capitalized there), is inside a protected span, or is immediately followed
    by another capitalized word (a forward proper-noun compound). Idempotent.
    Returns (text, count)."""
    if not text:
        return text, 0
    targets = _build_lowercase_targets(glossary)
    if not targets:
        return text, 0

    protected = _collect_protected_spans(text)
    count = 0
    out = text

    for canonical in targets:
        pat = re.compile(
            r"(?<![A-Za-z0-9_'’])"
            + re.escape(canonical)
            + r"(?![A-Za-z0-9_'’])",
            re.IGNORECASE,
        )
        for m in reversed(list(pat.finditer(out))):
            start, end = m.start(), m.end()
            if m.group(0) == canonical:
                continue
            if _in_protected_span(start, protected):
                continue
            if _is_sentence_initial(out, start):
                continue
            if _next_nonspace_is_upper(out, end):
                continue
            prev = _preceding_word(out, start)
            if prev and prev[0].isupper() and prev.lower() not in _FUNCTION_WORDS:
                continue
            out = out[:start] + canonical + out[end:]
            count += 1

    return out, count


# ---------------------------------------------------------------------------
# Trailing chapter-end marker strip
# ---------------------------------------------------------------------------

# Web-source chapters end with a CMS sentinel (`(本章完)`) that the model
# sometimes translates (`(End of Chapter)`) and leaves in the body instead of
# dropping. Anchored to end-of-text so only a genuinely trailing marker is
# removed; mid-body occurrences are left untouched.
_CHAPTER_END_MARKER_RE = re.compile(
    r"\s*[(（]?\s*(?:本章完|end\s+of\s+chapter)\s*[)）]?\s*$",
    re.IGNORECASE,
)


def strip_chapter_end_marker(text: str) -> tuple[str, int]:
    """Strip a leaked trailing `(本章完)` / `(End of Chapter)` sentinel.

    Only removes the marker when real body text precedes it. Returns
    (text, count) with count 0 or 1. Idempotent."""
    if not text:
        return text, 0
    m = _CHAPTER_END_MARKER_RE.search(text)
    if not m:
        return text, 0
    head = text[: m.start()]
    if not head.strip():
        return text, 0
    return head.rstrip(), 1


# ---------------------------------------------------------------------------
# Sentence-initial re-capitalization
# ---------------------------------------------------------------------------


# Trailing glyphs skipped when checking whether a paragraph ended a sentence:
# spaces plus closing quote / emphasis / bracket glyphs (`said."`).
_SENTENCE_CLOSERS = set(" \t\"'“”‘’*)]}")


def _preceding_ends_sentence(chars: list[str], idx: int) -> bool:
    """True iff the char before `idx` (skipping trailing spaces and closing
    quote / bracket glyphs) is a sentence terminator. Used to gate paragraph
    starts so a mid-sentence paragraph break (prev line ends in a comma or a
    bare word) is NOT cosmetically dressed up as a new sentence."""
    j = idx - 1
    while j >= 0 and chars[j] in _SENTENCE_CLOSERS:
        j -= 1
    return j >= 0 and chars[j] in ".!?"


def enforce_sentence_initial_capitalization(text: str) -> tuple[str, int]:
    """Capitalize the first letter of each sentence.

    Fixes inserted proper nouns left lowercase at a sentence head ("the Heaven
    of Non-Being..."). Boundaries: text start; the first content char of a
    paragraph ONLY when the previous paragraph ended a sentence (so a logged
    mid-sentence paragraph break is left untouched); and after a non-ellipsis
    `.!?` run immediately followed by whitespace (so a `."` dialogue-tag period
    is left alone). Opener glyphs (quotes, `*`, brackets) are skipped to reach
    the first letter. Protected spans are untouched. Idempotent. Returns
    (text, count)."""
    if not text:
        return text, 0

    protected = _collect_protected_spans(text)
    chars = list(text)
    n = len(chars)

    boundaries: list[int] = [0]
    j = 0
    while j < n:
        c = chars[j]
        if c == "\n":
            k = j
            while k < n and chars[k] in "\n \t":
                k += 1
            if _preceding_ends_sentence(chars, j):
                boundaries.append(k)
            j = k
            continue
        if c in ".!?":
            k = j
            while k < n and chars[k] in ".!?":
                k += 1
            run = "".join(chars[j:k])
            if ".." not in run and k < n and chars[k] in " \t":
                boundaries.append(k)
            j = k
            continue
        j += 1

    count = 0
    seen: set[int] = set()
    for b in boundaries:
        p = b
        while p < n and chars[p] in _SENTENCE_OPENERS:
            p += 1
        if p >= n or p in seen:
            continue
        seen.add(p)
        ch = chars[p]
        if "a" <= ch <= "z" and not _in_protected_span(p, protected):
            chars[p] = ch.upper()
            count += 1

    return "".join(chars), count


# Note: deterministic enforce_double_possessive_carriers /
# enforce_mid_sentence_paragraph_break helpers were removed during the
# 2026-05-25 audit cleanup. The single-pass thesis is that noticing has
# to happen inside the translator's thinking phase, so these failure
# modes are LOGGED by `detect_double_possessive` /
# `detect_mid_sentence_paragraph_break` in `text_observers.py` and the
# observer hits flow into the QA dashboard for review. The
# `_is_mid_sentence_paragraph_boundary` helper lives in text_observers.py;
# this module does not import it.
