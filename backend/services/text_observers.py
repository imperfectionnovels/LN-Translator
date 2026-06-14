"""Deterministic text observers — detect_* functions that LOG issues.

After the 2026-05-22 single-pass restructure these detectors became
observers, not gates: the queue worker calls them after every translation,
logs any hits at INFO, and **does not retry**. The single-pass thesis is
that noticing has to happen inside the translator's thinking phase; a
follow-up retry is the same shallow pass twice. The detectors stay around
because the log line is the only telemetry the project has for surface-tic
drift across chapters — a sudden spike in mt-texture or predicate-loss
hits is the signal that the prompt or model needs attention.

Every function is pure (no I/O, no DB) so they're trivially testable.

Companion module: `text_fixups.py` holds the `enforce_*` deterministic
transforms that still run on every translator commit. Two shared internals
live here (`_DOUBLE_POSSESSIVE_RE`, the mid-sentence-paragraph-break helpers)
because the detect side is the primary concept and the fixups import from
the observers, not the other way around.
"""

from __future__ import annotations

import re

from backend.models import GlossaryEntry
from backend.services.glossary import missing_translator_terms, split_aliases

# ---------------------------------------------------------------------------
# Machine-translation texture detection
# ---------------------------------------------------------------------------

# High-confidence machine-translation tics — phrasings an English novelist
# would rarely repeat, but CN web-novel MT clusters. Each maps a short label
# to a pattern. The detector is density-gated (see _MT_TEXTURE_THRESHOLD): a
# good chapter carries one or two of these, so a single match never fires.
_MT_TEXTURE_PATTERNS: dict[str, re.Pattern] = {
    '"could not help but"': re.compile(
        r"\b(?:could|can)(?:\s+not|\s*n['’]?t)\s+help\s+but\b", re.IGNORECASE
    ),
    '"heart filled with"': re.compile(
        r"\bhearts?\s+(?:was\s+|were\s+)?filled\s+with\b", re.IGNORECASE
    ),
    '"flashed across his eyes"': re.compile(
        r"\bflashed\s+(?:across|through|in)\s+(?:his|her|its|their)\s+eyes\b",
        re.IGNORECASE,
    ),
    '"a hint/trace of …"': re.compile(
        r"\ba\s+(?:hint|trace|wisp)\s+of\b", re.IGNORECASE
    ),
    'sentence-initial "As for …"': re.compile(r"(?:^|[.!?]\s+|\n)As\s+for\b"),
    '"it must/has to be said"': re.compile(
        r"\bit\s+(?:must|has\s+to)\s+be\s+said\b", re.IGNORECASE
    ),
}

# Total matches across all patterns at or above which a chapter is flagged.
_MT_TEXTURE_THRESHOLD = 4


def detect_mt_texture(text: str) -> list[str]:
    """Flag residual machine-translation texture — phrase tics that cluster in
    CN-web-novel MT (see `_MT_TEXTURE_PATTERNS`).

    Density-gated: returns [] until the total count of tells across the chapter
    reaches `_MT_TEXTURE_THRESHOLD`, so an isolated "a hint of a smile" never
    triggers. When flagged, returns `"<label> (<n>x)"` strings, most
    frequent first."""
    if not text:
        return []
    counts: list[tuple[str, int]] = []
    total = 0
    for label, pat in _MT_TEXTURE_PATTERNS.items():
        n = len(pat.findall(text))
        if n:
            counts.append((label, n))
            total += n
    if total < _MT_TEXTURE_THRESHOLD:
        return []
    counts.sort(key=lambda lc: -lc[1])
    return [f"{label} ({n}x)" for label, n in counts]


# ---------------------------------------------------------------------------
# Residual-CJK detection: untranslated ideographs left in the English output
# ---------------------------------------------------------------------------

# Runs of Han ideographs that survived into the English: a glossary term the
# model never rendered, or an OCR-garbled source token copied straight through.
# Scoped strictly to CJK ideographs (U+3400-U+9FFF: Unified + Extension A) so it
# never trips on intentional romanized names ("Hong Yun") or fullwidth
# punctuation; only literal Han characters in the output fire it.
_RESIDUAL_CJK_RE = re.compile(r"[㐀-鿿]+")
_RESIDUAL_CJK_MAX_FLAGS = 5


def detect_residual_cjk(text: str) -> list[str]:
    """Flag runs of CJK ideographs left untranslated in the English output.

    A run is a maximal span of Han ideographs (U+3400-U+9FFF). Counts distinct
    runs and returns a single issue string listing up to five, most frequent
    first, with a ``(+N more)`` suffix when the distinct count exceeds five.
    Returns ``[]`` when the text is empty or carries no ideographs, so a
    romanized name like "Hong Yun" never fires. Output-only by design: callers
    pass the English text, never the Chinese source."""
    if not text:
        return []
    counts: dict[str, int] = {}
    order: list[str] = []
    for m in _RESIDUAL_CJK_RE.finditer(text):
        run = m.group(0)
        if run not in counts:
            counts[run] = 0
            order.append(run)
        counts[run] += 1
    if not counts:
        return []
    # Most frequent first; first-appearance order breaks ties deterministically.
    first_seen = {run: i for i, run in enumerate(order)}
    distinct = sorted(counts, key=lambda r: (-counts[r], first_seen[r]))
    shown = distinct[:_RESIDUAL_CJK_MAX_FLAGS]
    parts = "; ".join(f"'{run}' ({counts[run]}x)" for run in shown)
    msg = f"residual CJK in output: {parts}"
    extra = len(distinct) - len(shown)
    if extra > 0:
        msg += f" (+{extra} more)"
    return [msg]


# ---------------------------------------------------------------------------
# Calqued-structure detection: what-clefts and orphan "Which" fragments
# ---------------------------------------------------------------------------

# Two translation-tic structures that carry CN topic-prominence into English
# and read as non-native flow (audited 2026-06-14). They are NOT auto-fixed —
# recasting a cleft or joining an orphan clause is a meaning-preserving rewrite
# the model/refiner must do, not a regex transform. These observers give the
# per-chapter count so a prompt or refiner change can be measured.
#
# base.md:32 already bans the what-cleft; this is the telemetry that surfaces
# the adherence failures, plus the 这说明-calque "Which …" continuation that no
# existing rule names.

# Anchors a sentence start: string start, a terminal-punctuation+space, or a
# newline. Shared by both structure detectors.
_SENTENCE_START = r"(?:^|[.!?…”’\"]\s+|\n\s*)"

# Sentence-initial "What … was/were/is/are …" with at least two words between
# "What" and the copula. The word gate excludes the common question forms
# ("What was that?", "What is this?") where the copula sits right after "What";
# a genuine what-cleft always has a full clause first ("What he wanted was …").
# Tokens exclude terminal punctuation so a match never spans two sentences.
_WHAT_CLEFT_RE = re.compile(
    _SENTENCE_START
    + r"(What\b(?:\s+[^\s.!?…]+){2,10}?\s+(?:was|were|is|are)\b)"
)

# Sentence-initial "Which" + (optional connective) + a reporting/copular verb —
# the 这说明 / 这意味着 calque rendered as a standalone fragment ("Which showed
# that …"). Narrow to "Which" on purpose: "This showed …" is valid English, so
# only the antecedent-less relative pronoun is flagged.
_ORPHAN_WHICH_VERBS = (
    "showed", "shows", "meant", "means", "proved", "proves",
    "indicated", "indicates", "suggested", "suggests",
    "demonstrated", "demonstrates", "explained", "explains",
    "was", "is",
)
_ORPHAN_WHICH_RE = re.compile(
    _SENTENCE_START
    + r"(Which\b(?:\s+(?:also|only|in\s+turn|then|further|just|again))?\s+"
    + r"(?:" + "|".join(_ORPHAN_WHICH_VERBS) + r")\b)"
)

_STRUCTURE_MAX_FLAGS = 5


def _next_terminal_is_question(text: str, start: int) -> bool:
    """True when the first sentence-terminal after `start` is a '?'.

    Used to drop interrogatives the structure patterns can otherwise catch
    ("Which is correct?", "What was that, really?"). When no terminal follows
    (end of text), the span is treated as a statement (returns False)."""
    m = re.search(r"[.!?…]", text[start:])
    return bool(m) and m.group(0) == "?"


def _collect_structure_spans(pattern: re.Pattern, text: str) -> list[str]:
    """Return up to `_STRUCTURE_MAX_FLAGS` distinct statement spans matched by
    `pattern`, skipping any whose sentence ends in a question mark."""
    flagged: list[str] = []
    seen: set[str] = set()
    for m in pattern.finditer(text):
        if _next_terminal_is_question(text, m.start(1)):
            continue
        span = m.group(1).strip()
        key = span.lower()
        if key in seen:
            continue
        seen.add(key)
        flagged.append(span)
        if len(flagged) >= _STRUCTURE_MAX_FLAGS:
            break
    return flagged


def detect_what_cleft(text: str) -> list[str]:
    """Flag sentence-initial what-clefts ("What he wanted was the future").

    These violate the existing base.md:32 ban; this observer surfaces the
    adherence failures. Fires on the first hit (no density gate); returns one
    issue string listing up to five distinct spans. Interrogatives are
    excluded via the trailing-question-mark check."""
    if not text:
        return []
    flagged = _collect_structure_spans(_WHAT_CLEFT_RE, text)
    if not flagged:
        return []
    quoted = ", ".join(f'"{s}…"' for s in flagged)
    return [
        f"What-cleft structure(s): {quoted}. Recast into a plain "
        f"subject-verb sentence (\"What he wanted was X\" → \"He wanted X\") — "
        f"the cleft carries CN topic-emphasis (是…的) into English and reads "
        f"as a translation tic (base.md:32)."
    ]


def detect_orphan_which_clause(text: str) -> list[str]:
    """Flag sentence-initial "Which showed/meant/…" relative fragments.

    The 这说明 / 这意味着 calque: a relative clause punctuated as its own
    sentence with no antecedent. Fires on the first hit (no density gate);
    returns one issue string with up to five distinct spans. "This showed …"
    (a valid demonstrative subject) is deliberately not flagged."""
    if not text:
        return []
    flagged = _collect_structure_spans(_ORPHAN_WHICH_RE, text)
    if not flagged:
        return []
    quoted = ", ".join(f'"{s}…"' for s in flagged)
    return [
        f"Orphan 'Which' clause(s): {quoted}. A relative clause is punctuated "
        f"as its own sentence with no antecedent (这说明 / 这意味着 calque). "
        f"Join it to the previous sentence, or recast with a real subject "
        f"(\"This showed that …\")."
    ]


# ---------------------------------------------------------------------------
# Double-possessive detection (regex shared with text_fixups.enforce_*)
# ---------------------------------------------------------------------------

# Two consecutive `'s`-possessive tokens (`Sea's Roar's confidence`,
# `Heaven's Will's mandate`). In published English literary prose this almost
# never occurs legitimately; in xianxia translations it appears when a glossary
# character name itself contains `'s` and the model appends another. Straight
# (`'`) and curly (`’`) apostrophes both caught.
_DOUBLE_POSSESSIVE_RE = re.compile(
    # Capital-anchored first owner: rules out "it's its'" garbage.
    r"\b[A-Z][A-Za-zÀ-ſ]*['’]s"
    # Optional 1- or 2-word continuation of the name itself.
    r"(?:\s+[A-Za-z][A-Za-zÀ-ſ]*){0,2}"
    # The double — another `'s`-marked noun within the same noun phrase.
    r"\s+['’]?[A-Za-z][A-Za-zÀ-ſ]*['’]s\b"
)


def detect_double_possessive(
    text: str,
    glossary: list[GlossaryEntry] | None = None,
) -> list[str]:
    """Flag `Owner's Owner's noun` double-possessive collisions.

    Fires on the first hit (no density gate). Backstops the translator's
    fixed-term carrier-syntax rule: when a glossary name already contains
    `'s` (Sea's Roar, Heaven's Will), an additional English `'s` should never
    be appended — the carrier should be rewritten to `of [Name]` instead.

    Returns one issue string listing up to five distinct flagged spans."""
    if not text:
        return []
    flagged: list[str] = []
    seen: set[str] = set()
    for m in _DOUBLE_POSSESSIVE_RE.finditer(text):
        span = m.group(0)
        # Reject false positives where the second `'s` is the contraction
        # `'s` = `is` / `has`.
        tail = text[m.end():m.end() + 12].lstrip()
        if tail[:5] in ("gone ", "going", "been ") or tail[:4] in (
            "had ", "got ", "not ",
        ):
            continue
        key = span.lower()
        if key in seen:
            continue
        seen.add(key)
        flagged.append(span)
        if len(flagged) >= 5:
            break
    if not flagged:
        return []
    quoted = ", ".join(f'"{s}"' for s in flagged)
    return [
        f"Double possessive on a name: {quoted}. A name that already contains "
        f"'s (Sea's Roar, Heaven's Will) must not take another 's — rewrite to "
        f"'of [Name]' or recast the sentence so the locked term is preserved "
        f"and the English grammar around it is fixed."
    ]


# ---------------------------------------------------------------------------
# Mid-sentence paragraph-break detection (helpers shared with text_fixups)
# ---------------------------------------------------------------------------

# Characters that legitimately end a paragraph in English literary prose.
# Sentence terminators, plus the closing markers that follow them (quotes,
# italics, parens). A paragraph ending in any of these is OK.
_PARA_TERMINAL_CHARS = set(
    '.?!…。？！」』*)"' + "”’"  # curly close-quote glyphs
)
# Non-terminal punctuation that signals the sentence ran off mid-clause.
_PARA_NON_TERMINAL_CHARS = set(",;:、，；：")
# If the NEXT paragraph opens with one of these, the prior paragraph
# legitimately ended without terminal punctuation (e.g. `He said:\n\n"…"`).
# CJK title brackets (《》 book/scripture/manual titles, 〈〉 sections) open a
# standalone title line that must not be glued onto a comma-ended paragraph.
_NEXT_PARA_DIALOGUE_OPENERS = ('"', "“", "”", "「", "『", "《", "〈", "—", "*")


def _is_mid_sentence_paragraph_boundary(prev: str, nxt: str) -> bool:
    if not prev or not nxt:
        return False
    last = prev[-1]
    if last in _PARA_TERMINAL_CHARS:
        return False
    if nxt.startswith(_NEXT_PARA_DIALOGUE_OPENERS):
        return False
    # Trigger: mid-clause punctuation, or a lowercase letter ending the
    # paragraph. Digits, capitals, and CJK chars do not trigger — those
    # are usually legitimate non-terminal forms (headings, addresses).
    return last in _PARA_NON_TERMINAL_CHARS or (
        last.isascii() and last.isalpha() and last.islower()
    )


def detect_mid_sentence_paragraph_break(text: str) -> list[str]:
    """Flag `\\n\\n` boundaries where the prior paragraph ends mid-clause.

    Detects CN paragraph breaks preserved at the wrong position (after a comma
    or in the middle of a lowercase clause), which the translator should join
    into one sentence. Suppressed when the next paragraph opens with a dialogue
    glyph — `He said:\\n\\n"…"` is a legitimate dialogue introduction.

    Returns up to three example spans with `⏎⏎` markers for the model to
    locate. Fires on the first qualifying boundary (no density gate)."""
    if not text:
        return []
    parts = text.split("\n\n")
    if len(parts) < 2:
        return []
    flagged: list[str] = []
    for i in range(len(parts) - 1):
        prev = parts[i].rstrip()
        nxt = parts[i + 1].lstrip()
        if not _is_mid_sentence_paragraph_boundary(prev, nxt):
            continue
        tail = prev[-80:]
        head = nxt[:80]
        flagged.append(f"…{tail}⏎⏎{head}…")
        if len(flagged) >= 3:
            break
    if not flagged:
        return []
    examples = " || ".join(flagged)
    return [
        f"Mid-sentence paragraph break(s) detected — the line before \\n\\n "
        f"ends in a comma, semicolon, colon, or lowercase word, but a new "
        f"paragraph follows: {examples}. Join the two halves into one "
        f"sentence on one line; only start a new paragraph after sentence-"
        f"terminal punctuation."
    ]


# ---------------------------------------------------------------------------
# Intensifier-inflation-on-glossary-term detection
# ---------------------------------------------------------------------------

# Adjectives that the prompt rule explicitly bans as prefixes on glossary
# terms: "Do not prefix glossary terms with intensifying adjectives
# ('the formidable / mighty / powerful X') unless the raw does." This
# detector enforces that rule at observe-time: when one of these words
# appears immediately before a locked glossary term in the English, log it.
#
# The list is conservative — words that frequently appear as legitimate
# parts of cultivation-domain proper nouns (e.g. "Divine Transformation",
# "Supreme Brightness", "Eternal Radiance Treasure-Light Grotto-Heaven")
# are NOT included, even though they read as intensifiers in casual English.
# Those words ARE legitimate glossary content; flagging them would create
# false positives on every chapter that mentions a divine / supreme /
# eternal-named technique.
_INTENSIFIER_WORDS = (
    "formidable", "mighty", "powerful", "awesome", "fearsome",
    "terrifying", "tremendous", "incredible", "magnificent",
    "fabled", "legendary",
)
# Intensifier alternation is case-insensitive (`(?i:...)` inline) but the
# term capture stays case-sensitive — without that distinction, [A-Z] would
# match lowercase prose under re.IGNORECASE and the captured "term" would
# spill into trailing narrative ("the mighty Soaring Firmament struck again"
# captured the whole phrase). Term is sequences of Title-Case-shaped tokens
# (each starting with [A-Z]) joined by spaces, with `of`/`the`/`and` as
# legitimate inner function words. The trailing optional `[A-Z]…` is dropped
# because the repeating group already covers it without the false-positive
# of consuming a stray Capitalized word after a lowercase break.
_INTENSIFIER_RE = re.compile(
    r"\b(?:[Tt]he\s+)?"
    r"((?i:" + "|".join(_INTENSIFIER_WORDS) + r"))\s+"
    r"((?:[A-Z][A-Za-zÀ-ſ'’-]*)"
    r"(?:\s+(?:[A-Z][A-Za-zÀ-ſ'’-]*|of|the|and))*)"
)

_INTENSIFIER_MAX_FLAGS = 5


def detect_intensifier_inflation_on_glossary_term(
    text: str,
    glossary: list[GlossaryEntry] | None = None,
) -> list[str]:
    """Flag `<intensifier> <LockedGlossaryTerm>` patterns in the English.

    Backstops the prompt rule against gilding glossary terms with
    intensifying adjectives the source didn't supply. Only locked entries
    are checked — auto-detected entries can absorb a few extra adjectives
    without much cost, but a user-curated locked term should sit bare.

    The detector is deliberately tight:
    - The intensifier word must be one of `_INTENSIFIER_WORDS` (a short,
      uncontroversial list — "divine", "supreme", "eternal" etc. are NOT
      included because they're frequently part of legitimate glossary names).
    - The phrase right after the intensifier must contain a locked glossary
      term as a contiguous substring (so "formidable Sword of Heaven" only
      fires if "Sword of Heaven" is a locked entry).
    - Two suppression branches handle "the intensifier IS part of the
      legitimate name" cases:
        (a) the FULL `<intensifier> <term>` phrase is itself a locked
            alias — e.g. "Mighty Sword Art" locked, model wrote
            "the Mighty Sword Art";
        (b) when a NESTED locked alias also exists (both "Mighty Sword Art"
            and "Sword Art" locked), the candidate's head-match would land
            on the short one and falsely flag; check whether
            `<intensifier> <head_match>` is ALSO locked and suppress if so.
    """
    if not text or not glossary:
        return []
    locked_alias_set: set[str] = set()
    for g in glossary:
        if not g.locked:
            continue
        for _, en in split_aliases(g.term_zh or "", g.term_en or ""):
            en_clean = (en or "").strip()
            if en_clean:
                locked_alias_set.add(en_clean.lower())
    if not locked_alias_set:
        return []

    flagged: list[str] = []
    seen: set[str] = set()
    for m in _INTENSIFIER_RE.finditer(text):
        intensifier_lower = (m.group(1) or "").lower()
        candidate_term = (m.group(2) or "").strip()
        candidate_lower = candidate_term.lower()
        if not candidate_lower:
            continue
        # Suppression (a): the `<intensifier> <full candidate>` phrase is
        # itself a complete locked term. Built WITHOUT any leading "the"
        # so suppression survives whether or not the model emitted the
        # determiner.
        full_phrase = f"{intensifier_lower} {candidate_lower}"
        if full_phrase in locked_alias_set:
            continue
        # Find the longest locked alias that's a head substring of the
        # candidate term. Try the whole candidate first, then shorter
        # prefixes — handles "formidable Soaring Firmament technique"
        # finding "Soaring Firmament" without requiring "technique" to
        # be locked.
        match_alias: str | None = None
        words = candidate_lower.split()
        for end in range(len(words), 0, -1):
            head = " ".join(words[:end])
            if head in locked_alias_set:
                match_alias = head
                break
        if match_alias is None:
            continue
        # Suppression (b): when a NESTED locked alias is hit but the
        # longer `<intensifier> <head>` form is ALSO locked, the model
        # is rendering the longer legitimate name — the head-match was
        # a false positive caused by the inner alias. Example: glossary
        # locks both "Mighty Sword Art" and bare "Sword Art"; head match
        # lands on "Sword Art" and would falsely flag the intensifier.
        if f"{intensifier_lower} {match_alias}" in locked_alias_set:
            continue
        span = m.group(0).strip()
        key = span.lower()
        if key in seen:
            continue
        seen.add(key)
        flagged.append(span)
        if len(flagged) >= _INTENSIFIER_MAX_FLAGS:
            break
    if not flagged:
        return []
    quoted = ", ".join(f'"{s}"' for s in flagged)
    return [
        f"Intensifier inflation on a locked glossary term: {quoted}. The "
        f"source typically does not supply '{_INTENSIFIER_WORDS[0]}' / "
        f"'{_INTENSIFIER_WORDS[1]}' / similar before a named term; drop the "
        f"adjective unless the raw explicitly has 强大的 / 强势的 / similar."
    ]


# ---------------------------------------------------------------------------
# Glossary-anchored predicate-loss detection
# ---------------------------------------------------------------------------

# The translator's most damaging failure mode that `missing_translator_terms`
# cannot see: the locked glossary term is preserved verbatim, but the source
# predicate attached to it is silently dropped (`再遇昂霄` → "Soaring Firmament
# Once Again" keeps the term and the adverb but loses 遇 / encounter). This
# detector pairs a Chinese predicate near a locked glossary term against the
# English; if the English segment carrying the term lacks the matching action
# verb, that pair is flagged.

_PREDICATE_GROUPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "encounter/meet",
        ("再遇", "又遇", "遇见", "遇到", "碰见", "碰到"),
        (
            "encounter", "encountered", "encounters", "encountering",
            "meet", "meets", "meeting", "met",
            "reencounter", "reencountered", "re-encounter", "re-encountered",
            "run into", "runs into", "ran into",
            "come upon", "comes upon", "came upon",
            "come across", "comes across", "came across",
        ),
    ),
    (
        "find/discover",
        ("发现", "找到", "寻到", "寻得"),
        (
            "find", "finds", "finding", "found",
            "discover", "discovers", "discovering", "discovered",
            "locate", "locates", "locating", "located",
            "spot", "spots", "spotting", "spotted",
            "notice", "notices", "noticing", "noticed",
        ),
    ),
    (
        "see/behold",
        ("看见", "看到", "见到", "望见"),
        (
            "see", "sees", "seeing", "saw", "seen",
            "behold", "beholds", "beholding", "beheld",
            "caught sight", "catches sight", "catching sight",
            "glimpse", "glimpses", "glimpsing", "glimpsed",
        ),
    ),
    (
        "strike/make a move",
        ("暗中出手", "出手", "下手", "动手"),
        (
            "strike", "strikes", "striking", "struck",
            "attack", "attacks", "attacking", "attacked",
            "make a move", "makes a move", "making a move", "made a move",
            "move against", "moves against", "moving against", "moved against",
            "act against", "acts against", "acting against", "acted against",
            "target", "targets", "targeting", "targeted",
        ),
    ),
    (
        "refine/extract",
        ("炼化", "提炼", "炼制"),
        (
            "refine", "refines", "refining", "refined",
            "extract", "extracts", "extracting", "extracted",
            "absorb", "absorbs", "absorbing", "absorbed",
            "process", "processes", "processing", "processed",
        ),
    ),
    (
        "leave behind/remain",
        ("留下", "遗留", "遗下"),
        (
            "leave behind", "leaves behind", "leaving behind", "left behind",
            "remain", "remains", "remaining", "remained",
            "bequeath", "bequeaths", "bequeathing", "bequeathed",
            "abandon", "abandons", "abandoning", "abandoned",
        ),
    ),
    (
        "cast/release/unleash",
        ("施展", "释放", "祭出", "放出", "使出"),
        (
            "cast", "casts", "casting",
            "release", "releases", "releasing", "released",
            "unleash", "unleashes", "unleashing", "unleashed",
            "loose", "looses", "loosing", "loosed",
            "launch", "launches", "launching", "launched",
            "deploy", "deploys", "deploying", "deployed",
        ),
    ),
    (
        "channel/invoke",
        ("催动", "运转", "运起", "调动"),
        (
            "channel", "channels", "channeling", "channelled", "channeled",
            "invoke", "invokes", "invoking", "invoked",
            "draw on", "draws on", "drawing on", "drew on", "drawn on",
            "summon", "summons", "summoning", "summoned",
            "gather", "gathers", "gathering", "gathered",
        ),
    ),
    (
        "wield/hold",
        ("手持", "执掌", "持着", "握住", "握着"),
        (
            "wield", "wields", "wielding", "wielded",
            "hold", "holds", "holding", "held",
            "grip", "grips", "gripping", "gripped",
            "grasp", "grasps", "grasping", "grasped",
            "carry", "carries", "carrying", "carried",
            "bear", "bears", "bearing", "bore", "borne",
        ),
    ),
    (
        "master/learn",
        ("掌握", "学会", "领悟", "领会", "参悟"),
        (
            "master", "masters", "mastering", "mastered",
            "learn", "learns", "learning", "learned", "learnt",
            "grasp", "grasps", "grasping", "grasped",
            "comprehend", "comprehends", "comprehending", "comprehended",
            "understand", "understands", "understanding", "understood",
            "internalize", "internalizes", "internalizing", "internalized",
        ),
    ),
    (
        "practice/cultivate",
        ("修炼", "修习", "练习", "修行"),
        (
            "practice", "practices", "practicing", "practiced",
            "practise", "practises", "practising", "practised",
            "cultivate", "cultivates", "cultivating", "cultivated",
            "train in", "trains in", "training in", "trained in",
            "drill", "drills", "drilling", "drilled",
            "study", "studies", "studying", "studied",
        ),
    ),
    (
        "destroy/shatter",
        ("毁去", "摧毁", "击碎", "粉碎", "破坏"),
        (
            "destroy", "destroys", "destroying", "destroyed",
            "shatter", "shatters", "shattering", "shattered",
            "smash", "smashes", "smashing", "smashed",
            "crush", "crushes", "crushing", "crushed",
            "annihilate", "annihilates", "annihilating", "annihilated",
            "break", "breaks", "breaking", "broke", "broken",
            "ruin", "ruins", "ruining", "ruined",
        ),
    ),
    (
        "recognize/identify",
        ("认出", "辨认", "识别", "分辨"),
        (
            "recognize", "recognizes", "recognizing", "recognized",
            "recognise", "recognises", "recognising", "recognised",
            "identify", "identifies", "identifying", "identified",
            "make out", "makes out", "making out", "made out",
            "tell", "tells", "telling", "told",
            "name", "names", "naming", "named",
        ),
    ),
)


def _compile_en_predicate(tokens: tuple[str, ...]) -> re.Pattern:
    """Build one alternation regex over the English token list.

    Sort longest-first INSIDE the alternation so multi-word phrases like
    "make a move" win against the bare-word "move" (regex alternation is
    leftmost-first, not longest-first)."""
    ordered = sorted(set(tokens), key=lambda t: -len(t))
    alt = "|".join(re.escape(t) for t in ordered)
    return re.compile(
        r"(?<![A-Za-z0-9])(?:" + alt + r")(?![A-Za-z0-9])", re.IGNORECASE
    )


_PREDICATE_GROUPS_COMPILED: tuple[
    tuple[str, tuple[str, ...], re.Pattern, tuple[str, ...]], ...
] = tuple(
    (label, cn_triggers, _compile_en_predicate(en_tokens), en_tokens)
    for label, cn_triggers, en_tokens in _PREDICATE_GROUPS
)

_CN_PREDICATE_SEGMENT_RE = re.compile(r"[。！？!?；;\n]+")
_EN_PREDICATE_SEGMENT_RE = re.compile(r"[.!?;]+|\n\s*\n")
_PREDICATE_PROXIMITY = 20
_PREDICATE_CLAUSE_BREAK_CHARS = set("，,、；;。！？!?：:\n\r")
_PREDICATE_LOSS_MAX_ISSUES = 5


def _normalize_en_for_match(term_en: str) -> list[str]:
    """Reduce a glossary `term_en` into one or more match candidates.

    - Strip a trailing parenthetical (`Dao Lord (junior)` → `Dao Lord`).
    - Split slash alternatives (`Lesser / Greater Heaven` → both sides).
    - Trim whitespace; drop empties.
    """
    if not term_en:
        return []
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", term_en).strip()
    if not cleaned:
        return []
    parts = [p.strip() for p in cleaned.split("/") if p.strip()]
    base = parts or [cleaned]
    expanded: list[str] = []
    seen: set[str] = set()
    for candidate in base:
        if candidate and candidate not in seen:
            seen.add(candidate)
            expanded.append(candidate)
        if (
            candidate
            and not candidate.endswith("s")
            and re.search(r"[A-Za-z]$", candidate)
        ):
            plural = candidate + "s"
            if plural not in seen:
                seen.add(plural)
                expanded.append(plural)
    return expanded


def _build_en_term_pattern(term_en: str) -> re.Pattern | None:
    candidates = _normalize_en_for_match(term_en)
    if not candidates:
        return None
    candidates.sort(key=lambda c: -len(c))
    alt = "|".join(re.escape(c) for c in candidates)
    return re.compile(
        r"(?<![A-Za-z0-9])(?:" + alt + r")(?![A-Za-z0-9])", re.IGNORECASE
    )


def detect_glossary_predicate_loss(
    source_zh: str,
    translated: str,
    glossary: list[GlossaryEntry] | None = None,
    *,
    source_label: str = "chapter",
) -> list[str]:
    """Flag glossary-anchored predicate loss in a translation.

    When the Chinese source attaches an action verb to a locked glossary term
    (再遇昂霄, 暗中对鸿运道人出手, 发现这座洞天碎片), the English MUST surface
    both the term and a verb from the matching predicate group. This detector
    catches the failure mode where the locked term survives but the verb is
    dropped — a failure `missing_translator_terms` cannot see, because the
    glossary term IS present in the English.

    Args:
      source_zh: Chinese source. Pass the chapter title for title-mode and
        the chapter body for body-mode.
      translated: the English translation matching `source_zh`.
      glossary: per-novel glossary. Only locked entries are checked.
      source_label: ``"chapter title"`` (strict — the predicate must sit in
        the same English segment as the term) or ``"chapter body"`` (relaxed
        — adjacent segments ±1 also count). Other labels are treated as
        chapter-body strictness so any future callers default to the
        more permissive rule.

    Returns up to five issue strings. Returns `[]` when nothing fires
    (including: no glossary, glossary term missing from the English —
    `missing_translator_terms` owns that case)."""
    if not source_zh or not translated or not glossary:
        return []

    locked = [g for g in glossary if g.locked]
    if not locked:
        return []

    alias_pairs: list[tuple[str, str]] = []
    for g in locked:
        for zh, en in split_aliases(g.term_zh or "", g.term_en or ""):
            zh = zh.strip()
            en = en.strip()
            if not zh or not en or len(zh) <= 1:
                continue
            alias_pairs.append((zh, en))
    if not alias_pairs:
        return []
    alias_pairs.sort(key=lambda p: -len(p[0]))

    en_segments = [
        s.strip()
        for s in _EN_PREDICATE_SEGMENT_RE.split(translated)
        if s and s.strip()
    ]
    if not en_segments:
        return []

    en_term_cache: dict[str, tuple[re.Pattern, list[int]] | None] = {}

    def _term_segments(en: str) -> tuple[re.Pattern, list[int]] | None:
        if en in en_term_cache:
            return en_term_cache[en]
        pat = _build_en_term_pattern(en)
        if pat is None:
            en_term_cache[en] = None
            return None
        idxs = [i for i, seg in enumerate(en_segments) if pat.search(seg)]
        result = (pat, idxs) if idxs else None
        en_term_cache[en] = result
        return result

    strict_title = source_label == "chapter title"

    issues: list[str] = []
    emitted: set[tuple[int, str]] = set()

    cn_segments = _CN_PREDICATE_SEGMENT_RE.split(source_zh)
    for cn_idx, raw_segment in enumerate(cn_segments):
        segment = raw_segment.strip()
        if not segment:
            continue
        for zh, en in alias_pairs:
            if (cn_idx, zh) in emitted:
                continue
            zh_pos = segment.find(zh)
            if zh_pos < 0:
                continue
            zh_end = zh_pos + len(zh)
            for label, cn_triggers, en_pat, en_tokens in _PREDICATE_GROUPS_COMPILED:
                trigger_hit: str | None = None
                for trigger in sorted(cn_triggers, key=lambda t: -len(t)):
                    trig_pos = segment.find(trigger)
                    if trig_pos < 0:
                        continue
                    trig_end = trig_pos + len(trigger)
                    gap = max(0, max(trig_pos - zh_end, zh_pos - trig_end))
                    if gap > _PREDICATE_PROXIMITY:
                        continue
                    between = (
                        segment[zh_end:trig_pos]
                        if zh_end <= trig_pos
                        else segment[trig_end:zh_pos]
                    )
                    if any(ch in _PREDICATE_CLAUSE_BREAK_CHARS for ch in between):
                        continue
                    trigger_hit = trigger
                    break
                if trigger_hit is None:
                    continue

                term_info = _term_segments(en)
                if term_info is None:
                    continue
                _, term_idxs = term_info

                if strict_title:
                    candidate_idxs = set(term_idxs)
                else:
                    candidate_idxs = set()
                    for i in term_idxs:
                        candidate_idxs.add(i)
                        if i - 1 >= 0:
                            candidate_idxs.add(i - 1)
                        if i + 1 < len(en_segments):
                            candidate_idxs.add(i + 1)

                accepted = any(
                    en_pat.search(en_segments[i]) for i in candidate_idxs
                )
                if accepted:
                    emitted.add((cn_idx, zh))
                    break

                trimmed = (
                    segment if len(segment) <= 60 else segment[:57] + "..."
                )
                examples = ", ".join(f'"{t}"' for t in en_tokens[:3])
                issues.append(
                    f'Predicate loss near "{en}" in {source_label}: source '
                    f'segment "{trimmed}" carries the action {label} '
                    f'(Chinese: "{trigger_hit}"), but the English containing '
                    f'"{en}" lacks an equivalent predicate. Render the verb '
                    f"(e.g. {examples}) alongside the glossary term — "
                    f"preserve both the term and the action."
                )
                emitted.add((cn_idx, zh))
                if len(issues) >= _PREDICATE_LOSS_MAX_ISSUES:
                    return issues
                break

    return issues


def body_correctness_observations(
    source_zh: str,
    en_text: str,
    glossary: list[GlossaryEntry],
) -> list[str]:
    """Compose the deterministic correctness observations for a chapter body.

    Orchestrates this module's detect_* observers plus the glossary service's
    missing_translator_terms into one list of human-readable hit strings. Post
    single-pass restructure these are observers, not gates: the queue worker
    logs hits at INFO and never retries. Lives here (not in the queue worker)
    because it only composes this module's observers and carries no queue
    state; the queue and the edit-paragraph route both call it.

    Body-only on purpose: title-targeted observations are added at the caller
    because they reference the translator's res.title_en.
    """
    found: list[str] = []
    for zh, en in missing_translator_terms(source_zh, en_text, glossary):
        found.append(f'missing locked glossary term {zh!r} → {en!r}')
    mt_tells = detect_mt_texture(en_text)
    if mt_tells:
        found.append("mt-texture tics: " + "; ".join(mt_tells))
    found.extend(detect_residual_cjk(en_text))
    found.extend(detect_what_cleft(en_text))
    found.extend(detect_orphan_which_clause(en_text))
    found.extend(detect_double_possessive(en_text, glossary))
    found.extend(detect_intensifier_inflation_on_glossary_term(en_text, glossary))
    found.extend(detect_mid_sentence_paragraph_break(en_text))
    found.extend(
        detect_glossary_predicate_loss(
            source_zh, en_text, glossary, source_label="chapter body",
        )
    )
    return found
