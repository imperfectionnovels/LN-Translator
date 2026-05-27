"""DeepSeek-backed translator. Uses the OpenAI-compatible API at
api.deepseek.com — calls go through the official `openai` async SDK with a
base_url override. DeepSeek does automatic prompt caching server-side, so the
glossary-heavy prefix gets cached transparently across chapters and the
per-chapter cost drops to a fraction of a cent on cache hits.

Quality model — this backend deliberately does NOT use JSON mode. DeepSeek's
free-form generation is markedly better than its `response_format=json_object`
output for long literary prose: JSON mode forces the whole chapter body into
one escaped string, which degrades the writing and raises the odds of
truncation / dropped passages. Instead the chapter body is emitted as raw text
inside a delimited envelope (see `_DELIMITED_OUTPUT_INSTRUCTION` /
`_parse_deepseek_response`).

On top of that, each chapter runs a translate → reflect → improve revision
pass (gated on `DEEPSEEK_REVISION_ENABLED`): the draft is critiqued against the
Chinese source and then corrected. This is the main lever for fidelity and
fluency — the translation is the final published text, so the revision pass
has to land it both correct and natural; nothing downstream repairs it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx
import openai

from backend.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_DRAFT_MODEL,
    DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEEPSEEK_REQUEST_TIMEOUT,
    DEEPSEEK_REVISION_ENABLED,
    DEEPSEEK_REVISION_MODE,
    DEEPSEEK_TRANSLATOR_MODEL,
    DEEPSEEK_TRANSLATOR_TEMPERATURE,
    DEFAULT_GENRE,
)
from backend.genres import resolve_genre
from backend.models import GlossaryEntry, NewTerm, TokenUsage, TranslationResult
from backend.services import llm_cache
from backend.services.providers import Provider, resolve_secret

from .base import (
    DELIMITED_OUTPUT_INSTRUCTION,
    BaseTranslator,
    TransientTranslatorError,
    _strip_code_fence,
    build_prompt,
    build_system_instruction,
)
from .deepseek_revise import (
    _IMPROVE_TEMPERATURE,
    _REFLECT_TEMPERATURE,
    _REVIEWER_SYSTEM_INSTRUCTION,
    _build_improve_prompt,
    _build_reflect_prompt,
    _build_revise_prompt,
    _glossary_block,
    _is_no_issues,
)

logger = logging.getLogger(__name__)

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Output-token ceiling for the draft pass specifically. The draft model
# (DEEPSEEK_DRAFT_MODEL, default deepseek-chat) is non-reasoning and caps lower
# than the v4-pro reasoning model — passing the larger DEEPSEEK_MAX_OUTPUT_TOKENS
# would risk an API rejection. 8192 is ample for a draft: a ~3000-word chapter
# is ~4-6k visible tokens, and the draft emits no reasoning tokens. The reflect
# and improve passes still use DEEPSEEK_MAX_OUTPUT_TOKENS (they need headroom
# for the reasoning model's chain-of-thought).
_DRAFT_MAX_OUTPUT_TOKENS = 8192

# Transient-error backoff for DeepSeek calls. One retry only: a per-request
# timeout already bounds each attempt (DEEPSEEK_REQUEST_TIMEOUT), and a
# transient failure that doesn't clear on a single retry won't clear on three
# — better to error the chapter quickly than stretch the pipeline by ~16 min.
_DEEPSEEK_BACKOFF = (5.0,)

# Delimited free-form response envelope. The body sits as raw text between
# delimiters — no JSON escaping — which is what keeps DeepSeek's prose quality
# intact. Picked to be extremely unlikely to occur in real translated prose.
_BODY_DELIM = "=====BODY====="
_TERMS_DELIM = "=====TERMS====="

# DeepSeek shares the genre-aware system instruction with every other backend
# via BaseTranslator.translate_chapter, which sets self.system_instruction
# from (genre, custom_brief) before invoking _complete. Use self.system_instruction
# everywhere a constant was tempting. _REVIEWER_SYSTEM_INSTRUCTION (for the
# reflect pass — editor voice, not translator voice) lives in deepseek_revise.py.


def _is_transient(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,
        ),
    ):
        return True
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and (status == 408 or status == 429 or status >= 500):
            return True
        return False
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    return False


def _log_deepseek_usage(label: str, response: object) -> None:
    """Log per-call token usage for a DeepSeek response.

    The key signal when a short chapter unexpectedly hits the max_tokens
    ceiling is `completion_tokens_details.reasoning_tokens` — on a
    reasoning-capable model the chain-of-thought counts toward the same output
    budget as the visible answer, so a high reasoning count with a small
    visible body means the model is over-thinking, not that the chapter is
    long. `cached_tokens` shows DeepSeek's automatic prompt-cache hit on the
    glossary-heavy prefix."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", None) or 0
    completion = getattr(usage, "completion_tokens", None) or 0
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = (getattr(details, "reasoning_tokens", None) or 0) if details else 0
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(prompt_details, "cached_tokens", None) or 0) if prompt_details else 0
    logger.info(
        "deepseek %s usage: prompt=%d (cached=%d), completion=%d (reasoning=%d)",
        label, prompt, cached, completion, reasoning,
    )


def _unwrap_outer_fence(text: str) -> str:
    """Strip a single code fence wrapping the WHOLE response, if present.

    Unlike base.py's `_strip_code_fence` (which returns the inner content of
    the last fenced block), this only peels an outer wrapper — it must not be
    applied to the full delimited envelope, because a model that fences just
    the trailing TERMS JSON would otherwise have its title + body discarded."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl != -1:
        t = t[first_nl + 1:]
    t = t.rstrip()
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


def _parse_terms(raw: str) -> list[NewTerm]:
    """Best-effort parse of the TERMS JSON array. Any failure → empty list:
    new-term extraction is a nice-to-have, never a reason to fail a chapter."""
    raw = _strip_code_fence(raw.strip())
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("DeepSeek: could not parse TERMS block — dropping new_terms")
        return []
    if not isinstance(data, list):
        return []
    terms: list[NewTerm] = []
    for t in data:
        if not (isinstance(t, dict) and t.get("zh") and t.get("en")):
            continue
        try:
            terms.append(NewTerm(**t))
        except Exception:
            logger.warning("DeepSeek: dropping malformed new_term: %r", t)
    return terms


def _parse_deepseek_response(
    raw: str, *, expect_terms: bool = True
) -> TranslationResult:
    """Parse the delimited free-form envelope into a TranslationResult.

    Raises ValueError when the BODY delimiter or the body text is missing, so
    the caller's retry-then-fallback path engages. A missing or malformed TERMS
    block is tolerated (new_terms → []). `expect_terms=False` is used for the
    improve pass, whose envelope has no TERMS section."""
    text = _unwrap_outer_fence(raw)
    if _BODY_DELIM not in text:
        raise ValueError("DeepSeek response missing the BODY delimiter")
    head, _, rest = text.partition(_BODY_DELIM)
    title_match = re.search(
        r"TITLE_EN\s*:\s*(.+?)\s*$", head.strip(), re.MULTILINE
    )
    title_en = (title_match.group(1).strip() if title_match else "") or "(untitled)"
    if _TERMS_DELIM in rest:
        body, _, terms_raw = rest.partition(_TERMS_DELIM)
    else:
        body, terms_raw = rest, ""
    body = body.strip()
    if not body:
        raise ValueError("DeepSeek response missing body text")
    new_terms = _parse_terms(terms_raw) if expect_terms else []
    return TranslationResult(
        title_en=title_en, translated_text=body, new_terms=new_terms
    )


class DeepSeekTranslator(BaseTranslator):
    name = "deepseek"
    model_id = DEEPSEEK_TRANSLATOR_MODEL
    # Forced serial. Matches every other backend in this project — the queue's
    # process-global lock enforces serial regardless of what we put here, and
    # the user is paying per-token so we don't speculatively parallelize.
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        # When a Provider is passed, its model_id, base_url, and env-resolved
        # secret take precedence over the legacy DEEPSEEK_* globals. Two
        # providers of provider_type=deepseek can point at different models
        # (deepseek-chat vs deepseek-v4-pro) and the LLM-cache key picks up
        # the difference via self.model_id.
        if provider is not None:
            # Explicit Provider: do NOT fall back to DEEPSEEK_API_KEY. Bad
            # provider config must surface as an error rather than silently
            # running under the legacy global key.
            api_key = resolve_secret(provider)
            self.model_id = provider.model_id or DEEPSEEK_TRANSLATOR_MODEL
            base_url = provider.base_url or _DEEPSEEK_BASE_URL
            # DeepSeek's two-call workflow: a cheap "draft" pass and a smarter
            # "revise" pass. The Provider's model_id drives the revise call.
            # params.draft_model overrides the draft model independently;
            # otherwise the draft runs on the same model_id so per-provider
            # routing is end-to-end (no DEEPSEEK_DRAFT_MODEL leak).
            self._draft_model = (provider.params or {}).get(
                "draft_model"
            ) or self.model_id
            if not api_key:
                raise RuntimeError(
                    f"Provider {provider.name!r} (deepseek) has no resolvable "
                    f"API key. Set the env var named in its secret_ref "
                    f"({provider.secret_ref!r}) or update the provider row."
                )
        else:
            api_key = DEEPSEEK_API_KEY
            self.model_id = DEEPSEEK_TRANSLATOR_MODEL
            base_url = _DEEPSEEK_BASE_URL
            # Legacy path: honor DEEPSEEK_DRAFT_MODEL so existing .env files
            # keep their cheap-draft + expensive-revise behavior.
            self._draft_model = DEEPSEEK_DRAFT_MODEL
            if not api_key:
                raise RuntimeError(
                    "DeepSeek API key is not set. Configure a DeepSeek provider "
                    "in /settings, or set DEEPSEEK_API_KEY in .env for the "
                    "legacy default path."
                )
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=DEEPSEEK_REQUEST_TIMEOUT,
        )

    def cache_identity(self) -> str:
        """Beyond name + model, fold in the settings that change the *output*
        for identical chapter inputs: the revision toggle, the draft
        temperature, and the draft model. Without this, flipping
        DEEPSEEK_REVISION_ENABLED, changing DEEPSEEK_TRANSLATOR_TEMPERATURE, or
        switching DEEPSEEK_DRAFT_MODEL would return a cached result produced
        under the old settings. (DEEPSEEK_MAX_OUTPUT_TOKENS is left out: it
        never changes a non-truncated result, and truncated results are never
        cached.)"""
        # The "revN" token also serves as a manual cache-buster for the
        # revision prompts: those prompts are NOT part of the cache key (only
        # the draft prompt + system instruction are), so bump this token
        # whenever _build_reflect_prompt / _build_improve_prompt /
        # _build_revise_prompt change. The token also encodes the revision
        # MODE so flipping DEEPSEEK_REVISION_MODE can't return a cached result
        # produced under the other revision structure.
        # rev2: expanded reflect focus list (physical-detail fidelity, flat
        # word choice, within-chapter consistency, missing italics).
        # rev2s: single-pass combined critique+rewrite (_build_revise_prompt).
        # rev3: added focus item 6 — AI-added content & filler audit (invented
        # connectives, in-line glosses, adjective inflation, AI-tell vocab).
        # rev4: native-English re-scope — item 2 now flags mechanical
        # translation artifacts (repeated names, identical tics, exclamation
        # density) as fixable, not preserved.
        # rev5: novel-wide consistency — recurring term / name / epithet drift,
        # title-order drift, and realm-stacked-on-group compounds added to the
        # reflect and revise checklists.
        # rev6: added focus item 7 — genre-register audit (no westernization
        # into medieval-epic / YA / LitRPG prose) to all three prompts; the
        # shared WORKED_EXAMPLES block dropped its malformed clan example.
        # rev7: shared base.py WORKED_EXAMPLES gained said-bookism + two calque
        # cases + honorific-flattening case, and SYSTEM_INSTRUCTION gained the
        # render-don't-invent / pronoun-symmetry / dialogue-tag-fidelity /
        # three-register-voice / honorifics-preservation / conditional-rhythm /
        # trailing-thought-punctuation rules. SYSTEM_INSTRUCTION is in the cache
        # key independently, but this token covers the WORKED_EXAMPLES change
        # (folded into the improve/revise prompts) explicitly.
        if not DEEPSEEK_REVISION_ENABLED:
            revision = "rev0"
        elif DEEPSEEK_REVISION_MODE == "single":
            revision = "rev7s"
        else:
            revision = "rev7"
        # The draft pass may run on a different (faster) model than the
        # reflect/improve passes. Fold it in so a draft-model change
        # invalidates stale entries. Omitted when it equals the translator
        # model so the single-model setup keeps its existing cache keys.
        draft = (
            f":d{self._draft_model}"
            if self._draft_model != self.model_id
            else ""
        )
        return (
            f"{self.name}:{self.model_id}:{revision}"
            f":t{DEEPSEEK_TRANSLATOR_TEMPERATURE:g}{draft}"
        )

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
        """DeepSeek-specific flow: free-form delimited draft, then an optional
        translate → reflect → improve revision pass."""
        # Reset the per-chapter LLM call counter + usage accumulator at
        # the start of each translate_chapter call. DeepSeek overrides this
        # method entirely so the reset has to happen here too (Base does
        # not run for this backend). _check_call_budget() ticks once per
        # _call_deepseek; _emit_usage() folds in token counts from each
        # SDK response (draft + optional reflect + improve passes).
        self._llm_call_count = 0
        self._usage_accumulator = TokenUsage()
        # Build the genre-aware system instruction before the cache key, same
        # pattern as BaseTranslator.translate_chapter — DeepSeek overrides
        # translate_chapter entirely for its revision pipeline, so the stash
        # has to happen here too. Also remember the resolved genre for the
        # _revise helpers (so they pull the right worked examples).
        # CRITICAL: _genre must use the same resolution path as the system
        # instruction, NOT `genre or "generic"`. Otherwise a NULL genre gets
        # `DEFAULT_GENRE` (xianxia) in the system prompt but "generic" in the
        # revision examples — inconsistent guidance to two stages of the same
        # call. resolve_genre folds in DEFAULT_GENRE the same way base.py does.
        self.system_instruction = build_system_instruction(genre, custom_brief)
        self._genre = resolve_genre(genre, DEFAULT_GENRE)
        prompt = build_prompt(
            chapter_zh,
            title_zh,
            glossary,
            previous_context,
            style_edits,
            output_instruction=DELIMITED_OUTPUT_INSTRUCTION,
            style_note=style_note,
            free_draft=free_draft,
        )
        cache_key = llm_cache.translation_key(
            backend_id=self.cache_identity(),
            system_instruction=self.system_instruction,
            prompt=prompt,
        )
        # use_cache=False (an explicit Retranslate) skips the read but still
        # stores the fresh result below, so the cache stays warm afterward.
        if use_cache:
            cached = llm_cache.load_translation(cache_key)
            if cached is not None:
                logger.info("deepseek translator cache HIT (key %s…)", cache_key[:12])
                return cached
            logger.info("deepseek translator cache MISS (key %s…)", cache_key[:12])
        else:
            logger.info(
                "deepseek translator cache SKIP (force_retranslate, key %s…)",
                cache_key[:12],
            )

        draft, used_fallback = await self._translate_draft(
            prompt, chapter_zh, title_zh
        )

        result = draft
        revision_ok = True
        if DEEPSEEK_REVISION_ENABLED:
            if DEEPSEEK_REVISION_MODE == "single":
                result, revision_ok = await self._revise_single(
                    chapter_zh, glossary, draft
                )
            else:
                result, revision_ok = await self._revise(
                    chapter_zh, glossary, draft
                )

        # Skip caching a degraded result. The plain-text fallback drops
        # new_terms; an incomplete revision pass would otherwise be frozen in
        # the cache, so every later Retranslate would return the un-revised
        # draft and never re-attempt revision.
        # Cache the result WITHOUT usage so future cache hits don't replay
        # token counts from the original call (matches base.py behavior).
        if not used_fallback and revision_ok:
            llm_cache.store_translation(cache_key, result)
        elif not result.degraded:
            # A fallback draft or an incomplete revision pass — flag it so the
            # reader can surface a degraded-translation banner.
            result = result.model_copy(update={"degraded": True})
        return self._attach_usage(result)

    async def _translate_draft(
        self, prompt: str, chapter_zh: str, title_zh: str | None
    ) -> tuple[TranslationResult, bool]:
        """Run the draft translation. Retry once on a malformed envelope, then
        fall back to the base plain-text translation. Returns
        (result, used_fallback)."""
        for attempt in range(2):
            try:
                raw = await self._call_deepseek(
                    prompt,
                    label="draft",
                    model=self._draft_model,
                    max_tokens=_DRAFT_MAX_OUTPUT_TOKENS,
                )
                return _parse_deepseek_response(raw), False
            except (ValueError, json.JSONDecodeError) as e:
                # Covers a malformed envelope AND a non-transient ValueError
                # from the API call (e.g. "no choices"). TransientTranslatorError
                # — transient exhaustion or a truncated response — is not a
                # ValueError, so it propagates and errors the chapter instead.
                logger.warning(
                    "deepseek draft call/parse failed (attempt %d): %s",
                    attempt + 1,
                    e,
                )
        logger.warning("deepseek draft falling back to plain-text translation")
        return await self._plain_text_fallback(chapter_zh, title_zh), True

    async def _revise(
        self,
        chapter_zh: str,
        glossary: list[GlossaryEntry],
        draft: TranslationResult,
    ) -> tuple[TranslationResult, bool]:
        """translate → reflect → improve. Returns (result, revision_ok).

        `revision_ok` is True when the pass completed — either it produced an
        improved translation, or the reflect pass legitimately found nothing to
        fix. It is False only when a reflect / improve call failed (transient
        or parse error), in which case the result degrades to the draft and the
        caller must NOT cache it (so a later Retranslate re-attempts revision).
        Revision is an enhancement, never a correctness requirement."""
        glossary_block = _glossary_block(glossary, chapter_zh)
        try:
            reflect_prompt = _build_reflect_prompt(
                chapter_zh, draft.translated_text, glossary_block,
                genre=getattr(self, "_genre", None) or resolve_genre(None, DEFAULT_GENRE),
            )
            suggestions = (
                await self._call_deepseek(
                    reflect_prompt,
                    system=_REVIEWER_SYSTEM_INSTRUCTION,
                    temperature=_REFLECT_TEMPERATURE,
                    label="reflect",
                )
            ).strip()
            if not suggestions or _is_no_issues(suggestions):
                logger.info("deepseek revision: reflect pass found no issues")
                return draft, True
            improve_prompt = _build_improve_prompt(
                chapter_zh, draft.translated_text, suggestions, glossary_block,
                genre=getattr(self, "_genre", None) or resolve_genre(None, DEFAULT_GENRE),
            )
            raw = await self._call_deepseek(
                improve_prompt, temperature=_IMPROVE_TEMPERATURE, label="improve"
            )
            improved = _parse_deepseek_response(raw, expect_terms=False)
        except TransientTranslatorError as e:
            logger.warning("deepseek revision skipped (transient error): %s", e)
            return draft, False
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("deepseek revision skipped (parse error): %s", e)
            return draft, False
        # The improve envelope omits TERMS, so carry new_terms from the draft;
        # a polish pass does not change which 【】 terms the chapter contains.
        title_en = (
            improved.title_en
            if improved.title_en != "(untitled)"
            else draft.title_en
        )
        revised = TranslationResult(
            title_en=title_en,
            translated_text=improved.translated_text,
            new_terms=draft.new_terms,
        )
        return revised, True

    async def _revise_single(
        self,
        chapter_zh: str,
        glossary: list[GlossaryEntry],
        draft: TranslationResult,
    ) -> tuple[TranslationResult, bool]:
        """Single-call revision: one combined critique-and-rewrite pass.

        Faster than `_revise` — one LLM round-trip instead of reflect +
        improve. The prompt asks the model to review the draft against the
        source, then emit the corrected translation in the same step. Same
        `(result, revision_ok)` contract as `_revise`: `revision_ok` is False
        only on a transient / parse failure, in which case the result degrades
        to the draft and the caller must NOT cache it (so a later Retranslate
        re-attempts revision)."""
        glossary_block = _glossary_block(glossary, chapter_zh)
        try:
            revise_prompt = _build_revise_prompt(
                chapter_zh, draft.translated_text, glossary_block,
                genre=getattr(self, "_genre", None) or resolve_genre(None, DEFAULT_GENRE),
            )
            raw = await self._call_deepseek(
                revise_prompt, temperature=_IMPROVE_TEMPERATURE, label="revise"
            )
            improved = _parse_deepseek_response(raw, expect_terms=False)
        except TransientTranslatorError as e:
            logger.warning(
                "deepseek single-pass revision skipped (transient error): %s", e
            )
            return draft, False
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(
                "deepseek single-pass revision skipped (parse error): %s", e
            )
            return draft, False
        # The revise envelope omits TERMS, so carry new_terms from the draft;
        # a polish pass does not change which 【】 terms the chapter contains.
        title_en = (
            improved.title_en
            if improved.title_en != "(untitled)"
            else draft.title_en
        )
        revised = TranslationResult(
            title_en=title_en,
            translated_text=improved.translated_text,
            new_terms=draft.new_terms,
        )
        return revised, True

    # `translate_chapter` above drives the real flow. These two hooks satisfy
    # BaseTranslator's ABC; `_complete_plain` also backs the plain-text
    # fallback in `_translate_draft`. Both are draft-stage, so they run on the
    # draft model with the draft token cap.
    async def _complete(self, prompt: str) -> str:
        return await self._call_deepseek(
            prompt, label="fallback",
            model=self._draft_model, max_tokens=_DRAFT_MAX_OUTPUT_TOKENS,
        )

    async def _complete_plain(self, prompt: str) -> str:
        return await self._call_deepseek(
            prompt, label="fallback",
            model=self._draft_model, max_tokens=_DRAFT_MAX_OUTPUT_TOKENS,
        )

    async def _call_deepseek(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        label: str = "draft",
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        # Per-chapter budget gate. DeepSeek's revision flow (draft + reflect
        # + improve, with a parse-retry on draft) reaches 4 calls in normal
        # operation, so MAX_LLM_CALLS_PER_CHAPTER=4 just fits. Going over is
        # the regression signal — surface it as a clean error rather than
        # let the loop drift.
        self._check_call_budget()
        # Default the system instruction to whatever translate_chapter set
        # for this call (genre-aware). Callers can override (reflect/improve
        # passes use _REVIEWER_SYSTEM_INSTRUCTION).
        if system is None:
            system = self.system_instruction
        if temperature is None:
            temperature = DEEPSEEK_TRANSLATOR_TEMPERATURE
        if model is None:
            model = self.model_id
        if max_tokens is None:
            max_tokens = DEEPSEEK_MAX_OUTPUT_TOKENS
        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        t0 = time.perf_counter()
        last_exc: BaseException | None = None
        for attempt in range(len(_DEEPSEEK_BACKOFF) + 1):
            try:
                response = await self._client.chat.completions.create(**kwargs)
                choices = response.choices or []
                if not choices:
                    raise ValueError("DeepSeek returned no choices")
                choice = choices[0]
                # Log usage regardless of outcome — on a truncation this is
                # the only place the reasoning/visible token split is visible.
                _log_deepseek_usage(label, response)
                # Plumb usage into the BaseTranslator accumulator. DeepSeek's
                # OpenAI-compatible API doesn't expose a cached-input concept;
                # cached_input_tokens stays 0 here. Accumulates across draft
                # + reflect + improve passes.
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self._emit_usage(
                        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    )
                if choice.finish_reason == "length":
                    # Output hit the token cap — the body is cut off mid-text.
                    # Retrying yields the same truncation, so fail loudly with a
                    # clear message rather than committing a partial chapter.
                    # TransientTranslatorError is not transient per _is_transient,
                    # so the handler below re-raises it without retrying. The
                    # `label` names which pass (draft / revise / reflect /
                    # improve) blew the budget.
                    raise TransientTranslatorError(
                        f"DeepSeek {label} pass output was cut off at the token "
                        f"limit ({max_tokens} tokens). The chapter "
                        "is unchanged. If the chapter is genuinely long, raise "
                        "the relevant max-output-tokens setting; if it is short, "
                        "the model is looping or over-reasoning — check the "
                        f"'deepseek {label} usage' log line for the "
                        "reasoning/completion token split. Then Retranslate."
                    )
                logger.info(
                    "deepseek %s call: %.1fs", label, time.perf_counter() - t0
                )
                return choice.message.content or ""
            except Exception as e:
                if not _is_transient(e):
                    raise
                last_exc = e
                if attempt >= len(_DEEPSEEK_BACKOFF):
                    break
                delay = _DEEPSEEK_BACKOFF[attempt]
                logger.warning(
                    "DeepSeek transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    len(_DEEPSEEK_BACKOFF) + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        status = getattr(last_exc, "status_code", None)
        pretty = (
            f"DeepSeek temporarily unavailable ({status or 'transient error'}). "
            "The chapter is unchanged — try Retranslate later."
        )
        raise TransientTranslatorError(pretty) from last_exc


async def _probe_deepseek_model(
    client: openai.AsyncOpenAI, model: str, *, role: str
) -> None:
    """Cheap round-trip for one model. Raises RuntimeError on a permanent
    misconfiguration (bad key / unknown model); returns quietly on a transient
    error so a flaky network doesn't block server boot. `role` ('translator' /
    'draft') names which setting to fix in the error message."""
    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ok"}],
            max_tokens=1,
        )
    except openai.AuthenticationError as e:
        raise RuntimeError(
            f"DeepSeek auth failed: {e}. Check DEEPSEEK_API_KEY in .env."
        ) from e
    except openai.NotFoundError as e:
        raise RuntimeError(
            f"DeepSeek {role} model {model!r} not found: {e}. Check the model "
            "name in .env — see https://api-docs.deepseek.com for the current "
            "model list."
        ) from e
    except openai.BadRequestError as e:
        # 400 here usually means the model name is invalid (DeepSeek returns
        # 400 instead of 404 for unknown models in some cases).
        raise RuntimeError(
            f"DeepSeek rejected probe for {role} model {model!r}: {e}. "
            "Check the model name in .env."
        ) from e
    except Exception as e:
        if _is_transient(e):
            logger.warning(
                "DeepSeek probe TRANSIENT failure for %s model %r: %s. Starting "
                "anyway — first real call will retry.",
                role, model, e,
            )
            return
        raise RuntimeError(
            f"DeepSeek probe failed for {role} model {model!r}: {e}."
        ) from e
    logger.info("DeepSeek probe ok (%s model=%s)", role, model)


async def probe_deepseek(provider: Provider | None = None) -> None:
    """Cheap startup round-trip. Fails loudly on bad key / unknown model so
    misconfiguration surfaces at boot, not on the first user-triggered
    translation. Probes the draft model too when it differs from the translator
    model. Transient errors are logged and let the server start.

    When `provider` is set, the probe targets the provider's secret/model/
    base_url (matching what the queue worker will actually call), NOT the
    legacy DEEPSEEK_* globals. A bad provider configuration must fail boot
    instead of being masked by an env var that happens to be set.
    """
    if provider is not None:
        api_key = resolve_secret(provider)
        if not api_key:
            raise RuntimeError(
                f"Default provider {provider.name!r} (deepseek) has no "
                f"resolvable API key. Set the env var named in its "
                f"secret_ref ({provider.secret_ref!r}) or update the row "
                f"in /api/providers."
            )
        base_url = provider.base_url or _DEEPSEEK_BASE_URL
        translator_model = provider.model_id or DEEPSEEK_TRANSLATOR_MODEL
        draft_model = (provider.params or {}).get("draft_model") or translator_model
    else:
        if not DEEPSEEK_API_KEY:
            raise RuntimeError(
                "TRANSLATOR_BACKEND=deepseek but DEEPSEEK_API_KEY is empty. "
                "Set DEEPSEEK_API_KEY in .env or configure a DeepSeek "
                "provider in /settings."
            )
        api_key = DEEPSEEK_API_KEY
        base_url = _DEEPSEEK_BASE_URL
        translator_model = DEEPSEEK_TRANSLATOR_MODEL
        draft_model = DEEPSEEK_DRAFT_MODEL
    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    await _probe_deepseek_model(client, translator_model, role="translator")
    if draft_model != translator_model:
        await _probe_deepseek_model(client, draft_model, role="draft")
