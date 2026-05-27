"""Translator backends. Routes import `translate_chapter` from here — the
factory decides which backend handles the call.

Callers pass an explicit `provider` to route per-novel. If `provider` is
None, the call falls back to the legacy global backend (the process-wide
TRANSLATOR_BACKEND env var) — used by the startup probe and tests that
skip the provider table.
"""

from __future__ import annotations

from backend.models import GlossaryEntry, TranslationResult
from backend.services.providers import Provider

from .base import TransientTranslatorError
from .factory import get_translator, translator_factory

__all__ = [
    "translate_chapter",
    "TransientTranslatorError",
    "translator_factory",
    "get_translator",
]


async def translate_chapter(
    chapter_zh: str,
    title_zh: str | None,
    glossary: list[GlossaryEntry],
    previous_context: str | None = None,
    style_edits: list[tuple[str, str]] | None = None,
    use_cache: bool = True,
    style_note: str | None = None,
    provider: Provider | None = None,
    genre: str | None = None,
    custom_brief: str | None = None,
    free_draft: str | None = None,
    source_language: str | None = None,
) -> TranslationResult:
    backend = get_translator(provider) if provider is not None else translator_factory()
    return await backend.translate_chapter(
        chapter_zh, title_zh, glossary, previous_context, style_edits,
        use_cache=use_cache,
        style_note=style_note,
        genre=genre,
        custom_brief=custom_brief,
        free_draft=free_draft,
        source_language=source_language,
    )
