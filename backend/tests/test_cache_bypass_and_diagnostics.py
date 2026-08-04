"""Bug hunt 2026-08-04: F9 (degenerate-branch cache bypass) and F7
(DeepSeek prompt_snapshot + parse_error population).

F9: translate_chapter(cacheable=False) bypasses the LLM cache in BOTH
directions. The queue passes it on the degenerate heading-only branch,
whose raw-text prompt runs with count validation OFF: a stored unvalidated
envelope must never be loadable under a colliding key from the validated
1:1 path (a cache LOAD skips check_paragraph_count), and vice versa.

F7: DeepSeek's override stamps prompt_snapshot on every fresh exit (the
attempts dialog otherwise fell back to showing an OLDER attempt's prompt),
keeps cache entries lean (a cache hit replays no snapshot), and both base
and DeepSeek populate parse_error on the plain-text-fallback path.
"""

from __future__ import annotations

import pytest

from backend.db import init_db, open_conn
from backend.models import TranslationResult
from backend.services import providers as providers_svc
from backend.services import queue as queue_svc
from backend.services.translators import deepseek as ds_module
from backend.services.translators.base import BaseTranslator

pytestmark = pytest.mark.asyncio


def _envelope(body: str, title: str = "The Gate") -> str:
    return f"TITLE_EN: {title}\n=====BODY=====\n{body}\n=====TERMS=====\n[]"


class _Scripted(BaseTranslator):
    name = "scripted-bypass"
    model_id = "test-model"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.plain_calls = 0

    async def _complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._responses.pop(0)

    async def _complete_plain(self, prompt: str) -> str:
        self.plain_calls += 1
        return "TITLE_EN: F\n=====BODY=====\nPlain fallback body."


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CACHE_ROOT", str(tmp_path))
    return tmp_path


SRC = "甲一段。\n\n乙二段。"


# ---------------------------------------------------------------------------
# F9: cacheable=False bypasses load AND store
# ---------------------------------------------------------------------------


async def test_cacheable_false_skips_store_and_load(isolated_cache):
    t = _Scripted([_envelope("P one.\n\nP two.")])
    r1 = await t.translate_chapter(SRC, "题", [], cacheable=False)
    assert r1.translated_text == "P one.\n\nP two."
    assert len(t.prompts) == 1

    # No store happened: an identical cacheable call must reach the LLM.
    t2 = _Scripted([_envelope("Fresh body.\n\nSecond para.")])
    r2 = await t2.translate_chapter(SRC, "题", [])
    assert len(t2.prompts) == 1
    assert r2.translated_text == "Fresh body.\n\nSecond para."

    # And no load happens under cacheable=False even when an entry exists
    # (t2's call above stored one under the same key).
    t3 = _Scripted([_envelope("Third body.\n\nSecond para.")])
    r3 = await t3.translate_chapter(SRC, "题", [], cacheable=False)
    assert len(t3.prompts) == 1
    assert r3.translated_text == "Third body.\n\nSecond para."


async def test_use_cache_false_still_stores(isolated_cache):
    """Contrast pin: use_cache=False (force retranslate) skips only the
    read; the fresh result is stored and serves the next call."""
    t = _Scripted([_envelope("Stored body.\n\nSecond para.")])
    await t.translate_chapter(SRC, "题", [], use_cache=False)
    t2 = _Scripted([])
    hit = await t2.translate_chapter(SRC, "题", [])
    assert hit.translated_text == "Stored body.\n\nSecond para."
    assert t2.prompts == []


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in ("chapter_segments", "chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await conn.commit()


async def test_worker_passes_cacheable_false_only_on_degenerate_branch(
    monkeypatch,
):
    """The queue arms cacheable=bool(source_paragraphs): a heading-only
    chapter (empty effective-paragraph split) bypasses the cache; a normal
    chapter keeps it."""
    await init_db()
    await _reset_db()
    await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m", is_default=True,
    )
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        # Heading-only body: chapter_source_paragraphs drops the heading
        # line, leaving no effective source paragraphs.
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, "
            "original_text, status, translate_queued) "
            "VALUES (?, 1, '第一章', '第一章 起始', 'pending', 1)",
            (novel_id,),
        )
        degenerate_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, "
            "original_text, status, translate_queued) "
            "VALUES (?, 2, '第二章', ?, 'pending', 1)",
            (novel_id, SRC),
        )
        armed_id = cur.lastrowid
        await conn.commit()

    calls: list[dict] = []

    async def _fake(chapter_zh, title_zh, glossary, **kwargs):
        calls.append({"chapter_zh": chapter_zh, **kwargs})
        n = max(len(chapter_zh.split("\n\n")), 1)
        text = "\n\n".join(f"Paragraph {i + 1}." for i in range(n))
        return TranslationResult(title_en="T", translated_text=text, new_terms=[])

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake)
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, degenerate_id)
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, armed_id)

    assert len(calls) == 2
    assert calls[0]["cacheable"] is False
    assert calls[0]["expected_paragraph_count"] is None
    assert calls[1]["cacheable"] is True
    assert calls[1]["expected_paragraph_count"] == 2
    await _reset_db()


# ---------------------------------------------------------------------------
# F7: parse_error population (base) + DeepSeek prompt_snapshot
# ---------------------------------------------------------------------------


async def test_base_fallback_populates_parse_error(isolated_cache):
    t = _Scripted(["no delimiter at all", "still no delimiter"])
    result = await t.translate_chapter(SRC, "题", [])
    assert result.degraded is True
    assert t.plain_calls == 1
    assert result.parse_error is not None
    assert "delimiter" in result.parse_error
    assert result.prompt_snapshot == t.prompts[-1]


def _make_deepseek(monkeypatch, responses: list[str]) -> ds_module.DeepSeekTranslator:
    monkeypatch.setattr(ds_module, "DEEPSEEK_API_KEY", "test-key")
    t = ds_module.DeepSeekTranslator()
    t.calls = []
    t._responses = list(responses)

    async def _fake_call(prompt: str, **kwargs) -> str:
        t.calls.append(prompt)
        return t._responses.pop(0)

    monkeypatch.setattr(t, "_call_deepseek", _fake_call)
    return t


async def test_deepseek_stamps_prompt_snapshot_on_success(
    isolated_cache, monkeypatch,
):
    t = _make_deepseek(monkeypatch, [_envelope("P one.\n\nP two.")])
    result = await t.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert result.prompt_snapshot == t.calls[0]
    assert result.parse_error is None

    # Cache entries stay lean: a hit replays no per-call snapshot.
    t2 = _make_deepseek(monkeypatch, [])
    hit = await t2.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert t2.calls == []
    assert hit.prompt_snapshot is None


async def test_deepseek_corrective_retry_snapshot_carries_final_prompt(
    isolated_cache, monkeypatch,
):
    t = _make_deepseek(monkeypatch, [
        _envelope("All one paragraph."),
        _envelope("P one.\n\nP two."),
    ])
    result = await t.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert result.paragraph_count_status == "count_mismatch_retry"
    # The snapshot is the LAST attempt's prompt: base prompt + corrective.
    assert result.prompt_snapshot == t.calls[-1]
    assert t.calls[-1].startswith(t.calls[0])
    assert len(t.calls[-1]) > len(t.calls[0])


async def test_deepseek_fallback_stamps_snapshot_and_parse_error(
    isolated_cache, monkeypatch,
):
    t = _make_deepseek(monkeypatch, [
        "garbage without a delimiter",
        "second garbage",
        "TITLE_EN: F\n=====BODY=====\nFallback body.",
    ])
    result = await t.translate_chapter(SRC, "题", [])
    assert result.degraded is True
    assert result.parse_error is not None
    assert "delimiter" in result.parse_error
    # The structured prompt that kept failing is the snapshot (the fallback
    # prompt is a diagnostic dead end; base.py stamps the same way).
    assert result.prompt_snapshot == t.calls[0]

    # Degraded results are never cached: a fresh identical call re-attempts.
    t2 = _make_deepseek(monkeypatch, [_envelope("Clean.\n\nBody.")])
    fresh = await t2.translate_chapter(SRC, "题", [])
    assert len(t2.calls) == 1
    assert fresh.degraded is False


async def test_deepseek_cacheable_false_skips_store_and_load(
    isolated_cache, monkeypatch,
):
    t = _make_deepseek(monkeypatch, [_envelope("Bypass body.\n\nTwo.")])
    r = await t.translate_chapter(SRC, "题", [], cacheable=False)
    assert r.translated_text == "Bypass body.\n\nTwo."

    # Nothing stored: a cacheable call reaches the API.
    t2 = _make_deepseek(monkeypatch, [_envelope("Fresh.\n\nTwo.")])
    r2 = await t2.translate_chapter(SRC, "题", [])
    assert len(t2.calls) == 1
    # Now an entry exists; cacheable=False still refuses to load it.
    t3 = _make_deepseek(monkeypatch, [_envelope("Third.\n\nTwo.")])
    r3 = await t3.translate_chapter(SRC, "题", [], cacheable=False)
    assert len(t3.calls) == 1
    assert r3.translated_text == "Third.\n\nTwo."
    assert r2.translated_text == "Fresh.\n\nTwo."
