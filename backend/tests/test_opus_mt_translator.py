"""Unit tests for backend.services.translators.opus_mt.OpusMTTranslator.

We construct the translator with a Provider stub and exercise:
  * constructor validation (rejects missing model_id, unknown pair).
  * source_language mismatch error on translate_chapter.
  * returned TranslationResult.degraded is True unconditionally.
  * paragraph boundaries survive the segment/translate/reassemble roundtrip.
  * registered in the catalog + factory dispatch + boot probe is non-fatal.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.models import TranslationResult
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
    input as ``EN(<input>)`` so we can verify boundaries without needing the
    real CT2 model."""

    pair: str = "zh-en"

    def translate_batch(self, sentences: list[str]) -> list[str]:
        return [f"EN({s})" for s in sentences]


def _install_fake_translator(monkeypatch) -> _FakeCTranslator:
    fake = _FakeCTranslator()

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
