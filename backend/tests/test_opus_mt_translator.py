"""Unit tests for backend.services.translators.opus_mt.OpusMTTranslator.

We construct the translator with a Provider stub and exercise:
  * constructor validation (rejects missing model_id, unknown pair).
  * source_language mismatch error on translate_chapter.
  * glossary placeholder substitution + restoration on a fake CTranslator
    that returns sentence-by-sentence translations with sentinels preserved.
  * leaked-sentinel detection when the fake translator drops a sentinel.
  * returned TranslationResult.degraded is True unconditionally.
  * registered in the catalog + factory dispatch + boot probe is non-fatal.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

import pytest

from backend.models import GlossaryEntry, TranslationResult
from backend.services import opus_mt_models
from backend.services.providers import Provider
from backend.services.translators.opus_mt import OpusMTTranslator


def _make_provider(model_id: str = "zh-en", pid: int = 1) -> Provider:
    return Provider(
        id=pid,
        name=f"opus_mt-{model_id}",
        provider_type="opus_mt",
        base_url=None,
        model_id=model_id,
        params={},
        secret_ref=None,
        is_default=False,
        last_tested_at=None,
        created_at="",
        updated_at="",
    )


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

def test_constructor_requires_provider():
    with pytest.raises(RuntimeError, match="requires an explicit Provider"):
        OpusMTTranslator(provider=None)


def test_constructor_rejects_missing_model_id():
    bad = _make_provider(model_id="")
    with pytest.raises(RuntimeError, match="missing model_id"):
        OpusMTTranslator(provider=bad)


def test_constructor_rejects_unknown_pair():
    bad = _make_provider(model_id="xx-en")
    with pytest.raises(RuntimeError, match="unsupported opus_mt model_id"):
        OpusMTTranslator(provider=bad)


def test_constructor_accepts_supported_pair():
    t = OpusMTTranslator(provider=_make_provider("zh-en"))
    assert t.model_id == "zh-en"
    assert t.name == "opus_mt"


# ---------------------------------------------------------------------------
# translate_chapter — source_language mismatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_language_mismatch_raises(monkeypatch):
    t = OpusMTTranslator(provider=_make_provider("zh-en"))
    with pytest.raises(RuntimeError, match="source_language is 'ja'"):
        await t.translate_chapter(
            chapter_zh="anything",
            title_zh="t",
            glossary=[],
            source_language="ja",
        )


# ---------------------------------------------------------------------------
# translate_chapter — happy path with a fake CTranslator
# ---------------------------------------------------------------------------

@dataclass
class _FakeCTranslator:
    """Stand-in for opus_mt_models.CTranslator. Translates by tagging each
    input as ``EN(<input>)`` so we can verify boundaries / substitution
    without needing the real CT2 model. Sentinels pass through unchanged."""

    pair: str = "zh-en"
    sentinel_fn: object = None  # set below
    leak_sentinels: tuple[str, ...] = ()

    def translate_batch(self, sentences: list[str]) -> list[str]:
        out = []
        for s in sentences:
            piece = s
            for leak in self.leak_sentinels:
                piece = piece.replace(leak, "")  # drop on purpose
            out.append(f"EN({piece})")
        return out


def _install_fake_translator(
    monkeypatch, *, sentinel_fn=None, leak_sentinels=()
) -> _FakeCTranslator:
    fake = _FakeCTranslator(
        sentinel_fn=sentinel_fn or (lambda i: f"ZX{i:03d}"),
        leak_sentinels=leak_sentinels,
    )

    def _load(pair: str):
        return fake

    monkeypatch.setattr(opus_mt_models, "load_translator", _load)
    return fake


@pytest.mark.asyncio
async def test_translate_chapter_returns_degraded_result(monkeypatch):
    _install_fake_translator(monkeypatch)
    t = OpusMTTranslator(provider=_make_provider("zh-en"))

    result = await t.translate_chapter(
        chapter_zh="第一句。第二句！",
        title_zh="标题",
        glossary=[],
        source_language="zh",
    )
    assert isinstance(result, TranslationResult)
    assert result.degraded is True
    assert result.new_terms == []
    assert "EN(" in result.translated_text
    # Title was non-empty and got translated.
    assert result.title_en.startswith("EN(")


@pytest.mark.asyncio
async def test_translate_chapter_paragraph_boundaries_preserved(monkeypatch):
    _install_fake_translator(monkeypatch)
    t = OpusMTTranslator(provider=_make_provider("zh-en"))

    src = "第一段。\n\n第二段。"
    result = await t.translate_chapter(
        chapter_zh=src,
        title_zh=None,
        glossary=[],
        source_language="zh",
    )
    # Two paragraphs in the output, separated by a blank line.
    assert "\n\n" in result.translated_text


# ---------------------------------------------------------------------------
# Placeholder substitution
# ---------------------------------------------------------------------------

def _g(zh: str, en: str, locked: bool = True) -> GlossaryEntry:
    return GlossaryEntry(
        id=hash(zh) & 0xFFFFFFFF,
        novel_id=1,
        term_zh=zh,
        term_en=en,
        category="character",
        notes=None,
        auto_detected=not locked,
        locked=locked,
    )


@pytest.mark.asyncio
async def test_locked_terms_are_substituted_and_restored(monkeypatch):
    """A locked glossary entry whose Chinese form appears in the source
    is replaced with a sentinel before translation; the sentinel survives
    the fake translator and is restored to the canonical English term."""
    _install_fake_translator(monkeypatch)
    t = OpusMTTranslator(provider=_make_provider("zh-en"))

    glossary = [_g("玄阳真人", "Patriarch Xuanyang", locked=True)]
    src = "玄阳真人来了。玄阳真人很强。"
    result = await t.translate_chapter(
        chapter_zh=src,
        title_zh=None,
        glossary=glossary,
        source_language="zh",
    )
    # All occurrences of the locked term land as "Patriarch Xuanyang" — not
    # as the sentinel and not as a translated-to-English mangle.
    assert result.translated_text.count("Patriarch Xuanyang") == 2
    assert "ZX001" not in result.translated_text


@pytest.mark.asyncio
async def test_unlocked_terms_do_not_substitute(monkeypatch):
    """Unlocked glossary entries don't get the substitution treatment —
    the LLM PEMT pass owns terminology shaping for those."""
    _install_fake_translator(monkeypatch)
    t = OpusMTTranslator(provider=_make_provider("zh-en"))

    glossary = [_g("术法", "spell craft", locked=False)]
    src = "他用术法打人。"
    result = await t.translate_chapter(
        chapter_zh=src,
        title_zh=None,
        glossary=glossary,
        source_language="zh",
    )
    # The fake translator wraps in EN(...) — the source is what we'd see
    # in the wrapper, NOT a substituted sentinel.
    assert "术法" in result.translated_text
    assert "spell craft" not in result.translated_text


@pytest.mark.asyncio
async def test_leaked_sentinels_remain_visible(monkeypatch, caplog):
    """If the fake translator drops a sentinel from its output, the
    substitution layer leaves the sentinel visible in the final body
    (rather than silently restoring it as a hallucinated term) and emits
    a WARNING log line."""
    fake = _install_fake_translator(monkeypatch, leak_sentinels=("ZX001",))
    t = OpusMTTranslator(provider=_make_provider("zh-en"))

    glossary = [_g("玄阳真人", "Patriarch Xuanyang", locked=True)]
    src = "玄阳真人来了。"
    import logging
    with caplog.at_level(logging.WARNING, logger="backend.services.translators.opus_mt"):
        result = await t.translate_chapter(
            chapter_zh=src,
            title_zh=None,
            glossary=glossary,
            source_language="zh",
        )
    assert "Patriarch Xuanyang" not in result.translated_text
    # WARN log line surfaces the leaked sentinel.
    assert any("sentinel" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_substitution_disabled_when_no_sentinel_format(monkeypatch):
    """When the pair's tokenizer has no sentinel format that survives the
    roundtrip probe (CTranslator.sentinel_fn is None), the translator
    skips substitution entirely and accepts terminology drift."""
    _install_fake_translator(monkeypatch, sentinel_fn=None)
    t = OpusMTTranslator(provider=_make_provider("zh-en"))

    # Build a translator with sentinel_fn=None.
    fake = _FakeCTranslator(sentinel_fn=None)
    monkeypatch.setattr(
        opus_mt_models, "load_translator", lambda pair: fake,
    )

    glossary = [_g("玄阳", "Xuanyang", locked=True)]
    result = await t.translate_chapter(
        chapter_zh="玄阳的剑很快。",
        title_zh=None,
        glossary=glossary,
        source_language="zh",
    )
    # Untouched source flows through the fake translator → no sentinel,
    # no substitution. The locked-term Chinese characters reach the EN()
    # wrapper of the fake translator unchanged.
    assert "玄阳" in result.translated_text


# ---------------------------------------------------------------------------
# Catalog + factory + probe integration
# ---------------------------------------------------------------------------

def test_opus_mt_appears_in_catalog():
    from backend.services.translator_catalog import _CATALOG
    entry = next((e for e in _CATALOG if e.type == "opus_mt"), None)
    assert entry is not None
    assert entry.auth == "none"
    assert entry.group == "Local"
    assert entry.secret_ref_hint is None
    assert entry.supports_custom_model is False
    model_ids = {m.id for m in entry.models}
    assert model_ids == {"zh-en", "ja-en", "ko-en"}


def test_factory_dispatch_includes_opus_mt():
    from backend.services.translators.factory import _DISPATCH
    spec = _DISPATCH.get("opus_mt")
    assert spec == ("backend.services.translators.opus_mt", "OpusMTTranslator")


def test_factory_instantiates_opus_mt(monkeypatch):
    """get_translator(provider) returns a working instance for opus_mt."""
    from backend.services.translators.factory import (
        get_translator, invalidate_provider_cache,
    )
    invalidate_provider_cache()
    provider = _make_provider("zh-en", pid=999)
    t = get_translator(provider)
    assert isinstance(t, OpusMTTranslator)
    assert t.model_id == "zh-en"
    invalidate_provider_cache(provider.id)


def test_probe_one_opus_mt_is_non_fatal(monkeypatch):
    """A provider configured for opus_mt with no installed model still lets
    the server boot — the probe logs a warn and returns."""
    import asyncio as _asyncio

    from backend.main import _probe_one, LAST_PROBE_STATE
    provider = _make_provider("zh-en", pid=42)
    # Force is_installed → False so the probe takes the "not installed" path.
    monkeypatch.setattr(opus_mt_models, "is_installed", lambda pair: False)
    LAST_PROBE_STATE["translator"] = "unknown"
    _asyncio.run(_probe_one("translator", provider))
    assert LAST_PROBE_STATE["translator"] == "warn"
