"""Tests for the 1:1 paragraph-count validation ladder (CAT Phase 1).

Contract under test (translators/base.py + queue.py):
- translate_chapter(expected_paragraph_count=N) validates the parsed body's
  blank-line paragraph count BEFORE the llm_cache store, so the cache never
  holds a violating envelope.
- First mismatch: one corrective retry (original prompt plus an appended
  corrective naming got/expected counts).
- Second mismatch: the body is accepted anyway (no plain-text fallback, no
  third call) and the result carries paragraph_count_status
  'count_mismatch_accepted'; a recovered retry carries 'count_mismatch_retry'.
- expected_paragraph_count=None skips validation entirely (back-compat).
- The queue feeds the model the pre-joined effective source paragraphs and
  records the mismatch outcome on the chapter_translation_attempts row.
"""

from __future__ import annotations

import pytest

from backend.models import TranslationResult
from backend.services.translators.base import (
    BaseTranslator,
    ParagraphCountMismatch,
)

pytestmark = pytest.mark.asyncio


def _envelope(body: str, title: str = "The Gate") -> str:
    return f"TITLE_EN: {title}\n=====BODY=====\n{body}\n=====TERMS=====\n[]"


class _Scripted(BaseTranslator):
    """Deterministic BaseTranslator: replays scripted _complete responses."""

    name = "scripted-1to1"
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
        return "PLAIN FALLBACK"


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Point the LLM cache at a per-test temp dir so cache assertions are
    hermetic (conftest's shared cache dir would leak entries between tests)."""
    monkeypatch.setenv("LLM_CACHE_ROOT", str(tmp_path))
    return tmp_path


SRC = "甲一段。\n\n乙二段。"


# ---------------------------------------------------------------------------
# BaseTranslator ladder
# ---------------------------------------------------------------------------


async def test_correct_count_passes_and_caches(isolated_cache):
    t = _Scripted([_envelope("P one.\n\nP two.")])
    result = await t.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert result.translated_text == "P one.\n\nP two."
    assert result.paragraph_count_status is None
    assert len(t.prompts) == 1

    # Same inputs on a fresh instance: served from cache, no LLM call.
    t2 = _Scripted([])
    cached = await t2.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert cached.translated_text == "P one.\n\nP two."
    assert t2.prompts == []


async def test_first_mismatch_retries_with_corrective_and_recovers(isolated_cache):
    t = _Scripted([
        _envelope("All in one paragraph."),
        _envelope("P one.\n\nP two."),
    ])
    result = await t.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert len(t.prompts) == 2
    # The corrective ride-along: base prompt plus the appended instruction
    # naming both counts.
    assert t.prompts[1].startswith(t.prompts[0])
    corrective_tail = t.prompts[1][len(t.prompts[0]):]
    assert "exactly 2 blank-line-separated paragraphs" in corrective_tail
    assert "1" in corrective_tail  # the got-count is cited
    assert result.paragraph_count_status == "count_mismatch_retry"
    assert result.translated_text == "P one.\n\nP two."
    assert result.degraded is False
    assert t.plain_calls == 0


async def test_double_mismatch_accepts_body_without_caching(isolated_cache):
    t = _Scripted([
        _envelope("One paragraph only."),
        _envelope("A.\n\nB.\n\nC."),
    ])
    result = await t.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert result.paragraph_count_status == "count_mismatch_accepted"
    # The second (still violating) body is committed as-is: never a third
    # call, never the plain-text fallback.
    assert result.translated_text == "A.\n\nB.\n\nC."
    assert result.degraded is False
    assert t.plain_calls == 0
    assert len(t.prompts) == 2

    # Validation preceded the cache store: the violating envelope must NOT be
    # served on a fresh identical call, which instead reaches the LLM again.
    t2 = _Scripted([_envelope("P one.\n\nP two.")])
    fresh = await t2.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert len(t2.prompts) == 1
    assert fresh.translated_text == "P one.\n\nP two."


async def test_recovered_result_cached_without_per_call_status(isolated_cache):
    t = _Scripted([_envelope("one para."), _envelope("P1.\n\nP2.")])
    first = await t.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert first.paragraph_count_status == "count_mismatch_retry"

    # A cache hit replays the clean result, not the per-call retry status.
    t2 = _Scripted([])
    hit = await t2.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert t2.prompts == []
    assert hit.paragraph_count_status is None
    assert hit.translated_text == "P1.\n\nP2."


async def test_none_expected_count_skips_validation(isolated_cache):
    t = _Scripted([_envelope("A.\n\nB.\n\nC.")])
    result = await t.translate_chapter(SRC, "题", [])
    assert result.paragraph_count_status is None
    assert result.translated_text == "A.\n\nB.\n\nC."
    assert len(t.prompts) == 1


async def test_parse_failure_retry_coexists_with_count_validation(isolated_cache):
    t = _Scripted(["garbage with no delimiter", _envelope("P1.\n\nP2.")])
    result = await t.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert len(t.prompts) == 2
    assert t.prompts[0] == t.prompts[1]  # parse retry keeps the same prompt
    assert result.paragraph_count_status is None
    assert result.degraded is False


async def test_compound_mismatch_and_parse_failures_reach_fallback(isolated_cache):
    """Worst-case interleaving on the base loop: mismatch, then two parse
    failures. Lands on the plain-text fallback with exactly one budget tick
    per real LLM call (3 structured + 1 plain = MAX_LLM_CALLS_PER_CHAPTER),
    not a TransientTranslatorError from double-counting."""
    t = _Scripted([
        _envelope("All in one paragraph."),  # parses, count mismatch
        "garbage with no delimiter",          # corrective retry: parse fail
        "garbage again",                      # parse retry: parse fail
    ])
    result = await t.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert result.degraded is True
    assert result.translated_text == "PLAIN FALLBACK"
    assert len(t.prompts) == 3
    assert t.plain_calls == 1
    assert t._llm_call_count == 4


async def test_paragraph_count_mismatch_carries_counts():
    e = ParagraphCountMismatch(got=3, expected=2)
    assert e.got == 3
    assert e.expected == 2
    assert isinstance(e, ValueError)


# ---------------------------------------------------------------------------
# DeepSeek ladder (independent translate_chapter flow)
# ---------------------------------------------------------------------------


def _scripted_deepseek(monkeypatch, responses: list[str]):
    """A DeepSeekTranslator built without __init__ (no client, no API key),
    with _call_deepseek stubbed to replay scripted responses. The budget tick
    lives in the orchestration loops, not in _call_deepseek, so the stub does
    not affect counting."""
    from backend.services.translators.deepseek import DeepSeekTranslator

    ds = object.__new__(DeepSeekTranslator)
    remaining = list(responses)
    calls: list[tuple[str, str]] = []

    async def _fake_call(prompt, *, label="translate", **kw):
        calls.append((label, prompt))
        return remaining.pop(0)

    monkeypatch.setattr(ds, "_call_deepseek", _fake_call)
    return ds, calls


async def test_deepseek_mismatch_corrective_retry_recovers_and_caches_clean(
    isolated_cache, monkeypatch,
):
    ds, calls = _scripted_deepseek(monkeypatch, [
        _envelope("All in one paragraph."),
        _envelope("P one.\n\nP two."),
    ])
    result = await ds.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert result.paragraph_count_status == "count_mismatch_retry"
    assert result.translated_text == "P one.\n\nP two."
    assert [label for label, _ in calls] == ["translate", "translate"]
    assert "exactly 2 blank-line-separated paragraphs" in calls[1][1]

    # The recovered result was cached with the per-call status stripped: a
    # fresh instance serves it from cache without any LLM call.
    ds2, calls2 = _scripted_deepseek(monkeypatch, [])
    hit = await ds2.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert calls2 == []
    assert hit.translated_text == "P one.\n\nP two."
    assert hit.paragraph_count_status is None


async def test_deepseek_mismatch_plus_double_parse_fail_reaches_fallback(
    isolated_cache, monkeypatch,
):
    """The compound path: count mismatch, then the corrective retry and the
    parse retry both return garbage. Must land on the plain-text fallback
    (4 budget ticks exactly, no TransientTranslatorError) and cache nothing."""
    ds, calls = _scripted_deepseek(monkeypatch, [
        _envelope("All in one paragraph."),  # parses, count mismatch
        "garbage with no delimiter",          # corrective retry: parse fail
        "garbage again",                      # parse retry: parse fail
        "PLAIN FALLBACK BODY",                # plain-text fallback
    ])
    result = await ds.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert [label for label, _ in calls] == [
        "translate", "translate", "translate", "fallback",
    ]
    assert result.degraded is True
    assert result.translated_text == "PLAIN FALLBACK BODY"
    # Exactly one tick per real LLM call: 3 structured + 1 plain.
    assert ds._llm_call_count == 4

    # Nothing was cached (fallbacks are never cached): the same inputs reach
    # the LLM again on a fresh instance.
    ds2, calls2 = _scripted_deepseek(monkeypatch, [_envelope("P1.\n\nP2.")])
    fresh = await ds2.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert len(calls2) == 1
    assert fresh.translated_text == "P1.\n\nP2."


async def test_deepseek_double_mismatch_accepts_uncached(
    isolated_cache, monkeypatch,
):
    ds, calls = _scripted_deepseek(monkeypatch, [
        _envelope("One paragraph only."),
        _envelope("A.\n\nB.\n\nC."),
    ])
    result = await ds.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert result.paragraph_count_status == "count_mismatch_accepted"
    assert result.translated_text == "A.\n\nB.\n\nC."
    assert result.degraded is False
    assert [label for label, _ in calls] == ["translate", "translate"]

    # The violating body must not be served from cache afterwards.
    ds2, calls2 = _scripted_deepseek(monkeypatch, [_envelope("P1.\n\nP2.")])
    fresh = await ds2.translate_chapter(SRC, "题", [], expected_paragraph_count=2)
    assert len(calls2) == 1
    assert fresh.translated_text == "P1.\n\nP2."


# ---------------------------------------------------------------------------
# Queue integration
# ---------------------------------------------------------------------------


async def _fresh_db():
    from backend.db import init_db, open_conn

    await init_db()
    async with open_conn() as conn:
        for t in ("chapter_translation_attempts", "chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await conn.commit()


async def _seed_novel_chapter(source: str) -> tuple[int, int]:
    from backend.db import open_conn
    from backend.services import providers as providers_svc

    await providers_svc.create_provider(
        name="p", provider_type="gemini", model_id="m", is_default=True,
    )
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, "
            "original_text, status, translate_queued) "
            "VALUES (?, 1, '第1章 开端', ?, 'pending', 1)",
            (novel_id, source),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


QUEUE_SOURCE = "第1章 开端\n\n他说，\n\n这不可能。\n\n第二段。"


async def test_queue_feeds_prejoined_source_and_expected_count(monkeypatch):
    from backend.db import open_conn
    from backend.services import queue as queue_svc

    await _fresh_db()
    novel_id, chapter_id = await _seed_novel_chapter(QUEUE_SOURCE)

    seen: dict = {}

    async def _fake_translate(chapter_zh, title_zh, glossary, **kw):
        seen["chapter_zh"] = chapter_zh
        seen["expected"] = kw.get("expected_paragraph_count")
        return TranslationResult(title_en="T", translated_text="A.\n\nB.", new_terms=[])

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status, original_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()

    # Heading dropped, mid-sentence break pre-joined, count passed through.
    assert seen["chapter_zh"] == "他说，这不可能。\n\n第二段。"
    assert seen["expected"] == 2
    # Stored source stays verbatim.
    assert row["original_text"] == QUEUE_SOURCE
    assert row["status"] == "done"


async def _run_queue_with_status(monkeypatch, status: str) -> tuple[str, str]:
    """Translate one chapter with a stub returning the given
    paragraph_count_status; return (chapter_status, attempt_status)."""
    from backend.db import open_conn
    from backend.services import queue as queue_svc

    await _fresh_db()
    novel_id, chapter_id = await _seed_novel_chapter(QUEUE_SOURCE)

    async def _fake_translate(chapter_zh, title_zh, glossary, **kw):
        return TranslationResult(
            title_en="T", translated_text="A.\n\nB.", new_terms=[],
            paragraph_count_status=status,
        )

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status FROM chapters WHERE id = ?", (chapter_id,),
        )
        ch = await cur.fetchone()
        cur = await conn.execute(
            "SELECT status FROM chapter_translation_attempts WHERE chapter_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (chapter_id,),
        )
        attempt = await cur.fetchone()
    return ch["status"], attempt["status"]


async def test_queue_records_count_mismatch_retry_attempt(monkeypatch):
    ch_status, attempt_status = await _run_queue_with_status(
        monkeypatch, "count_mismatch_retry",
    )
    assert ch_status == "done"
    assert attempt_status == "count_mismatch_retry"


async def test_queue_records_count_mismatch_accepted_attempt(monkeypatch):
    # A double miss still commits the chapter; only the attempt row differs.
    ch_status, attempt_status = await _run_queue_with_status(
        monkeypatch, "count_mismatch_accepted",
    )
    assert ch_status == "done"
    assert attempt_status == "count_mismatch_accepted"
