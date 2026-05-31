"""Google Translate (free, unauthenticated) translator.

Wraps the ``deep_translator.GoogleTranslator`` class, which hits Google's
public web Translate endpoint without an API key. Free, no per-month quota,
~5K character limit per call, occasional IP throttling on bursty usage. For
single-user serial chapter translation under the free-draft lock this is a
viable replacement for the older OPUS-MT backend (which produced
unsalvageable output on literary CJK prose).

Like the OPUS-MT predecessor, this translator does NOT consume the literary
prompt assembled by ``build_prompt`` — MT models don't follow instructions
and the prompt header says "Chinese" which is wrong for JA/KO. Instead we
override ``translate_chapter`` to take the inputs directly and produce a
``TranslationResult`` with ``degraded=True`` unconditionally. Glossary
terminology is the LLM PEMT pass's job; this draft is a fidelity anchor.

Source language is taken from the novel's ``source_language`` (mapped via
``_lang_for_google``) and never from the provider's ``model_id`` — there is
only one ``model_id`` value (``google-web``), kept for catalog parity.

TOS note: the underlying endpoint is the public web Translate, not Google
Cloud Translation v3. The deep-translator library wraps it without
authentication. For single-user personal use this is well within the
informal "personal use" carve-out; do not run unattended large-batch jobs.
"""

from __future__ import annotations

import asyncio
import logging
import time

from backend.models import GlossaryEntry, TranslationResult
from backend.services.providers import Provider

from .base import BaseTranslator

logger = logging.getLogger(__name__)

# Conservative chunk size — Google's web endpoint accepts ~5000 chars per
# call; we leave room for inter-segment overhead and tokenization weirdness.
_CHUNK_LIMIT = 4500

# Google's public endpoint intermittently drops the connection or returns a
# transient request error under light load. Without a retry a single blip
# permanently stamps the chapter free_draft_status='error' (the live DB had 8
# such chapters, all with the same "api connection error" message) until the
# user manually refreshes. Retry transient failures a few times with linear
# backoff before surfacing the error. Runs inside a worker thread, so the
# blocking sleep here doesn't stall the event loop.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 1.5

# Map novel.source_language (ISO 639-1) → Google language code.
# Chinese is the special case: Google distinguishes zh-CN (Simplified) from
# zh-TW (Traditional). We default to zh-CN — the LLM in the PEMT pass sees
# the original CN and applies the canonical English term anyway.
_LANG_MAP = {
    "zh": "zh-CN",
    "ja": "ja",
    "ko": "ko",
}


def _lang_for_google(source_language: str | None) -> str:
    """Map a novel's source_language to a Google Translate source code.

    Unknown languages pass through as-is; deep-translator raises a clear
    error if Google doesn't recognize them, which the queue worker
    persists to ``free_draft_error``.
    """
    if not source_language:
        return "auto"
    return _LANG_MAP.get(source_language, source_language)


def _chunk_for_translate(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """Split ``text`` into chunks ≤ ``limit`` chars at paragraph boundaries.

    Preserves the original blank-line rhythm. A single paragraph larger than
    the limit is emitted as-is and deep-translator's per-call retry handles
    the truncation/failure. In practice cultivation-novel paragraphs are
    well under 2k chars, so the split-at-blank-line strategy is sufficient.
    """
    if not text:
        return []
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for para in paragraphs:
        added = len(para) + (2 if buf else 0)  # account for the joining "\n\n"
        if buf and buf_len + added > limit:
            chunks.append("\n\n".join(buf))
            buf = [para]
            buf_len = len(para)
        else:
            buf.append(para)
            buf_len += added
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


class GoogleTranslateFreeTranslator(BaseTranslator):
    """Free-tier online MT backend. Requires internet, no API key."""

    name = "google_translate_free"
    # max_parallel stays 1 — Google's public endpoint throttles burst traffic
    # per IP. The free-draft queue's process-global FREE_DRAFT_LOCK already
    # serializes these calls; this is a belt-and-suspenders marker for any
    # future caller that reads max_parallel.
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        if provider is None:
            raise RuntimeError(
                "GoogleTranslateFreeTranslator requires an explicit Provider row — "
                "configure one via /settings or onboarding."
            )
        # model_id is required for catalog parity but the only valid value is
        # "google-web" (we deliberately don't expose per-language pairs —
        # source language comes from the novel, not the provider).
        if not provider.model_id:
            raise RuntimeError(
                f"Provider {provider.name!r} (google_translate_free) is missing "
                "model_id. Set it to 'google-web'."
            )
        self.model_id = provider.model_id
        self._provider_id = provider.id
        self._provider_name = provider.name

    # -- BaseTranslator abstract hooks (kept inert) --

    async def _complete(self, prompt: str) -> str:  # pragma: no cover
        raise NotImplementedError(
            "GoogleTranslateFreeTranslator overrides translate_chapter — "
            "_complete must not be reached."
        )

    async def _complete_plain(self, prompt: str) -> str:  # pragma: no cover
        raise NotImplementedError(
            "GoogleTranslateFreeTranslator overrides translate_chapter — "
            "_complete_plain must not be reached."
        )

    # -- Main entry point --

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
        """Translate ``chapter_zh`` via Google Translate and return the result.

        ``glossary``, ``previous_context``, ``style_edits``, ``style_note``,
        ``genre``, ``custom_brief``, ``free_draft`` are accepted to match the
        BaseTranslator surface but are deliberately ignored. MT can't act on
        instructions, and the glossary is applied authoritatively by the LLM
        PEMT pass downstream.
        """
        src = _lang_for_google(source_language)

        body = await asyncio.to_thread(self._translate_body, chapter_zh, src)
        title_en = await asyncio.to_thread(self._translate_title, title_zh, src)

        return TranslationResult(
            title_en=title_en or "(untitled)",
            translated_text=body,
            new_terms=[],
            degraded=True,
        )

    # -- Body / title translation --

    def _translate_body(self, chapter_zh: str, src: str) -> str:
        """Translate the chapter body in paragraph-bounded chunks."""
        if not chapter_zh or not chapter_zh.strip():
            return ""
        chunks = _chunk_for_translate(chapter_zh.strip())
        translated = [self._translate_chunk(chunk, src) for chunk in chunks]
        return "\n\n".join(t for t in translated if t)

    def _translate_title(self, title_zh: str | None, src: str) -> str:
        """Translate the title in one call. Titles are always under the limit."""
        if not title_zh or not title_zh.strip():
            return ""
        return self._translate_chunk(title_zh.strip(), src).strip()

    def _translate_chunk(self, text: str, src: str) -> str:
        """One Google Translate call. Network / throttle errors raise a
        clean RuntimeError the queue worker persists to ``free_draft_error``.
        """
        # Imported lazily so a missing dep at boot doesn't break the rest of
        # the app — the catalog still loads, the user sees a clear error
        # when they try to use this provider.
        try:
            from deep_translator import GoogleTranslator
            from deep_translator.exceptions import (
                NotValidPayload,
                RequestError,
                TooManyRequests,
                TranslationNotFound,
            )
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "deep-translator is not installed. Run `pip install deep-translator` "
                "to enable the Google Translate free-tier backend."
            ) from exc

        translator = GoogleTranslator(source=src, target="en")
        # NotValidPayload is a deterministic input error — never worth a retry.
        # RequestError / TranslationNotFound are usually a transient endpoint
        # blip; retry those with backoff. TooManyRequests means we are already
        # throttled, so retrying immediately would make it worse: surface it.
        last_request_err: Exception | None = None
        for attempt in range(1, _MAX_TRANSIENT_RETRIES + 1):
            try:
                result = translator.translate(text)
                return result or ""
            except TooManyRequests as exc:
                raise RuntimeError(
                    "Google Translate rate-limited this IP. Wait a few minutes "
                    "and retry, or switch to an LLM provider for this chapter."
                ) from exc
            except NotValidPayload as exc:
                raise RuntimeError(
                    f"Google Translate rejected the input: {exc}."
                ) from exc
            except (RequestError, TranslationNotFound) as exc:
                last_request_err = exc
                if attempt < _MAX_TRANSIENT_RETRIES:
                    logger.info(
                        "google-translate transient failure (attempt %d/%d), "
                        "retrying: %s",
                        attempt, _MAX_TRANSIENT_RETRIES, exc,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                    continue
            except Exception as exc:
                raise RuntimeError(
                    f"Google Translate raised an unexpected error: {exc!r}."
                ) from exc
        raise RuntimeError(
            f"Google Translate request failed after {_MAX_TRANSIENT_RETRIES} "
            f"attempts: {last_request_err}. Check internet connectivity, or "
            "switch to an LLM provider."
        ) from last_request_err

    def cache_identity(self) -> str:
        """Google Translate does not share the LLM cache layer (translate_chapter
        is overridden and never calls llm_cache). This identity is kept for the
        rare diagnostic that wants to know which backend produced a row."""
        return f"google_translate_free:{self.model_id}"
