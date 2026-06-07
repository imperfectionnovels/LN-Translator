"""Optional per-novel refinement pass.

A novel with `novels.refinement_provider_id` set runs an extra LLM call
after the translator commits. The refiner takes the translator's draft
English body and polishes it into more readable novel prose — surface
smoothing only, NO re-checking against the Chinese source. The draft is
the source of truth for fidelity; the refiner trusts it.

State machine on the `chapters` table:
- `refinement_status` ∈ {none, pending, in_progress, done, error}
- `refined_text` populated on success
- `refinement_error` populated on failure
- `refined_at` timestamp on success

The queue worker drives the state transitions; this module is the pure
"run an LLM call to polish text" function.

Glossary block: locked terms are passed into the refiner so it doesn't
accidentally mutate them while polishing ("True Person Sea's Roar" →
"The True Person of Sea's Roar" would otherwise read as a legitimate
edit). The user's stated requirement "Keep genre terminology consistent"
relies on the refiner seeing the locked names.
"""

from __future__ import annotations

import logging

from backend.models import GlossaryEntry
from backend.services import llm_cache
from backend.services.glossary import dedupe_against_locked
from backend.services.providers import Provider
from backend.services.translators.base import (
    TransientTranslatorError,
    format_glossary,
)
from backend.services.translators.factory import get_translator

logger = logging.getLogger(__name__)


# System instruction the refiner runs under. Distinct from the translator's
# genre-aware system instruction: this voice is the editor's, not the
# translator's. Kept short and stable so the cache key is predictable.
#
# CRITICAL (2026-05-23): 3 of 4 backends ignore self.system_instruction in
# _complete_plain (gemini, claude_agent, claude_cli) — only DeepSeek reads
# it. To guarantee the editor role reaches every backend, this string is
# also folded into the user prompt at the top (see _REFINER_USER_TEMPLATE).
# The class-level system instruction is kept for backends that DO use it
# (DeepSeek) but is no longer load-bearing.
_REFINER_SYSTEM_INSTRUCTION = (
    "You are a meticulous literary editor of English novel prose. You take a "
    "draft translation and polish its surface: rhythm, sentence variety, "
    "dialogue and thought clarity, paragraphing. Preserve the draft's voice "
    "and force; do not flatten its vividness toward a plainer register, and "
    "do not push it further. The draft is the canonical text, so you trust "
    "its meaning and never re-translate, add, drop, reorder, or alter content "
    "or glossary terms."
)


# The user-supplied prompt template (rewritten 2026-06-06 from the user's
# style brief; kept genre-agnostic so every genre's refiner reads the same
# editor brief). Edit-only directives: surface polish, preserve everything
# else. The {glossary_block} placeholder names every glossary entry so the
# refiner preserves them. The editor role is folded in at the top because
# most backends' plain-text completion path doesn't forward
# system_instruction.
_REFINER_USER_TEMPLATE = """Your job is to rewrite the chapter below for clarity, flow, and novel readability while preserving the original meaning, tone, terminology, character names, and plot details. The draft has already been translated from the source; treat it as canonical and rewrite only its English surface.

Style:
- Polish the prose into smooth, natural English suitable for a novel.
- Keep the tone crisp and clean, and preserve the draft's own voice and register rather than flattening it toward a plainer style.
- Preserve all specialized and setting-specific terminology exactly unless it is clearly awkward or inconsistent.
- Preserve character names, place names, titles, and other proper nouns.
- Do not simplify worldbuilding or setting-specific terms; keep them exactly as the draft renders them.
- Avoid over-explaining.
- Avoid adding new information, new imagery, or new actions unless needed for grammar or clarity.
- Keep the passage close to the original, but make it smoother and more polished.
- Prefer vivid but clean phrasing.
- Break up long or tangled sentences when needed.
- Preserve paragraph breaks when they help pacing.
- Use standard novel dialogue formatting.

Return only the edited chapter, with no commentary, unless you hit a genuine ambiguity worth flagging.

GLOSSARY (preserve every entry exactly as written):
{glossary_block}

DRAFT PASSAGE TO EDIT:
{draft}"""


def _build_refiner_prompt(
    draft: str, glossary: list[GlossaryEntry] | None,
) -> str:
    # CRITICAL (2026-05-23 fix): the refiner must see BOTH locked AND
    # auto-detected entries, not just locked ones. The queue worker merges
    # the translator's new_terms into the glossary BEFORE the refiner runs,
    # so any auto-detected term the refiner is allowed to mutate would
    # leave the stored glossary entry pointing at text the reader never
    # sees. Pass everything; tell the refiner to preserve all of it.
    all_terms = dedupe_against_locked(glossary or [])
    glossary_block = format_glossary(
        all_terms, empty_label="(no glossary entries)"
    )
    return _REFINER_USER_TEMPLATE.format(
        glossary_block=glossary_block, draft=draft,
    )


async def refine_chapter(
    draft: str,
    provider: Provider,
    glossary: list[GlossaryEntry] | None = None,
    *,
    use_cache: bool = True,
) -> str:
    """Run the refiner against `draft` and return the polished text.

    The refiner uses the backend's `_complete_plain` hook because it has
    no envelope to parse — the output is the polished prose, full stop.
    """
    backend = get_translator(provider)
    prompt = _build_refiner_prompt(draft, glossary)
    cache_key = llm_cache.refinement_key(
        backend_id=backend.cache_identity(),
        system_instruction=_REFINER_SYSTEM_INSTRUCTION,
        draft_translation=prompt,
    )
    if use_cache:
        cached = llm_cache.load_refinement(cache_key)
        if cached is not None:
            logger.info(
                "refiner cache HIT (key %s…, provider=%s)",
                cache_key[:12], provider.name,
            )
            return cached
        logger.info(
            "refiner cache MISS (key %s…, provider=%s)",
            cache_key[:12], provider.name,
        )
    else:
        logger.info(
            "refiner cache SKIP (key %s…, provider=%s)",
            cache_key[:12], provider.name,
        )
    # Run the polish pass through the public editor seam: it stashes the
    # editor system instruction for backends that forward it (gemini,
    # deepseek, claude_agent) and calls the plain-completion hook, so the
    # refiner no longer reaches into the translator's protected state.
    refined = (await backend.complete_editor_pass(
        prompt, system_instruction=_REFINER_SYSTEM_INSTRUCTION,
    )).strip()
    if not refined:
        # Empty refiner output is a service-side failure the user can retry,
        # not a programming bug — use the domain exception the translator
        # backends and base.py terminal paths already raise.
        raise TransientTranslatorError(
            f"refiner ({provider.name}) returned empty output for a "
            f"{len(draft)}-char draft"
        )
    llm_cache.store_refinement(cache_key, refined)
    return refined
