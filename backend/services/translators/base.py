"""Shared translator scaffolding.

`BaseTranslator` owns the prompt structure, the delimited-envelope response
parsing (`parse_delimited_response`), retry-then-fallback orchestration, and
the plain-text fallback. Backends only have to implement two hooks (`_complete`
for the structured delimited call and `_complete_plain` for the last-ditch
plain-text retry) so the per-backend code is just "how do I run an LLM call."
The system instruction, glossary formatting, and response envelope stay
identical across backends so a switch doesn't change translation behavior.

System instructions are GENRE-AWARE. The text is composed per call from
three files under `backend/prompts/`:
- `base.md` — universal literary translator rules (always present).
- `genres/<genre>.md` — genre-specific overlay (xianxia, wuxia, modern-romance,
  isekai, slice-of-life, mystery, generic).
- `examples/<genre>.md` — worked examples for that genre.

`build_system_instruction(genre, custom_brief)` does the composition with an
LRU cache. The cache key in `llm_cache.translation_key` must include the
result of this call so different-genre translations don't collide.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from functools import lru_cache

from pydantic import ValidationError

from backend.config import (
    DEFAULT_GENRE,
    FREE_DRAFT_REF_MAX_CHARS,
    MAX_LLM_CALLS_PER_CHAPTER,
    PROJECT_ROOT,
)
from backend.genres import resolve_genre
from backend.models import GlossaryEntry, NewTerm, TokenUsage, TranslationResult
from backend.services import llm_cache
from backend.services.glossary import dedupe_against_locked, filter_glossary_for_chapter
from backend.services.glossary_filters import canonical_zh

# Above this many entries, skip the O(n^2) sub-term containment scan in
# format_glossary. The per-chapter master / chapter blocks are filtered to the
# chapter and stay small; only the refiner's full-glossary call gets large, and
# there the containment hint matters least.
_CONTAINMENT_SCAN_MAX = 300

logger = logging.getLogger(__name__)

# Backoff schedule shared by every backend's transient-error retry.
BACKOFF_SCHEDULE = (2.0, 5.0, 12.0)

# Bumped per Phase 2 refactor: prompt content moved from
# data/the-translator-fiction.md to backend/prompts/*.md AND the WORKED_EXAMPLES
# constant was removed. The hashed prompt contents go into the cache key via
# self.system_instruction, but bumping this constant lets us force-invalidate
# existing entries that were cached under the old monolithic prompt.
#
# 2026-05-26 bump: build_prompt now accepts an optional ``free_draft`` kwarg
# and inserts a REFERENCE TRANSLATION section when it's set. The free-draft
# text itself is part of the cached prompt body via build_prompt's output,
# so the cache key tracks it automatically; the version bump force-misses
# any pre-PEMT cached translation so a re-run picks up the new prompt shape.
#
# 2026-05-29 bump: ground-up reframe of base.md and every genre overlay, from
# a defensive rule manual into a positive novelist's brief, plus the free-draft
# REFERENCE TRANSLATION block defaulted off (config.PROMPT_INCLUDE_FREE_DRAFT).
# 2026-06-01 bump: license paragraph-level recomposition by default — base.md's
# two-tier "what is fixed / what is yours" rewrite (structural recompose free;
# bounded amplification with a hard no-invention invariant), positive annotated
# xianxia examples, and the refiner reconciled to preserve (not flatten) force.
# The composed system instruction is already part of the llm_cache key, so this
# token is provenance plus a belt-and-suspenders force-miss of stale caches.
# 2026-06-09 bump: anchor the novelist frame to the web-novel register (plain,
# contemporary, direct English per the professionally published translations).
# Opus under the unanchored "English novelist" frame dressed plain narration in
# period diction ("talked himself round", "I should think"); the register rule
# now names that failure and "fix every flat line" no longer treats plainness
# as a defect. The -2 bump sweeps the same anchor through the remaining
# costume vectors: the fantasy overlay/example ("elevated standard English")
# and the sci-fi cyberpunk example (added image), with the refiner brief
# reframed in the same commit (its cache key hashes its own prompt).
# 2026-06-09 -3 bump: close the gap to the professional wuxiaworld register,
# diagnosed against Unsheathed/Renegade Immortal/A Will Eternal source-target
# pairs. base.md now unspools long sentences into plain linear ones (semicolon
# demoted to rare), extends the register rule to antique SYNTAX (inversion,
# subjunctive, archaic quantifiers), makes glossary labels fix form not
# frequency (pronouns between weight-bearing title uses), keeps stated
# emotions stated, allows filter verbs, bands the lexicon to everyday words,
# defaults contractions on, stops synonym rotation of repeated stock phrases,
# and flattens fossil lexicalized idioms to their plain sense (user-approved).
# Refiner brief swept for the same vectors in this commit.
# 2026-06-10 phase9 bump: construction layer. The user's own retranslate of
# ch427 showed the remaining gap is sentence ARCHITECTURE (suspended
# subjects, stacked trailing modifiers, absolute phrases, inverted
# presentation, bare-adjective dialogue tags, poetic verbs), and the audit
# found base.md manufacturing it: a novelist frame stated four times, a
# trailing-chain license stated twice, a dialogue rule that taught
# participial tag-binding while disparaging the two flat sentences WW uses,
# verb-reaching ("reach for the verb that carries its force"), and worked
# examples modeling "he said, low". All replaced with the
# webnovel-translation frame, the subject-verb-early construction rule, and
# WW-architecture example prose. Refiner brief gained the same guards.
PROMPT_TEMPLATE_VERSION = "phase9-novel-voice-ww-construction-1"

# Prompts live under backend/prompts/, NOT data/. The bundled-vs-userdata
# split makes EXE packaging clean — these files ship inside sys._MEIPASS, while
# data/ stays purely user-mutable runtime state.
_PROMPTS_ROOT = PROJECT_ROOT / "backend" / "prompts"
_BASE_PROMPT_PATH = _PROMPTS_ROOT / "base.md"
_GENRES_DIR = _PROMPTS_ROOT / "genres"
_EXAMPLES_DIR = _PROMPTS_ROOT / "examples"


class PromptAssetError(RuntimeError):
    """A bundled prompt file (base.md / a genre overlay / an examples file)
    is missing from the install. A packaging/deployment invariant violation,
    not a translator-runtime failure: retrying won't help, the file has to be
    restored. Subclasses RuntimeError so the existing startup-probe handlers
    still catch it while the type stays greppable and distinct from an
    unrelated programming RuntimeError."""


def _read_required(path) -> str:
    if not path.is_file():
        raise PromptAssetError(
            f"Prompt file not found at {path}. Restore it from version "
            "control before starting the server."
        )
    return path.read_text(encoding="utf-8")


def get_genre_overlay(genre: str) -> str:
    """Read the genre-specific overlay file. Falls back to 'generic' if the
    named overlay is missing — defensive against a genre key landing in the
    DB before its overlay file ships."""
    resolved = resolve_genre(genre, DEFAULT_GENRE)
    path = _GENRES_DIR / f"{resolved}.md"
    if not path.is_file() and resolved != "generic":
        logger.warning(
            "genre overlay %s.md missing; falling back to generic.md", resolved,
        )
        path = _GENRES_DIR / "generic.md"
    return _read_required(path)


def get_worked_examples(genre: str) -> str:
    """Read the genre-specific worked-examples file, layered into the system
    instruction so the model sees worked examples matching the novel's genre."""
    resolved = resolve_genre(genre, DEFAULT_GENRE)
    path = _EXAMPLES_DIR / f"{resolved}.md"
    if not path.is_file() and resolved != "generic":
        logger.warning(
            "genre examples %s.md missing; falling back to generic.md", resolved,
        )
        path = _EXAMPLES_DIR / "generic.md"
    return _read_required(path)


@lru_cache(maxsize=64)
def _build_system_instruction_cached(
    genre: str, custom_brief_hash: str, custom_brief: str | None,
) -> str:
    """LRU-cached composer. Keyed by (genre, hash) because the cache must be
    a stable function of the inputs — but we still need the full brief text
    to compose, hence it's a third (non-cache-key) parameter. We accept the
    duplication: callers go through `build_system_instruction` which derives
    the hash and passes the brief alongside it."""
    base = _read_required(_BASE_PROMPT_PATH)
    overlay = get_genre_overlay(genre)
    examples = get_worked_examples(genre)
    parts = [
        base,
        "GENRE OVERLAY:",
        overlay,
        "WORKED EXAMPLES (illustrative: they show the rules applied to sample "
        "lines; they never override a stated rule):",
        examples,
    ]
    if custom_brief:
        parts.extend([
            "CUSTOM STYLE BRIEF, a user-supplied directive for THIS novel. It "
            "governs word choice, set-phrase and idiom sense, naturalization, "
            "and this novel's voice, and wins over base/overlay defaults within "
            "that scope. It does NOT change glossary term wordings or the "
            "overlay's structural conventions (forms of address, title order, "
            "realm names, casing, register zones); where it appears to, the "
            "structural rule wins. Precedence, highest first: (1) the "
            "deterministic post-pass and glossary term wordings are fixed; "
            "(2) write natural English novel prose, which is the job itself, "
            "not a tiebreaker to settle last; (3) the overlay's structural "
            "conventions; (4) this brief, for voice and word choice; (5) the "
            "universal base rules; (6) the worked examples (illustrative "
            "only):",
            custom_brief.strip(),
        ])
    return "\n\n".join(parts)


def build_system_instruction(
    genre: str | None, custom_brief: str | None = None,
) -> str:
    """Compose the system instruction for a chapter translation.

    Layering: base.md (universal) + genres/<genre>.md (genre overlay) +
    examples/<genre>.md (worked examples) + optional appended custom brief.

    NULL genre resolves to DEFAULT_GENRE via the registry; unknown genres
    fall back to 'generic' inside the loader so a bad DB value cannot
    crash the translator. Whitespace-only briefs are normalized to None
    so the cache key matches the no-brief case (UI/PATCH layers also
    normalize, but this is the load-bearing check — it owns the contract).
    """
    resolved = resolve_genre(genre, DEFAULT_GENRE)
    if custom_brief is not None and not custom_brief.strip():
        custom_brief = None
    brief_hash = hashlib.sha256(
        custom_brief.encode("utf-8")
    ).hexdigest()[:16] if custom_brief else ""
    return _build_system_instruction_cached(resolved, brief_hash, custom_brief)


# Delimiter in the plain-text fallback prompt. Picked to be extremely unlikely
# to appear in real Chinese-novel English translations.
_FALLBACK_BODY_DELIMITER = "=====BODY====="

# Matches a fenced code block. We use finditer (not anchored match) so backends
# that prepend prose like "Here's the JSON:\n```json\n{...}\n```" still get
# the fence stripped. Tolerates an optional language tag and missing trailing
# newline before the closing fence (some backends emit ```json{...}``` on a
# single line). Picks the LAST balanced pair on purpose — when an LLM emits a
# schema example fence before the real answer, the answer is the trailing
# fence; picking the first would parse the example as the response.
_CODE_FENCE_RE = re.compile(
    r"```[a-zA-Z0-9_-]*[ \t]*\n?(?P<body>.*?)\n?```",
    re.DOTALL,
)


def _strip_code_fence(raw: str) -> str:
    raw = raw.strip()
    last: re.Match[str] | None = None
    for m in _CODE_FENCE_RE.finditer(raw):
        last = m
    if last is not None:
        return last.group("body").strip()
    return raw


class TransientTranslatorError(Exception):
    """Raised after every retry attempt has been exhausted on a transient
    upstream failure, or when a backend hits a usage cap that retrying won't
    immediately fix. The chapter is marked error and the user is told it's a
    service issue (not a content issue) so they know to retry later."""


def _scope_marker(g: GlossaryEntry) -> str:
    """Scope label for a prompt-glossary line.

    The MASTER (locked terms) vs THIS CHAPTER (auto-detected) block headers
    already convey novel-locked vs novel-auto, so per-line tags are omitted for
    novel entries to save tokens; only a cross-novel `[global]` term carries a
    tag, since a global entry can sit inside either block. Empty string for any
    novel-scope entry.
    """
    return "[global]" if getattr(g, "scope", "novel") == "global" else ""


def _containment_notes(
    glossary: list[GlossaryEntry],
) -> dict[int, GlossaryEntry]:
    """Map id(entry) -> the longest OTHER entry whose canonical `term_zh`
    strictly contains this entry's canonical `term_zh`.

    Category grouping scatters a compound (法力道主, character) and its
    sub-token (法力, other) into different blocks, so within-category
    longest-first ordering can't put the compound ahead of the sub-token. This
    makes the containment explicit instead, reinforcing base.md's "match the
    longest term first" rule: the sub-token line carries a pointer to the
    compound so the model does not decompose the compound into its parts.

    O(n^2); skipped above `_CONTAINMENT_SCAN_MAX` entries."""
    if len(glossary) > _CONTAINMENT_SCAN_MAX:
        return {}
    canon: dict[int, str] = {}
    for g in glossary:
        cz = canonical_zh(g.term_zh or "")
        if cz:
            canon[id(g)] = cz
    notes: dict[int, GlossaryEntry] = {}
    for g in glossary:
        cz = canon.get(id(g))
        if not cz or len(cz) < 2:
            continue
        best: GlossaryEntry | None = None
        for h in glossary:
            if h is g:
                continue
            hz = canon.get(id(h))
            if not hz or len(hz) <= len(cz) or cz not in hz:
                continue
            if best is None or len(hz) > len(canon[id(best)]):
                best = h
        if best is not None:
            notes[id(g)] = best
    return notes


def format_glossary(
    glossary: list[GlossaryEntry],
    empty_label: str = "(empty — extract terms as needed)",
) -> str:
    if not glossary:
        return empty_label
    notes = _containment_notes(glossary)
    by_cat: dict[str, list[GlossaryEntry]] = {}
    for g in glossary:
        by_cat.setdefault(g.category, []).append(g)
    lines: list[str] = []
    for cat in ("character", "place", "technique", "item", "other", "idiom"):
        entries = by_cat.get(cat, [])
        if not entries:
            continue
        lines.append(f"[{cat}]")
        # Longest-term-first inside each category so the LLM matches compound
        # terms before their substrings. Stable secondary sort by term_zh.
        for g in sorted(entries, key=lambda e: (-len(e.term_zh), e.term_zh)):
            base = f"  {g.term_zh} → {g.term_en}"
            marker = _scope_marker(g)
            if marker:
                base += f"  {marker}"
            usage = getattr(g, "usage_note", None)
            if usage and usage.strip():
                base += f"  [usage: {usage.strip()}]"
            container = notes.get(id(g))
            if container is not None:
                # Compact containment pointer; base.md already states the
                # longest-match rule, so the per-line note need not repeat it.
                base += f"  [part of {container.term_zh} → {container.term_en}]"
            lines.append(base)
    return "\n".join(lines)


def format_style_edits(style_edits: list[tuple[str, str]]) -> str:
    """Render captured user paragraph edits as a "preferred rewrites" block.

    Each tuple is (before_text, after_text). Examples are truncated to keep
    the prompt manageable: a few hundred chars per side is enough to convey
    the rewriting pattern."""
    if not style_edits:
        return ""
    lines: list[str] = []
    for i, (before, after) in enumerate(style_edits, start=1):
        b = (before or "").strip().replace("\n", " ")[:400]
        a = (after or "").strip().replace("\n", " ")[:400]
        if not b or not a:
            continue
        lines.append(f"Example {i}:\n  BEFORE: {b}\n  AFTER:  {a}")
    if not lines:
        return ""
    return (
        "USER STYLE PREFERENCES (paragraph rewrites the user made on prior chapters: "
        "treat as voice and phrasing guidance, not as text to reproduce literally. "
        "Where one conflicts with a glossary term or a structural rule, the rule wins):\n"
        + "\n\n".join(lines)
        + "\n\n"
    )


# Single output mode: raw-body delimited envelope across ALL backends. The
# chapter body rides outside any JSON-escaped string so prose quality is not
# taxed by escape-rule optimization; the small TERMS block stays as a JSON
# array because it is machine-readable shape, not literary prose.
_DELIMITED_BODY_DELIMITER = "=====BODY====="
_DELIMITED_TERMS_DELIMITER = "=====TERMS====="
DELIMITED_OUTPUT_INSTRUCTION = f"""Return the translation in EXACTLY this delimited format and nothing else — no JSON wrapper, no markdown code fences, no commentary:

TITLE_EN: <the English chapter title on one line>
{_DELIMITED_BODY_DELIMITER}
<the full English translation of the chapter body, with normal paragraph breaks>
{_DELIMITED_TERMS_DELIMITER}
<a JSON array of new glossary terms you introduced this chapter: [{{"zh": "...", "en": "...", "category": "..."}}, ...] — categories are character, technique, item, place, other, idiom. If there are none, output exactly: []>"""


def build_prompt(
    chapter_zh: str,
    title_zh: str | None,
    glossary: list[GlossaryEntry],
    previous_context: str | None = None,
    style_edits: list[tuple[str, str]] | None = None,
    output_instruction: str | None = None,
    style_note: str | None = None,
    free_draft: str | None = None,
) -> str:
    # Both locked (user-curated) and unlocked (auto-detected) entries are
    # filtered to ones whose `term_zh` appears in this chapter — terms absent
    # from the chapter can't be mistranslated, so sending them just burns
    # input tokens. Trades the byte-stable-prefix property (which mattered
    # for upstream implicit caching) for a smaller per-call prompt.
    # Drop auto-detected entries already covered by a locked alias row (e.g.
    # unlocked 筑基 alongside locked 筑基 / 築基) so the prompt carries one
    # authoritative rendering per term.
    glossary = dedupe_against_locked(glossary)
    locked_all = [g for g in glossary if g.locked]
    unlocked = [g for g in glossary if not g.locked]
    locked = filter_glossary_for_chapter(locked_all, chapter_zh)
    unlocked_in_chapter = filter_glossary_for_chapter(unlocked, chapter_zh)

    master_block = format_glossary(locked, empty_label="(none yet)")
    chapter_block = format_glossary(
        unlocked_in_chapter, empty_label="(none in this chapter)"
    )
    title_line = f"CHAPTER TITLE (Chinese): {title_zh}\n" if title_zh else ""
    # Tonal-continuity reference. Labelled DO NOT TRANSLATE so the model
    # treats it as a voice anchor rather than source text to render.
    context_block = ""
    if previous_context and previous_context.strip():
        context_block = (
            "PREVIOUS CHAPTER TAIL (English, for continuity only: carry over names, "
            "honorifics, and ongoing tone. DO NOT translate or repeat it, and do not "
            "imitate its phrasing where that conflicts with the voice rules above; the "
            "brief and overlay win over this tail):\n"
            f"{previous_context.strip()}\n\n"
        )
    style_block = format_style_edits(style_edits or [])
    style_note_block = ""
    if style_note and style_note.strip():
        style_note_block = (
            "STYLE NOTE — this novel's English voice (read as a voice instruction, "
            "match this prose):\n"
            f"{style_note.strip()}\n\n"
        )
    # PEMT: REFERENCE TRANSLATION block. Inserted only when a non-empty
    # mechanical NMT free draft is available. The instruction frames the
    # draft as a fidelity anchor — NMT preserves event order and named
    # entities more literally than LLMs do — while telling the LLM to
    # produce its own natural prose. "DO NOT TRANSLATE OR COPY VERBATIM"
    # guards against the LLM either re-translating the draft or echoing its
    # awkward phrasings.
    free_draft_block = ""
    if free_draft and free_draft.strip():
        ref = free_draft.strip()
        # Bound the reference so a pathologically long draft can't balloon the
        # prompt. The default cap is well above any normal chapter's draft.
        if FREE_DRAFT_REF_MAX_CHARS > 0 and len(ref) > FREE_DRAFT_REF_MAX_CHARS:
            ref = ref[:FREE_DRAFT_REF_MAX_CHARS].rstrip() + "\n[reference truncated]"
        free_draft_block = (
            "REFERENCE TRANSLATION (mechanical NMT — for fidelity comparison only, "
            "DO NOT TRANSLATE OR COPY VERBATIM):\n"
            f"{ref}\n\n"
            "This reference was produced by a machine-translation model. "
            "It tends to be more literal than necessary and may sound awkward, but "
            "it preserves event order, named entities, and quantities faithfully. "
            "As you translate the Chinese source, consult this reference: where "
            "its phrasing is more accurate than yours, prefer its meaning; where "
            "its phrasing is awkward, use your own. Produce a single, fluent, "
            "faithful English translation that combines the best parts of each. "
            "The output is YOUR translation — not a copy of the reference, not a "
            "simple polish of the reference.\n\n"
        )
    instruction = (
        output_instruction if output_instruction is not None
        else DELIMITED_OUTPUT_INSTRUCTION
    )
    return f"""{style_note_block}GLOSSARY — MASTER (locked terms, preserve exactly across all chapters):
{master_block}

GLOSSARY — THIS CHAPTER (auto-detected terms appearing here):
{chapter_block}

{style_block}{context_block}{free_draft_block}{title_line}CHAPTER (Chinese):
{chapter_zh}

{instruction}
"""


def _unwrap_outer_fence(text: str) -> str:
    """Strip a code fence only when it wraps the whole response.

    Do not use `_strip_code_fence` on the full delimited envelope: if the
    model fences only the TERMS JSON, taking the last fence would discard the
    title and chapter body. This mirrors the DeepSeek parser's behavior.
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl != -1:
        t = t[first_nl + 1 :]
    t = t.rstrip()
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


def _parse_new_terms_block(raw: str) -> list[NewTerm]:
    """Best-effort parse for the small TERMS JSON array in delimited mode."""
    raw = _strip_code_fence(raw.strip())
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("dropping malformed TERMS block from delimited response")
        return []
    if not isinstance(data, list):
        return []
    terms: list[NewTerm] = []
    for t in data:
        if not (isinstance(t, dict) and t.get("zh") and t.get("en")):
            continue
        try:
            terms.append(NewTerm(**t))
        except ValidationError:
            logger.warning("dropping malformed new_term: %r", t)
    return terms


def _looks_like_terms_json(block: str) -> bool:
    """True when a text block parses as the TERMS array shape: a non-empty
    JSON list whose entries are dicts carrying zh/en keys. Used to catch a
    response that emitted its terms without the TERMS delimiter, where the
    array would otherwise be committed as chapter prose."""
    block = _strip_code_fence(block.strip())
    if not (block.startswith("[") and block.endswith("]")):
        return False
    try:
        data = json.loads(block)
    except (json.JSONDecodeError, ValueError):
        return False
    return (
        isinstance(data, list)
        and len(data) > 0
        and all(isinstance(t, dict) and "zh" in t and "en" in t for t in data)
    )


def parse_delimited_response(raw: str) -> TranslationResult:
    """Parse the raw-body translator envelope into a TranslationResult."""
    text = _unwrap_outer_fence(raw)
    if _DELIMITED_BODY_DELIMITER not in text:
        raise ValueError("translation response missing BODY delimiter")
    head, _, rest = text.partition(_DELIMITED_BODY_DELIMITER)
    title_match = re.search(
        r"TITLE_EN\s*:\s*(.+?)\s*$", head.strip(), re.MULTILINE
    )
    title_en = (title_match.group(1).strip() if title_match else "") or "(untitled)"
    if _DELIMITED_TERMS_DELIMITER in rest:
        body, _, terms_raw = rest.partition(_DELIMITED_TERMS_DELIMITER)
    else:
        body, terms_raw = rest, ""
        # An absent TERMS block normally means zero new terms, but a terms
        # JSON array sitting at the end of the body means the delimiter was
        # dropped: raising here lets the caller's one-retry path fire instead
        # of committing the array as prose and silently losing the terms.
        tail = body.strip().rsplit("\n\n", 1)[-1]
        if _looks_like_terms_json(tail):
            raise ValueError(
                "translation response has a terms JSON tail but no TERMS "
                "delimiter"
            )
    body = body.strip()
    if not body:
        raise ValueError("translation response missing body text")
    return TranslationResult(
        title_en=title_en,
        translated_text=body,
        new_terms=_parse_new_terms_block(terms_raw),
    )


def _parse_titled_fallback(raw: str) -> tuple[str | None, str]:
    """Split the "TITLE_EN: X\n=====BODY=====\n..." plain-text fallback format.

    Returns (title_en, body). If the structure isn't present, returns
    (None, raw) so the caller can fall back to using the Chinese title."""
    if _FALLBACK_BODY_DELIMITER not in raw:
        return None, raw
    head, _, body = raw.partition(_FALLBACK_BODY_DELIMITER)
    match = re.search(r"TITLE_EN\s*:\s*(.+?)\s*$", head.strip(), re.MULTILINE)
    if not match:
        return None, body.strip()
    return match.group(1).strip(), body.strip()


class BaseTranslator(ABC):
    """Shared orchestration: retry once on malformed primary output, then fall
    back to a plain-text translation. Subclasses just plug in how to run the
    underlying LLM call for each of the two modes."""

    name: str = "base"
    # Upstream model identifier (e.g. "claude-opus-4-5"). Subclasses set this
    # so the LLM response cache key includes the model — a model swap then
    # invalidates entries instead of returning a result from the old model.
    model_id: str = ""
    # How many chapters this backend can safely translate in parallel. Routes
    # read this to size their semaphore. Subclasses override for stricter
    # serialization (Claude CLI subscription) or wider concurrency.
    max_parallel: int = 1
    # System instruction for the current call. Populated per-call by
    # translate_chapter from the resolved (genre, custom_brief) before any
    # backend hook runs. Backends read self.system_instruction, so genre
    # changes propagate without modifying _complete signatures.
    # Process-global queue lock keeps this single-writer; do not parallelize.
    system_instruction: str = ""

    # Per-chapter call counter. Reset at the start of translate_chapter;
    # _check_call_budget increments and raises if exhausted. Process-global
    # queue lock keeps this single-writer.
    _llm_call_count: int = 0
    # Per-chapter token usage accumulator. Reset at the start of
    # translate_chapter; backends call _emit_usage(...) after each
    # successful _complete / _complete_plain to add to the totals, and the
    # final TranslationResult carries the summed counts so the queue
    # worker can persist them.
    _usage_accumulator: TokenUsage | None = None

    def cache_identity(self) -> str:
        """Stable backend identifier used as part of the LLM cache key."""
        return f"{self.name}:{self.model_id}:{PROMPT_TEMPLATE_VERSION}"

    def _begin_chapter(
        self,
        chapter_zh: str,
        title_zh: str | None,
        glossary: list[GlossaryEntry],
        previous_context: str | None,
        style_edits: list[tuple[str, str]] | None,
        *,
        style_note: str | None,
        genre: str | None,
        custom_brief: str | None,
        free_draft: str | None,
    ) -> tuple[str, str]:
        """Shared translate_chapter prologue: reset the per-chapter call
        counter + usage accumulator, stash the genre-aware system instruction,
        build the user prompt, and derive the LLM cache key.

        Returns (prompt, cache_key). Both the standard `translate_chapter`
        loop and DeepSeek's single-pass override call this so the scaffolding
        lives in one place. The system instruction is stashed on `self` BEFORE
        the cache key is built so the key folds in the prompt content.
        """
        self._llm_call_count = 0
        self._usage_accumulator = TokenUsage()
        self.system_instruction = build_system_instruction(genre, custom_brief)
        prompt = build_prompt(
            chapter_zh, title_zh, glossary, previous_context, style_edits,
            style_note=style_note,
            free_draft=free_draft,
        )
        cache_key = llm_cache.translation_key(
            backend_id=self.cache_identity(),
            system_instruction=self.system_instruction,
            prompt=prompt,
        )
        return prompt, cache_key

    def _emit_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
    ) -> None:
        """Backends call this after each LLM round-trip to record token
        usage from the SDK response. Multiple calls accumulate (e.g. one
        translate_chapter that does parse-retry + fallback should sum
        usage across all calls). Counts coerce to int and floor at 0 to
        defang SDKs that occasionally hand back None or a negative."""
        if self._usage_accumulator is None:
            self._usage_accumulator = TokenUsage()
        self._usage_accumulator.input_tokens += max(int(input_tokens or 0), 0)
        self._usage_accumulator.output_tokens += max(int(output_tokens or 0), 0)
        self._usage_accumulator.cached_input_tokens += max(
            int(cached_input_tokens or 0), 0
        )

    def _check_call_budget(self) -> None:
        """Raise if this chapter has already burned MAX_LLM_CALLS_PER_CHAPTER
        LLM completions. Counted at the BaseTranslator orchestration layer
        (each _complete + _complete_plain ticks one), not inside an SDK's
        own transient-retry loop.
        """
        if self._llm_call_count >= MAX_LLM_CALLS_PER_CHAPTER:
            raise TransientTranslatorError(
                f"{self.name} translator exceeded the "
                f"{MAX_LLM_CALLS_PER_CHAPTER}-call per-chapter budget — "
                f"refusing further LLM calls. Raise MAX_LLM_CALLS_PER_CHAPTER "
                f"if this is an unusually long retry path you expected."
            )
        self._llm_call_count += 1

    async def translate_chapter(
        self,
        chapter_zh: str,
        title_zh: str | None,
        glossary: list[GlossaryEntry],
        previous_context: str | None = None,
        style_edits: list[tuple[str, str]] | None = None,
        use_cache: bool = True,
        style_note: str | None = None,
        genre: str | None = None,
        custom_brief: str | None = None,
        free_draft: str | None = None,
        source_language: str | None = None,
    ) -> TranslationResult:
        # ``source_language`` is accepted by the BaseTranslator surface so
        # downstream MT-only backends can route it to their underlying
        # engine. LLM backends ignore it — they read the language implicitly
        # from the source text. ``free_draft`` is the optional mechanical-NMT
        # reference layer threaded into build_prompt for PEMT mode.

        # Reset the per-chapter call counter + usage accumulator, stash the
        # genre-aware system instruction, build the prompt, and derive the
        # cache key. _check_call_budget() ticks the counter once per _complete
        # / _complete_plain invocation; _emit_usage() adds to the accumulator
        # after each successful call. The system instruction is built BEFORE
        # the cache key so the key folds in the prompt content; backend
        # _complete hooks read self.system_instruction without signature
        # changes.
        prompt, cache_key = self._begin_chapter(
            chapter_zh, title_zh, glossary, previous_context, style_edits,
            style_note=style_note, genre=genre, custom_brief=custom_brief,
            free_draft=free_draft,
        )
        if use_cache:
            cached = llm_cache.load_translation(cache_key)
            if cached is not None:
                logger.info(
                    "%s translator cache HIT (key %s…)", self.name, cache_key[:12]
                )
                return cached
            # Pair with the HIT log so `grep "translator cache" server.log
            # | awk` can produce a hit-rate stat without extra plumbing.
            logger.info(
                "%s translator cache MISS (key %s…)", self.name, cache_key[:12]
            )
        else:
            logger.info(
                "%s translator cache SKIP (force_retranslate, key %s…)",
                self.name, cache_key[:12],
            )
        for attempt in range(2):
            self._check_call_budget()
            try:
                raw = await self._complete(prompt)
                result = parse_delimited_response(raw)
                # Cache the structured result WITHOUT usage so future
                # cache hits don't replay token counts from the original
                # call (the cache hit itself burns no tokens). Usage
                # rides back to the caller via _attach_usage so the queue
                # worker can persist it on this specific commit.
                llm_cache.store_translation(cache_key, result)
                return self._attach_usage(result)
            except (ValueError, ValidationError) as e:
                logger.warning(
                    "%s response parse failed (attempt %d): %s",
                    self.name, attempt + 1, e,
                )
                if attempt == 0:
                    continue
                # Plain-text fallback intentionally not cached: it drops
                # `new_terms` and would poison the next proper call.
                fallback = await self._plain_text_fallback(chapter_zh, title_zh)
                return self._attach_usage(fallback)
        # Defensive: the loop always returns (success, or plain-text fallback
        # on the second attempt). If we somehow fall through, surface it as a
        # transient translator failure so the worker marks the chapter retryable
        # rather than letting a bare RuntimeError look like an unrelated bug.
        raise TransientTranslatorError(
            "translate_chapter exited the retry loop unexpectedly"
        )

    def _attach_usage(self, result: TranslationResult) -> TranslationResult:
        """Return a copy of `result` with the accumulated TokenUsage attached.
        Called only on FRESH translation paths (not cache hits) so cached
        TranslationResults never carry stale per-call metadata."""
        usage = self._usage_accumulator
        if usage is None or (
            usage.input_tokens == 0
            and usage.output_tokens == 0
            and usage.cached_input_tokens == 0
        ):
            return result
        return result.model_copy(update={"usage": usage})

    async def _plain_text_fallback(
        self, chapter_zh: str, title_zh: str | None
    ) -> TranslationResult:
        """Last-ditch fallback when the delimited envelope keeps failing to
        parse. Asks for the title and body in a simple plain-text format so the
        reader doesn't end up with a Chinese title above an English body. If the
        model fails to follow the format, falls back to title_zh (or
        "(untitled)")."""
        self._check_call_budget()
        if title_zh:
            prompt = (
                "Translate the following Chinese chapter to natural English. "
                "Preserve paragraph breaks. Translate BOTH the title and the "
                "body. Output exactly in this format and nothing else:\n\n"
                f"TITLE_EN: <English title>\n"
                f"{_FALLBACK_BODY_DELIMITER}\n"
                "<English body>\n\n"
                f"CHINESE TITLE: {title_zh}\n\n"
                f"CHINESE BODY:\n{chapter_zh}"
            )
            raw = (await self._complete_plain(prompt)).strip()
            title_en, body = _parse_titled_fallback(raw)
            if not body:
                raise ValueError("plain-text fallback produced empty body")
            return TranslationResult(
                title_en=title_en or title_zh,
                translated_text=body,
                new_terms=[],
                degraded=True,
            )
        prompt = (
            "Translate the following Chinese chapter to natural English. "
            "Preserve paragraph breaks. Output the English translation only.\n\n"
            f"{chapter_zh}"
        )
        body = (await self._complete_plain(prompt)).strip()
        if not body:
            raise ValueError("plain-text fallback produced empty body")
        return TranslationResult(
            title_en="(untitled)",
            translated_text=body,
            new_terms=[],
            degraded=True,
        )

    async def complete_editor_pass(
        self, prompt: str, *, system_instruction: str
    ) -> str:
        """Public seam for a standalone editor / polish pass (the refiner).

        Stashes `system_instruction` for backends that forward it as the
        system message, then runs the plain-completion hook. The caller owns
        the prompt body; this method owns the instruction stash plus the hook
        call, so the polish boundary is a named part of the translator contract
        instead of an external reach into protected state. The process-global
        queue lock keeps the instruction stash single-writer.
        """
        self.system_instruction = system_instruction
        return await self._complete_plain(prompt)

    @abstractmethod
    async def _complete(self, prompt: str) -> str:
        """Run the primary translation call. Return the raw model text — the
        delimited envelope (`TITLE_EN: ...\\n=====BODY=====\\n...\\n=====TERMS=====\\n[...]`)
        that `parse_delimited_response` will consume. Provider-specific
        envelope stripping (Claude CLI JSON wrapper, etc.) happens here."""

    @abstractmethod
    async def _complete_plain(self, prompt: str) -> str:
        """Run a plain-text completion (no delimited envelope) for the
        fallback path."""
