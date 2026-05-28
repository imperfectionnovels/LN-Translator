"""OPUS-MT free-tier translator.

Wraps the lazy-loaded ``CTranslator`` from ``backend.services.opus_mt_models``
to give the rest of the pipeline a BaseTranslator-shaped surface. Unlike the
LLM backends, this class does NOT consume the literary prompt assembled by
``build_prompt`` — NMT models don't follow instructions, so the genre overlay,
glossary instruction, style brief, previous-chapter tail, and system
instruction would be wasted tokens (and the prompt header says "Chinese"
which is wrong for JA/KO). Instead we override ``translate_chapter`` to take
the inputs directly and produce a ``TranslationResult`` with ``degraded=True``
unconditionally.

The output is the OPUS-MT model's raw text, untouched. We do NOT try to
enforce glossary terminology in the NMT output. Terminology is the LLM PEMT
pass's responsibility: it receives the glossary as a separate input and
applies authoritative casing in the final user-visible translation. The
free-draft only needs to be a fidelity anchor (event order, named-entity
positions, quantities), which raw OPUS-MT output gives us.

Source-language safety: ``provider.model_id`` encodes the pair (``zh-en``,
``ja-en``, ``ko-en``). When the queue passes a chapter whose
``novels.source_language`` doesn't match this pair, we raise immediately
rather than silently translate Japanese through a Chinese model.
"""

from __future__ import annotations

import asyncio
import logging

from backend.models import GlossaryEntry, TranslationResult
from backend.services import opus_mt_models
from backend.services.providers import Provider

from .base import BaseTranslator

logger = logging.getLogger(__name__)


class OpusMTTranslator(BaseTranslator):
    """Free-tier offline NMT backend. CPU-only. Never burns API tokens."""

    name = "opus_mt"
    # max_parallel stays 1 — the OPUS-MT lock in services.free_draft_queue
    # serializes calls (CTranslate2 instances aren't thread-safe), and even
    # if multiple chapters' OPUS-MT work could run on different CPU cores,
    # we don't yet pay for that infrastructure.
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        if provider is None:
            raise RuntimeError(
                "OpusMTTranslator requires an explicit Provider row — configure "
                "one via /settings or onboarding."
            )
        if not provider.model_id:
            raise RuntimeError(
                f"Provider {provider.name!r} (opus_mt) is missing model_id. "
                "Set it to one of: zh-en, ja-en, ko-en."
            )
        if provider.model_id not in opus_mt_models.SUPPORTED_PAIRS:
            raise RuntimeError(
                f"Provider {provider.name!r} has unsupported opus_mt model_id "
                f"{provider.model_id!r}. Supported pairs: "
                f"{sorted(opus_mt_models.SUPPORTED_PAIRS)}."
            )
        self.model_id = provider.model_id  # e.g. "zh-en"
        self._provider_id = provider.id
        self._provider_name = provider.name

    # -- BaseTranslator abstract hooks (kept inert) --

    async def _complete(self, prompt: str) -> str:  # pragma: no cover
        raise NotImplementedError(
            "OpusMTTranslator overrides translate_chapter — _complete must not be reached."
        )

    async def _complete_plain(self, prompt: str) -> str:  # pragma: no cover
        raise NotImplementedError(
            "OpusMTTranslator overrides translate_chapter — _complete_plain must not be reached."
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
        """Translate ``chapter_zh`` via OPUS-MT and return the result.

        ``glossary``, ``previous_context``, ``style_edits``, ``style_note``,
        ``genre``, ``custom_brief``, ``free_draft`` are accepted to match the
        BaseTranslator surface but are deliberately ignored. NMT can't act on
        instructions, and the glossary is applied authoritatively by the LLM
        PEMT pass downstream; trying to enforce it here turned out to be net-
        negative (mangled placeholder leakage corrupted the draft, and a
        draft is only ever a fidelity anchor, not the final user-visible
        translation). When OPUS-MT is the main translator it *is* the free
        draft, so a passed-in ``free_draft`` would be its own previous output.

        ``source_language`` is validated against ``self.model_id``. ``None``
        is permissive (legacy callers that haven't been migrated yet); a
        mismatch raises with a clean error pointing the user at Settings.
        """
        if source_language is not None:
            expected = opus_mt_models.SUPPORTED_PAIRS[self.model_id].source_lang
            if source_language != expected:
                raise RuntimeError(
                    f"OPUS-MT provider {self._provider_name!r} is configured for "
                    f"{self.model_id!r} but this novel's source_language is "
                    f"{source_language!r}. Change the provider's model to the "
                    f"matching pair (e.g. {source_language}-en) in Settings, "
                    "or use a different provider for this novel."
                )

        # ``load_translator`` is synchronous + CPU-bound (model load is heavy
        # on the cold path). Run it in a thread so we don't block the event
        # loop while the C++ libraries warm up.
        ct = await asyncio.to_thread(opus_mt_models.load_translator, self.model_id)

        body = await asyncio.to_thread(self._translate_body, ct, chapter_zh)
        title_en = await asyncio.to_thread(self._translate_title, ct, title_zh)

        return TranslationResult(
            title_en=title_en or "(untitled)",
            translated_text=body,
            new_terms=[],
            degraded=True,
        )

    # -- Body / title translation --

    def _translate_body(
        self,
        ct: "opus_mt_models.CTranslator",
        chapter_zh: str,
    ) -> str:
        """Translate the chapter body. Raw OPUS-MT output, no post-processing."""
        paragraphs = opus_mt_models.segment_paragraphs(chapter_zh)
        # Flatten paragraphs into one batch so CTranslate2 batches them
        # together; track paragraph boundaries so we can reassemble after.
        flat_sentences: list[str] = []
        boundaries: list[int] = []
        for para in paragraphs:
            boundaries.append(len(flat_sentences))
            flat_sentences.extend(para)
        boundaries.append(len(flat_sentences))

        translated_sentences = ct.translate_batch(flat_sentences)

        rebuilt_paragraphs: list[list[str]] = []
        for i in range(len(boundaries) - 1):
            start, end = boundaries[i], boundaries[i + 1]
            rebuilt_paragraphs.append(translated_sentences[start:end])

        return opus_mt_models.reassemble(rebuilt_paragraphs)

    def _translate_title(
        self,
        ct: "opus_mt_models.CTranslator",
        title_zh: str | None,
    ) -> str:
        """Translate the title. Titles are short — treat as one sentence."""
        if not title_zh or not title_zh.strip():
            return ""
        translated = ct.translate_batch([title_zh.strip()])
        return (translated[0] if translated else "").strip()

    def cache_identity(self) -> str:
        """OPUS-MT does not share the LLM cache layer (translate_chapter is
        overridden and never calls llm_cache). This identity is kept for the
        rare diagnostic that wants to know which backend produced a row."""
        return f"opus_mt:{self.model_id}"
