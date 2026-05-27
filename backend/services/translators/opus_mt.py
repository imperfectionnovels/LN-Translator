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

Locked-glossary terminology is applied via placeholder substitution:
    1. Find each locked glossary entry whose ``term_zh`` appears in the source.
    2. Replace each with a unique sentinel (e.g. ``ZX001``).
    3. Translate.
    4. Restore sentinels to the locked ``term_en``.

If a sentinel doesn't survive into the output (SentencePiece split it, or the
model dropped/rearranged it), the literal sentinel is left in place and logged
at WARNING. Visible failure beats silent mistranslation for the glossary-
critical "this character's name must always be X" case.

Source-language safety: ``provider.model_id`` encodes the pair (``zh-en``,
``ja-en``, ``ko-en``). When the queue passes a chapter whose
``novels.source_language`` doesn't match this pair, we raise immediately
rather than silently translate Japanese through a Chinese model.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable

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

        ``previous_context``, ``style_edits``, ``style_note``, ``genre``,
        ``custom_brief``, ``free_draft`` are accepted to match the
        BaseTranslator surface but are deliberately ignored — NMT can't act
        on them, and when OPUS-MT is the main translator it *is* the free
        draft, so a passed-in ``free_draft`` would be its own previous output.
        The free-tier result is the same regardless of those inputs; the LLM
        PEMT pass layered on top is where they take effect.

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

        body, leaked_sentinels = await asyncio.to_thread(
            self._translate_body, ct, chapter_zh, glossary,
        )
        title_en = await asyncio.to_thread(
            self._translate_title, ct, title_zh, glossary,
        )

        if leaked_sentinels:
            logger.warning(
                "OPUS-MT translator %r emitted %d output(s) with unrestored "
                "sentinels: %s. Locked glossary terminology may be wrong on "
                "those occurrences.",
                self.model_id, len(leaked_sentinels), leaked_sentinels[:5],
            )

        return TranslationResult(
            title_en=title_en or "(untitled)",
            translated_text=body,
            new_terms=[],
            degraded=True,
        )

    # -- Body translation with glossary placeholder substitution --

    def _translate_body(
        self,
        ct: "opus_mt_models.CTranslator",
        chapter_zh: str,
        glossary: Iterable[GlossaryEntry],
    ) -> tuple[str, list[str]]:
        """Translate the chapter body. Returns ``(body_en, leaked_sentinels)``."""
        sub_map, marker_text = self._apply_glossary_substitution(chapter_zh, glossary, ct)

        paragraphs = opus_mt_models.segment_paragraphs(marker_text)
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

        body = opus_mt_models.reassemble(rebuilt_paragraphs)
        body, leaked = self._restore_glossary_substitution(body, sub_map)
        return body, leaked

    def _translate_title(
        self,
        ct: "opus_mt_models.CTranslator",
        title_zh: str | None,
        glossary: Iterable[GlossaryEntry],
    ) -> str:
        """Translate the title. Locked glossary terms inside the title get
        the same placeholder treatment as the body so a character's name in
        the title comes out consistent."""
        if not title_zh or not title_zh.strip():
            return ""
        sub_map, marker_text = self._apply_glossary_substitution(
            title_zh.strip(), glossary, ct,
        )
        if not marker_text.strip():
            return ""
        # Titles are usually short, treat them as one sentence.
        translated = ct.translate_batch([marker_text])
        out = translated[0] if translated else ""
        out, _ = self._restore_glossary_substitution(out, sub_map)
        return out.strip()

    # -- Placeholder substitution helpers --

    def _apply_glossary_substitution(
        self,
        text: str,
        glossary: Iterable[GlossaryEntry],
        ct: "opus_mt_models.CTranslator",
    ) -> tuple[dict[str, str], str]:
        """Replace each in-text locked-glossary ``term_zh`` with a fresh sentinel.

        Returns ``(sub_map, transformed_text)`` where ``sub_map`` is
        ``{sentinel: term_en}``. The caller passes ``sub_map`` to
        ``_restore_glossary_substitution`` after translation to put the
        canonical English term back in place of each sentinel.

        If the pair's sentinel format selection failed (no format survives
        SentencePiece roundtrip on this tokenizer), returns ``({}, text)``
        and the caller accepts terminology drift — better than silently
        producing splintered placeholder fragments.
        """
        if ct.sentinel_fn is None:
            return {}, text

        # Filter to LOCKED entries with non-empty zh+en, sorted by zh length
        # descending so we substitute compound terms (e.g. "九霄玄宫") before
        # any shorter substring of them ("玄宫"). This matches the same
        # longest-first discipline the format_glossary block uses for the LLM.
        candidates = [
            g for g in glossary
            if getattr(g, "locked", False)
            and (g.term_zh or "").strip()
            and (g.term_en or "").strip()
        ]
        candidates.sort(key=lambda g: (-len(g.term_zh), g.term_zh))

        sub_map: dict[str, str] = {}
        idx = 0
        out = text
        for g in candidates:
            if g.term_zh not in out:
                continue
            idx += 1
            sentinel = ct.sentinel_fn(idx)
            sub_map[sentinel] = g.term_en
            out = out.replace(g.term_zh, sentinel)
        return sub_map, out

    def _restore_glossary_substitution(
        self, text: str, sub_map: dict[str, str],
    ) -> tuple[str, list[str]]:
        """Replace each sentinel in ``text`` with its canonical English term.

        Returns ``(restored_text, leaked_sentinels)`` where ``leaked_sentinels``
        is the list of sentinels that did NOT appear in the output (i.e. the
        NMT model dropped them or SentencePiece split them despite the probe).
        These are logged by the caller; the un-restored sentinel stays visible
        in the output as a deliberate "this term failed" signal."""
        if not sub_map:
            return text, []
        leaked: list[str] = []
        out = text
        for sentinel, term_en in sub_map.items():
            # Case-insensitive match because NMT decoders sometimes Title-Case
            # all-caps tokens. Plain substring (no \b word boundaries) — CJK
            # characters are Unicode "word" chars in Python's re module, so a
            # \b right after the sentinel would fail at the Latin→CJK boundary
            # in mixed output like ``EN(ZX001来了)``. Sentinel uniqueness comes
            # from the mint counter, not from boundary tokens.
            pattern = re.compile(re.escape(sentinel), re.IGNORECASE)
            if not pattern.search(out):
                leaked.append(sentinel)
                continue
            out = pattern.sub(lambda m: term_en, out)
        return out, leaked

    def cache_identity(self) -> str:
        """OPUS-MT does not share the LLM cache layer (translate_chapter is
        overridden and never calls llm_cache). This identity is kept for the
        rare diagnostic that wants to know which backend produced a row."""
        return f"opus_mt:{self.model_id}"
