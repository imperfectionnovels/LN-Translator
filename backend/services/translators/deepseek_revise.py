"""DeepSeek revision-pass prompts and helpers.

After the draft pass, DeepSeek runs a translate → revise pass internally
(gated on `DEEPSEEK_REVISION_ENABLED`) — either reflect→improve (two calls)
or a single combined critique+rewrite call, per `DEEPSEEK_REVISION_MODE`.

This module owns the prompts, the editor system instruction, the
temperatures used by reflect / improve / revise, and the small helpers
(`_glossary_block`, `_is_no_issues`). The orchestration methods that call
the LLM (`DeepSeekTranslator._revise` and `_revise_single`) stay in
`deepseek.py` because they're instance methods that need `self._call_deepseek`.

IMPORTANT cache-key contract:
- `DeepSeekTranslator.cache_identity` includes a `rev{N}` token that's the
  manual cache-buster for these prompts. The revision prompts are NOT part
  of the content-addressed cache key (only the draft prompt + system
  instruction are). When any prompt in THIS file changes meaningfully, bump
  the `rev{N}` literal in `DeepSeekTranslator.cache_identity`.
"""

from __future__ import annotations

import re

from backend.models import GlossaryEntry
from backend.services.glossary import dedupe_against_locked, filter_glossary_for_chapter

from .base import (
    _DELIMITED_BODY_DELIMITER as _BODY_DELIM,
)
from .base import (
    format_glossary,
    get_worked_examples,
)

# Fixed temperatures for the two revision passes. Reflect is analytical and
# wants determinism; improve is a constrained rewrite (a little above reflect,
# well below free generation). The single-pass revise call reuses the improve
# temperature — it is the same constrained-rewrite task. The draft pass uses
# the tunable DEEPSEEK_TRANSLATOR_TEMPERATURE from config.
_REFLECT_TEMPERATURE = 0.3
_IMPROVE_TEMPERATURE = 0.5

# Reflect pass runs as an editor, not a translator — its own system prompt.
# Generic enough to cover any genre; the genre-specific lifting happens in
# the prompt body, which interpolates get_worked_examples(genre).
_REVIEWER_SYSTEM_INSTRUCTION = (
    "You are a meticulous bilingual literary editor. You compare a draft "
    "English translation against its source-language original and report "
    "concrete, specific problems."
)

# The improve / revise passes carry new_terms over from the draft, so their
# output envelope omits the TERMS section.
_IMPROVE_OUTPUT_INSTRUCTION = f"""Return the final translation in EXACTLY this delimited format and nothing else — no JSON wrapper, no markdown code fences, no commentary:

TITLE_EN: <the English chapter title on one line>
{_BODY_DELIM}
<the full corrected English translation of the chapter body, with normal paragraph breaks>"""


def _glossary_block(glossary: list[GlossaryEntry], chapter_zh: str) -> str:
    """Glossary terms relevant to this chapter, formatted for the revision
    prompts. Reuses the same chapter-filtering the draft prompt applies."""
    relevant = filter_glossary_for_chapter(
        dedupe_against_locked(glossary), chapter_zh
    )
    return format_glossary(relevant, empty_label="(none)")


def _is_no_issues(suggestions: str) -> bool:
    """True when the reflect pass reported nothing to fix.

    The reflect prompt asks for the literal sentinel `NO ISSUES`. This
    normalizes the response — strip every non-letter, collapse whitespace,
    uppercase — and only treats it as a clean pass when the WHOLE response
    reduces to `NO ISSUES` (or `NO ISSUES FOUND`). A real review, even one
    whose first line says "no issues with the dialogue", has other words and
    so will not match."""
    letters = re.sub(r"[^A-Za-z]+", " ", suggestions)
    normalized = " ".join(letters.split()).upper()
    return normalized in ("NO ISSUES", "NO ISSUES FOUND")


def _build_reflect_prompt(
    chapter_zh: str, draft_en: str, glossary_block: str,
    genre: str = "generic",
) -> str:
    return f"""Review the DRAFT TRANSLATION below against the Chinese SOURCE and list concrete problems to fix before it is finalized. Focus on:

1. Fidelity — content that is mistranslated, dropped, summarized, or invented relative to the source. Verify physical, spatial, and body-part details against the source specifically (脊背 = spine/back, not forehead; 左/右; up/down) — these mistranslate easily and are easy to miss.
2. Natural English — the translation must read as native literary English, not as translated-from-Chinese. Flag: clunky, robotic, or awkward phrasing; broken grammar; unnatural word order; stiff calques; a cultivation realm or stage stacked straight onto a clan, sect, or other group as a compound noun ("early Foundation Establishment clan"), which only people can hold; a character's name repeated where natural English wants a pronoun; mechanically identical tics ("could not help but", "at this moment") rendered the same way every time; runaway exclamation-mark density beyond English convention; flat or mechanically literal word choice where a stronger, equally faithful rendering exists (a generic "stirred" for a clearly excited reaction, "retainer" where xianxia register wants "guest elder" for 客卿). A natural-English fix is valid only when it leaves meaning unchanged.
3. Consistency — the same source term, name, recurring concept, or character title/epithet rendered differently across the chapter, or differently from the GLOSSARY's established rendering; also a title/epithet whose order (title-first vs epithet-first) drifts between uses. Pick one rendering and one order, and flag the others.
4. Formatting — first-person present-tense internal thought that is not wrapped in *italics*; recited/read text or titles of written works (techniques, scriptures, manuals) not italicized per the rules.
5. Glossary — any GLOSSARY term rendered differently in the draft than specified.
6. AI-added content & filler — text the draft layered on top of the source: invented connectives ("However," "As a result," "Consequently,") between sentences the source juxtaposes without one; in-line glosses explaining a term mid-prose; intensifying adjectives prefixed onto glossary terms ("the formidable X"); stacked epic-fantasy atmosphere adjectives ("vast, ancient, mysterious") the raw lacks; emotion-clusters where the raw names one emotion; AI-tell vocabulary ("delve," "tapestry," "myriad" as filler, "navigate"/"harness" as metaphor). Flag each for removal.
7. Genre register — prose that has drifted out of the source novel's register (the SYSTEM_INSTRUCTION supplied earlier names the genre and its conventions). Flag prose that has slipped into a generic-fantasy, generic-romance, or otherwise off-register voice.

Review rules:
- Be specific. Quote the draft phrase and state the correction.
- Only list real problems. Do not invent issues, and do not rewrite the whole chapter here.
- Never flag clear, graceful CN idiom imagery as a problem merely because it is foreign. But awkward calques, unintelligible literal images, repeated character names where English wants a pronoun, identically-rendered tics, and runaway exclamation density ARE natural-English problems: flag them under item 2 when they make the prose read as translated-from-Chinese. Do not flatten a faithful idiom just because it is Chinese; do flag a literal rendering that sounds like machine translation.

GLOSSARY:
{glossary_block}

SOURCE (Chinese):
{chapter_zh}

DRAFT TRANSLATION (English):
{draft_en}

Output a numbered list of issues, most important first. If the draft has no significant problems, output exactly: NO ISSUES"""


def _build_improve_prompt(
    chapter_zh: str, draft_en: str, suggestions: str, glossary_block: str,
    genre: str = "generic",
) -> str:
    return f"""Produce the final English translation of this chapter: apply the REVIEW NOTES to the DRAFT TRANSLATION, then re-render the result as polished English novel prose.

Rules:
- Apply every valid correction in the review notes — prose, word-choice, consistency, AND formatting (italicize internal thought / recited text / work titles) alike.
- Beyond the flagged spots, actively elevate flat, stiff, or mechanically literal writing throughout — strengthen weak verbs, vary sentence rhythm and length, sharpen word choice. A paragraph that is not "wrong" but still reads as translated-from-source must be lifted too.
- This is a prose-surface licence ONLY. Never add, drop, summarize, reorder, or reinterpret content. Preserve every paragraph break.
- Preserve GLOSSARY terms exactly as specified.
- Render recurring terms, names, and titles consistently — one rendering and one epithet order per name across the whole chapter.
- Keep the chapter in the genre register established by the SYSTEM_INSTRUCTION — do not let the prose drift into a different genre's voice.
- Keep source idiom imagery when it is intelligible and reads well; otherwise render the meaning in period-appropriate English. Translationese — a repeated character name where English wants a pronoun, an identically-rendered tic, runaway exclamation density, topic-comment word order — should be smoothed into native prose, as long as meaning is unchanged.

{get_worked_examples(genre)}

GLOSSARY:
{glossary_block}

SOURCE (Chinese):
{chapter_zh}

DRAFT TRANSLATION (English):
{draft_en}

REVIEW NOTES:
{suggestions}

{_IMPROVE_OUTPUT_INSTRUCTION}"""


def _build_revise_prompt(
    chapter_zh: str, draft_en: str, glossary_block: str,
    genre: str = "generic",
) -> str:
    """Single-pass revision prompt: critique + rewrite in one call.

    Merges the reflect checklist (_build_reflect_prompt) and the improve
    correction rules (_build_improve_prompt) into one instruction. The model
    is told to review the draft against the source, then produce the corrected
    translation in the same response — collapsing the two-call reflect→improve
    pass into one round-trip."""
    return f"""Review the DRAFT TRANSLATION below against the Chinese SOURCE, then re-render it as a finished, publishable English-language novel chapter. Do the review internally — output only the final translation, not the review.

Step 1 — review the draft for:
1. Fidelity — content mistranslated, dropped, summarized, or invented relative to the source. Verify physical, spatial, and body-part details against the source specifically (脊背 = spine/back, not forehead; 左/右; up/down) — these mistranslate easily.
2. Natural English — the translation must read as native literary English, not as translated-from-Chinese. Flag clunky, robotic, or awkward phrasing; broken grammar; unnatural word order; stiff calques; a cultivation realm or stage stacked straight onto a clan, sect, or other group as a compound noun ("early Foundation Establishment clan"), which only people can hold; a character's name repeated where natural English wants a pronoun; mechanically identical tics ("could not help but", "at this moment") rendered the same way every time; runaway exclamation-mark density beyond English convention; flat or mechanically literal word choice where a stronger, equally faithful rendering exists (a generic "stirred" for a clearly excited reaction, "retainer" where xianxia register wants "guest elder" for 客卿). A natural-English fix is valid only when it leaves meaning unchanged.
3. Consistency — the same source term, name, recurring concept, or character title/epithet rendered differently across the chapter, or differently from the GLOSSARY; also a title/epithet whose order (title-first vs epithet-first) drifts between uses. Pick one rendering and one order.
4. Formatting — first-person present-tense internal thought not wrapped in *italics*; recited/read text or titles of written works (techniques, scriptures, manuals) not italicized per the rules.
5. Glossary — any GLOSSARY term rendered differently in the draft than specified.
6. AI-added content & filler — invented connectives the source has no connector for, in-line term glosses, intensifying adjectives prefixed onto glossary terms, stacked epic-fantasy atmosphere adjectives the raw lacks, emotion-clusters where the raw names one emotion, and AI-tell vocabulary ("delve," "tapestry," "myriad" as filler).
7. Genre register — prose that has drifted out of the genre register established by the SYSTEM_INSTRUCTION; keep the translation in that genre's voice.

Step 2 — produce the final translation as polished English novel prose:
- Re-render the chapter as publishable literary fiction. Do NOT merely patch the issues found in Step 1 — actively elevate flat, stiff, or mechanically literal writing throughout: strengthen weak verbs, vary sentence rhythm and length, sharpen word choice. A paragraph that is not "wrong" but still reads as translated-from-source must be lifted too.
- This is a prose-surface licence ONLY. Never add, drop, summarize, reorder, or reinterpret content — every story beat, action, and line of dialogue stays exactly as the source has it. Preserve every paragraph break.
- Preserve GLOSSARY terms exactly as specified.
- Keep source idiom imagery when it is intelligible and reads well; otherwise render the meaning in period-appropriate English. Mechanical artifacts — a repeated character name where English wants a pronoun, an identically-rendered tic, runaway exclamation density, topic-comment word order — are translationese: smooth them into native prose.

{get_worked_examples(genre)}

GLOSSARY:
{glossary_block}

SOURCE (Chinese):
{chapter_zh}

DRAFT TRANSLATION (English):
{draft_en}

{_IMPROVE_OUTPUT_INSTRUCTION}"""
