"""Unit tests for backend.services.opus_mt_models.

The module is lazy-import-only — ctranslate2 / sentencepiece are not imported
until ``load_translator`` is called. These tests exercise the bits that don't
need the wheels installed: registry, path resolution, sentence segmentation,
the download lock, and the atomic-extraction control flow against a synthetic
tarball.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import httpx
import pytest

from backend.services import opus_mt_models


# ---------------------------------------------------------------------------
# Registry + paths
# ---------------------------------------------------------------------------

def test_registry_exposes_three_pairs():
    pairs = set(opus_mt_models.SUPPORTED_PAIRS.keys())
    assert pairs == {"zh-en", "ja-en", "ko-en"}


def test_pair_for_language_routes_iso_codes():
    assert opus_mt_models.pair_for_language("zh") == "zh-en"
    assert opus_mt_models.pair_for_language("ja") == "ja-en"
    assert opus_mt_models.pair_for_language("ko") == "ko-en"
    assert opus_mt_models.pair_for_language("en") is None
    assert opus_mt_models.pair_for_language("zt") is None


def test_model_dir_rejects_unknown_pair():
    with pytest.raises(KeyError):
        opus_mt_models.model_dir("xx-en")


def test_is_installed_false_for_missing_pair_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(opus_mt_models, "USER_DATA_ROOT", tmp_path, raising=True)
    # model_dir computed from the patched root.
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    assert opus_mt_models.is_installed("zh-en") is False


def test_is_installed_true_when_required_files_present(tmp_path, monkeypatch):
    pair_dir = tmp_path / "opus_mt" / "zh-en"
    pair_dir.mkdir(parents=True)
    for fname in ("model.bin", "source.spm", "target.spm"):
        (pair_dir / fname).write_bytes(b"stub")
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    assert opus_mt_models.is_installed("zh-en") is True

    # Missing one required file → not installed.
    (pair_dir / "model.bin").unlink()
    assert opus_mt_models.is_installed("zh-en") is False


# ---------------------------------------------------------------------------
# Sentence segmentation
# ---------------------------------------------------------------------------

def test_split_sentences_handles_cjk_punctuation():
    txt = "你好。这是测试！还有问题吗？"
    sents = opus_mt_models.split_sentences(txt)
    assert sents == ["你好。", "这是测试！", "还有问题吗？"]


def test_split_sentences_respects_quote_pairs():
    # A 「」 quote span containing sentence terminators must NOT split inside.
    # Only the trailing 。 outside the quote terminates the sentence — so the
    # entire string lands as one sentence even though it has three 。 marks.
    txt = "他说「你好。我来了。」然后走了。"
    sents = opus_mt_models.split_sentences(txt)
    assert sents == ["他说「你好。我来了。」然后走了。"]

    # Two outside-quote terminators → two sentences, even with interior 。 marks.
    txt2 = "他说「你好。」我笑了。然后离开。"
    sents2 = opus_mt_models.split_sentences(txt2)
    assert sents2 == ["他说「你好。」我笑了。", "然后离开。"]


def test_split_sentences_trailing_text_without_terminator():
    """A paragraph ending without ! ? . still produces a sentence."""
    sents = opus_mt_models.split_sentences("这是一段没有结束符的话")
    assert sents == ["这是一段没有结束符的话"]


def test_segment_paragraphs_preserves_blank_lines():
    chapter = "第一段。\n\n第二段，有两句。这是第二句！\n\n第三段。"
    paras = opus_mt_models.segment_paragraphs(chapter)
    assert len(paras) == 3
    assert paras[0] == ["第一段。"]
    assert paras[1] == ["第二段，有两句。", "这是第二句！"]
    assert paras[2] == ["第三段。"]


def test_reassemble_roundtrips_through_segmenter():
    """A paragraph-segmented chapter rebuilds in a stable form."""
    chapter = "Hello.\n\nWorld today. Tomorrow we leave."
    paras = opus_mt_models.segment_paragraphs(chapter)
    out = opus_mt_models.reassemble(paras)
    # Whitespace inside paragraphs collapses to single spaces — that's by
    # design (NMT outputs lose internal whitespace anyway). Paragraph
    # boundary survives.
    assert out == "Hello.\n\nWorld today. Tomorrow we leave."


# ---------------------------------------------------------------------------
# Download flow — synthetic tar served from an in-process httpx mock transport.
# ---------------------------------------------------------------------------

def _build_test_bundle() -> bytes:
    """Pack a tar.gz with the three required files under a top-level dir."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for fname in ("model.bin", "source.spm", "target.spm"):
            data = f"contents of {fname}".encode()
            info = tarfile.TarInfo(name=f"opus-mt-test/{fname}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _mock_transport(payload: bytes) -> httpx.MockTransport:
    """One-shot mock that returns ``payload`` with a correct Content-Length."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={
            "Content-Length": str(len(payload)),
        })
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_download_pair_atomic_extract(tmp_path, monkeypatch):
    """Download a synthetic bundle, verify the pair dir lands with all files,
    no half-state staging dir remains, and the events stream surfaces a 'done'."""
    monkeypatch.setattr(opus_mt_models, "USER_DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    # Reset module-level dicts so cross-test state doesn't bleed in.
    opus_mt_models._download_locks.clear()

    bundle = _build_test_bundle()
    transport = _mock_transport(bundle)

    # Build a spec with a non-empty URL and empty SHA (skip verification).
    spec = opus_mt_models.ModelSpec(
        pair="zh-en", source_lang="zh", target_lang="en",
        url="https://example.invalid/opus-mt-zh-en.tar.gz",
        sha256="", size_mb=1, display="zh→en (test)",
    )
    monkeypatch.setitem(opus_mt_models.SUPPORTED_PAIRS, "zh-en", spec)

    async with httpx.AsyncClient(transport=transport) as client:
        events = []
        async for ev in opus_mt_models.download_pair("zh-en", http_client=client):
            events.append(ev)

    assert events[-1].phase == "done"
    pair_dir = tmp_path / "opus_mt" / "zh-en"
    assert pair_dir.is_dir()
    for fname in ("model.bin", "source.spm", "target.spm"):
        assert (pair_dir / fname).is_file()
    # No leftover staging dir.
    staging = list((tmp_path / "opus_mt").glob(".zh-en-staging-*"))
    assert staging == []


@pytest.mark.asyncio
async def test_download_pair_idempotent_when_installed(tmp_path, monkeypatch):
    """If a pair is already installed, download_pair short-circuits with 'done'."""
    monkeypatch.setattr(opus_mt_models, "USER_DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    pair_dir = tmp_path / "opus_mt" / "zh-en"
    pair_dir.mkdir(parents=True)
    for fname in ("model.bin", "source.spm", "target.spm"):
        (pair_dir / fname).write_bytes(b"stub")
    opus_mt_models._download_locks.clear()

    # spec needs SOME URL so download_pair doesn't bail on the "no URL" path.
    spec = opus_mt_models.ModelSpec(
        pair="zh-en", source_lang="zh", target_lang="en",
        url="https://example.invalid/opus-mt-zh-en.tar.gz",
        sha256="", size_mb=1, display="zh→en (test)",
    )
    monkeypatch.setitem(opus_mt_models.SUPPORTED_PAIRS, "zh-en", spec)

    events = []
    async for ev in opus_mt_models.download_pair("zh-en"):
        events.append(ev)
    assert len(events) == 1
    assert events[0].phase == "done"


@pytest.mark.asyncio
async def test_download_pair_refuses_unsafe_tar_member(tmp_path, monkeypatch):
    """A tar member whose name escapes the destination must abort extraction."""
    monkeypatch.setattr(opus_mt_models, "USER_DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    opus_mt_models._download_locks.clear()

    # Build a malicious tar with a ../ traversal entry.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="../escape.bin")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"evil"))
    transport = _mock_transport(buf.getvalue())

    spec = opus_mt_models.ModelSpec(
        pair="zh-en", source_lang="zh", target_lang="en",
        url="https://example.invalid/opus-mt-zh-en.tar.gz",
        sha256="", size_mb=1, display="zh→en (test)",
    )
    monkeypatch.setitem(opus_mt_models.SUPPORTED_PAIRS, "zh-en", spec)

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="escapes destination"):
            async for _ in opus_mt_models.download_pair("zh-en", http_client=client):
                pass

    # Pair dir was never created.
    assert not (tmp_path / "opus_mt" / "zh-en").exists()


def test_remove_pair_returns_false_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(opus_mt_models, "USER_DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    assert opus_mt_models.remove_pair("zh-en") is False


def test_remove_pair_deletes_dir_and_evicts_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(opus_mt_models, "USER_DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    pair_dir = tmp_path / "opus_mt" / "zh-en"
    pair_dir.mkdir(parents=True)
    (pair_dir / "model.bin").write_bytes(b"x")

    # Seed the translator cache so we can confirm eviction.
    opus_mt_models._translator_cache["zh-en"] = "sentinel"

    assert opus_mt_models.remove_pair("zh-en") is True
    assert not pair_dir.exists()
    assert "zh-en" not in opus_mt_models._translator_cache
