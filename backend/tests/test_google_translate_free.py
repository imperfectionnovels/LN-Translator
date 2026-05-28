"""Unit tests for backend.services.translators.google_translate_free.

We construct the translator with a Provider stub and exercise:
  * constructor validation (rejects missing provider, missing model_id).
  * source_language mapping (zh → zh-CN, ja/ko pass through, None → auto).
  * chunking large chapter into multiple Google Translate calls.
  * returned TranslationResult.degraded is True unconditionally.
  * network / throttle errors raise clean RuntimeError.
  * registered in the catalog + factory dispatch.
"""

from __future__ import annotations

import pytest

from backend.models import TranslationResult
from backend.services.providers import Provider
from backend.services.translators import google_translate_free
from backend.services.translators.google_translate_free import (
    GoogleTranslateFreeTranslator,
    _chunk_for_translate,
    _lang_for_google,
)


def _make_provider(model_id: str = "google-web", pid: int = 1) -> Provider:
    return Provider(
        id=pid,
        name=f"google-translate-{pid}",
        provider_type="google_translate_free",
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
        GoogleTranslateFreeTranslator(provider=None)


def test_constructor_rejects_missing_model_id():
    bad = _make_provider(model_id="")
    with pytest.raises(RuntimeError, match="missing model_id"):
        GoogleTranslateFreeTranslator(provider=bad)


def test_constructor_accepts_default_model_id():
    t = GoogleTranslateFreeTranslator(provider=_make_provider("google-web"))
    assert t.model_id == "google-web"
    assert t.name == "google_translate_free"


# ---------------------------------------------------------------------------
# Source-language mapping
# ---------------------------------------------------------------------------

def test_lang_for_google_zh_mapped_to_simplified():
    assert _lang_for_google("zh") == "zh-CN"


def test_lang_for_google_ja_ko_pass_through():
    assert _lang_for_google("ja") == "ja"
    assert _lang_for_google("ko") == "ko"


def test_lang_for_google_none_means_auto():
    assert _lang_for_google(None) == "auto"
    assert _lang_for_google("") == "auto"


def test_lang_for_google_unknown_passes_through():
    assert _lang_for_google("vi") == "vi"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def test_chunk_empty_input_returns_empty_list():
    assert _chunk_for_translate("") == []


def test_chunk_small_input_single_chunk():
    out = _chunk_for_translate("short paragraph", limit=4500)
    assert out == ["short paragraph"]


def test_chunk_respects_paragraph_boundaries():
    # Two paragraphs, each well under the limit, fit in one chunk.
    text = "Paragraph one.\n\nParagraph two."
    out = _chunk_for_translate(text, limit=4500)
    assert out == [text]


def test_chunk_splits_when_over_limit():
    # Three paragraphs of 2000 chars each = 6000+ chars; with limit=4500 we
    # get two chunks (first holds two paragraphs, second holds one).
    para = "x" * 2000
    text = "\n\n".join([para, para, para])
    out = _chunk_for_translate(text, limit=4500)
    assert len(out) == 2
    # No paragraph is split mid-text.
    for chunk in out:
        for inner in chunk.split("\n\n"):
            assert inner == para


# ---------------------------------------------------------------------------
# translate_chapter — happy path with deep_translator stubbed
# ---------------------------------------------------------------------------

class _FakeGoogleTranslator:
    """Stand-in for deep_translator.GoogleTranslator. Wraps each call as
    ``EN(<input>)`` so tests can verify chunk boundaries and source-lang."""

    instances: list["_FakeGoogleTranslator"] = []

    def __init__(self, source: str, target: str) -> None:
        self.source = source
        self.target = target
        self.calls: list[str] = []
        _FakeGoogleTranslator.instances.append(self)

    def translate(self, text: str) -> str:
        self.calls.append(text)
        return f"EN({text})"


@pytest.fixture(autouse=True)
def _reset_fake_translator_state():
    _FakeGoogleTranslator.instances.clear()
    yield
    _FakeGoogleTranslator.instances.clear()


def _install_fake(monkeypatch):
    """Patch deep_translator.GoogleTranslator inside the translator module."""
    # The translator imports lazily inside _translate_chunk, so we patch
    # the deep_translator module itself.
    import deep_translator
    monkeypatch.setattr(deep_translator, "GoogleTranslator", _FakeGoogleTranslator)


@pytest.mark.asyncio
async def test_translate_chapter_returns_degraded_result(monkeypatch):
    _install_fake(monkeypatch)
    t = GoogleTranslateFreeTranslator(provider=_make_provider())
    result = await t.translate_chapter(
        chapter_zh="第一段。\n\n第二段。",
        title_zh="标题",
        glossary=[],
        source_language="zh",
    )
    assert isinstance(result, TranslationResult)
    assert result.degraded is True
    assert result.new_terms == []
    assert "EN(" in result.translated_text
    assert result.title_en.startswith("EN(")


@pytest.mark.asyncio
async def test_translate_chapter_uses_zh_cn_for_chinese(monkeypatch):
    _install_fake(monkeypatch)
    t = GoogleTranslateFreeTranslator(provider=_make_provider())
    await t.translate_chapter(
        chapter_zh="第一段。", title_zh="标题",
        glossary=[], source_language="zh",
    )
    # All GoogleTranslator instances created during this call were given
    # source='zh-CN'.
    sources = {inst.source for inst in _FakeGoogleTranslator.instances}
    assert sources == {"zh-CN"}


@pytest.mark.asyncio
async def test_translate_chapter_uses_auto_when_no_source_lang(monkeypatch):
    _install_fake(monkeypatch)
    t = GoogleTranslateFreeTranslator(provider=_make_provider())
    await t.translate_chapter(
        chapter_zh="abc", title_zh=None,
        glossary=[], source_language=None,
    )
    sources = {inst.source for inst in _FakeGoogleTranslator.instances}
    assert sources == {"auto"}


@pytest.mark.asyncio
async def test_translate_chapter_chunks_large_body(monkeypatch):
    _install_fake(monkeypatch)
    t = GoogleTranslateFreeTranslator(provider=_make_provider())
    # 3 paragraphs * 2000 chars each = body that splits into >1 chunk.
    para = "x" * 2000
    chapter = "\n\n".join([para, para, para])
    await t.translate_chapter(
        chapter_zh=chapter, title_zh=None,
        glossary=[], source_language="zh",
    )
    # Title was None (zero calls); body produced 2 chunk calls. Net: 2 calls
    # total across instances (or 1 instance with 2 calls, depending on the
    # per-chunk new-instance pattern). The stub creates one instance per
    # translator construction, so all calls land on the same instance pool.
    total_calls = sum(len(inst.calls) for inst in _FakeGoogleTranslator.instances)
    assert total_calls == 2


# ---------------------------------------------------------------------------
# translate_chapter — error mapping
# ---------------------------------------------------------------------------

def _install_raising_fake(monkeypatch, exc_type, message="boom"):
    """Patch deep_translator.GoogleTranslator to raise on .translate()."""
    import deep_translator

    class _Raises:
        def __init__(self, source, target):
            pass

        def translate(self, text):
            raise exc_type(message)

    monkeypatch.setattr(deep_translator, "GoogleTranslator", _Raises)


@pytest.mark.asyncio
async def test_throttle_maps_to_runtime_error(monkeypatch):
    from deep_translator.exceptions import TooManyRequests
    _install_raising_fake(monkeypatch, TooManyRequests)
    t = GoogleTranslateFreeTranslator(provider=_make_provider())
    with pytest.raises(RuntimeError, match="rate-limited"):
        await t.translate_chapter(
            chapter_zh="text", title_zh=None,
            glossary=[], source_language="zh",
        )


@pytest.mark.asyncio
async def test_request_error_maps_to_runtime_error(monkeypatch):
    from deep_translator.exceptions import RequestError
    _install_raising_fake(monkeypatch, RequestError)
    t = GoogleTranslateFreeTranslator(provider=_make_provider())
    with pytest.raises(RuntimeError, match="request failed"):
        await t.translate_chapter(
            chapter_zh="text", title_zh=None,
            glossary=[], source_language="zh",
        )


@pytest.mark.asyncio
async def test_unexpected_error_wrapped(monkeypatch):
    _install_raising_fake(monkeypatch, ValueError, "weird")
    t = GoogleTranslateFreeTranslator(provider=_make_provider())
    with pytest.raises(RuntimeError, match="unexpected error"):
        await t.translate_chapter(
            chapter_zh="text", title_zh=None,
            glossary=[], source_language="zh",
        )


# ---------------------------------------------------------------------------
# Catalog + factory integration
# ---------------------------------------------------------------------------

def test_google_translate_free_in_catalog():
    from backend.services.translator_catalog import _CATALOG
    entry = next((e for e in _CATALOG if e.type == "google_translate_free"), None)
    assert entry is not None
    assert entry.auth == "none"
    assert entry.group == "Local"
    assert entry.supports_custom_model is False
    model_ids = {m.id for m in entry.models}
    assert "google-web" in model_ids


def test_factory_dispatch_includes_google_translate_free():
    from backend.services.translators.factory import _DISPATCH
    spec = _DISPATCH.get("google_translate_free")
    assert spec == (
        "backend.services.translators.google_translate_free",
        "GoogleTranslateFreeTranslator",
    )


def test_factory_instantiates_google_translate_free():
    from backend.services.translators.factory import (
        get_translator, invalidate_provider_cache,
    )
    invalidate_provider_cache()
    provider = _make_provider("google-web", pid=999)
    t = get_translator(provider)
    assert isinstance(t, GoogleTranslateFreeTranslator)
    invalidate_provider_cache(provider.id)


# ---------------------------------------------------------------------------
# Guard: opus_mt should NOT be in the catalog or dispatch
# ---------------------------------------------------------------------------

def test_opus_mt_removed_from_catalog():
    from backend.services.translator_catalog import _CATALOG
    assert all(e.type != "opus_mt" for e in _CATALOG)


def test_opus_mt_removed_from_factory():
    from backend.services.translators.factory import _DISPATCH
    assert "opus_mt" not in _DISPATCH
