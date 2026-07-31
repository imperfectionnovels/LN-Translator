"""Refiner paragraph-count guard (CAT Phase 1).

Contract under test (refiner.py + queue._refine_chapter_in_db):
- refine_chapter(expected_paragraph_count=N) validates the polished text's
  blank-line paragraph count BEFORE llm_cache.store_refinement, raising
  ParagraphCountMismatch so a violating refinement is never cached.
- The queue retries ONCE with a corrective_note; a second mismatch DISCARDS
  the refinement (refinement_status='error', message "refiner broke paragraph
  alignment", refined_text stays NULL so the reader falls back to the draft).
"""

from __future__ import annotations

import pytest

from backend.db import init_db, open_conn
from backend.services import providers as providers_svc
from backend.services import queue as queue_svc
from backend.services import refiner as refiner_svc
from backend.services.translators.base import ParagraphCountMismatch

pytestmark = pytest.mark.asyncio


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in ("chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    yield
    await _reset_db()


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CACHE_ROOT", str(tmp_path))
    return tmp_path


# A 3-paragraph draft comfortably above _REFINEMENT_MIN_DRAFT_CHARS.
DRAFT = (
    "The first paragraph runs long enough to clear the refinement floor. "
    + "It keeps going with more ordinary prose. " * 6
    + "\n\nThe second paragraph is short.\n\nThe third closes the scene."
)
GOOD_REFINED = (
    "The first paragraph, polished, runs long enough to clear the floor. "
    + "It keeps going with more ordinary prose. " * 6
    + "\n\nThe second paragraph is short and clean.\n\nThe third closes the scene well."
)
BAD_REFINED = "Everything collapsed into a single paragraph by the refiner."


async def _seed_refiner_provider() -> int:
    p = await providers_svc.create_provider(
        name="refiner", provider_type="gemini", model_id="r",
    )
    return p.id


async def _make_refinable_chapter(refiner_id: int) -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, refinement_provider_id) "
            "VALUES ('N', 'paste', ?)",
            (refiner_id,),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status, refinement_status) "
            "VALUES (?, 1, '原文。', ?, 'done', 'pending')",
            (novel_id, DRAFT),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


class _ScriptedBackend:
    """Fake translator backend replaying scripted editor-pass outputs."""

    name = "fake-refiner"
    model_id = "r"
    system_instruction = ""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def cache_identity(self) -> str:
        return f"{self.name}:{self.model_id}"

    async def complete_editor_pass(self, prompt: str, *, system_instruction: str) -> str:
        self.prompts.append(prompt)
        return self._outputs.pop(0)


# ---------------------------------------------------------------------------
# refine_chapter level: raise on violation, never cache a violating output
# ---------------------------------------------------------------------------


async def test_violating_refinement_raises_and_is_not_cached(
    isolated_cache, monkeypatch,
):
    p = await providers_svc.create_provider(
        name="r1", provider_type="gemini", model_id="m",
    )
    backend = _ScriptedBackend([BAD_REFINED, GOOD_REFINED])
    monkeypatch.setattr(
        "backend.services.refiner.get_translator", lambda provider: backend,
    )

    with pytest.raises(ParagraphCountMismatch) as exc:
        await refiner_svc.refine_chapter(
            DRAFT, p, expected_paragraph_count=3,
        )
    assert exc.value.got == 1
    assert exc.value.expected == 3

    # Identical call again: had the violating text been cached, this would
    # return BAD_REFINED without touching the backend. It must instead reach
    # the backend and return the good output.
    out = await refiner_svc.refine_chapter(
        DRAFT, p, expected_paragraph_count=3,
    )
    assert out == GOOD_REFINED
    assert len(backend.prompts) == 2

    # And the GOOD output was cached: a third call is a cache hit.
    out2 = await refiner_svc.refine_chapter(
        DRAFT, p, expected_paragraph_count=3,
    )
    assert out2 == GOOD_REFINED
    assert len(backend.prompts) == 2


async def test_corrective_note_rides_on_the_prompt(isolated_cache, monkeypatch):
    p = await providers_svc.create_provider(
        name="r2", provider_type="gemini", model_id="m",
    )
    backend = _ScriptedBackend([GOOD_REFINED])
    monkeypatch.setattr(
        "backend.services.refiner.get_translator", lambda provider: backend,
    )
    note = "Corrective marker text for the retry."
    out = await refiner_svc.refine_chapter(
        DRAFT, p, expected_paragraph_count=3, corrective_note=note,
    )
    assert out == GOOD_REFINED
    assert note in backend.prompts[0]


# ---------------------------------------------------------------------------
# queue ladder
# ---------------------------------------------------------------------------


async def test_count_preserving_refinement_commits(monkeypatch):
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_refinable_chapter(refiner_id)

    seen: dict = {}

    async def _stub_refine(draft, provider, glossary=None, **kw):
        seen["expected"] = kw.get("expected_paragraph_count")
        seen["corrective"] = kw.get("corrective_note")
        return GOOD_REFINED

    monkeypatch.setattr("backend.services.queue.refine_chapter", _stub_refine)

    async with open_conn() as conn:
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT refinement_status, refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert seen["expected"] == 3          # the draft's paragraph count
    assert seen["corrective"] is None     # first call carries no corrective
    assert row["refinement_status"] == "done"
    assert row["refined_text"] is not None


async def test_single_violation_then_good_retry_commits(monkeypatch):
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_refinable_chapter(refiner_id)

    calls: list[dict] = []

    async def _stub_refine(draft, provider, glossary=None, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise ParagraphCountMismatch(got=1, expected=3)
        return GOOD_REFINED

    monkeypatch.setattr("backend.services.queue.refine_chapter", _stub_refine)

    async with open_conn() as conn:
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT refinement_status, refined_text, refinement_error "
            "FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert len(calls) == 2
    corrective = calls[1].get("corrective_note") or ""
    assert "exactly 3 blank-line-separated paragraphs" in corrective
    assert row["refinement_status"] == "done"
    assert row["refined_text"] is not None
    assert row["refinement_error"] is None


async def test_double_violation_discards_refinement_keeps_draft(monkeypatch):
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_refinable_chapter(refiner_id)

    async def _always_violating(draft, provider, glossary=None, **kw):
        raise ParagraphCountMismatch(got=1, expected=3)

    monkeypatch.setattr(
        "backend.services.queue.refine_chapter", _always_violating,
    )

    async with open_conn() as conn:
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status, translated_text, refinement_status, refined_text, "
            "refinement_error FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "error"
    assert row["refinement_error"] == "refiner broke paragraph alignment"
    assert row["refined_text"] is None
    # The draft stays displayed: chapter is still done with its translation.
    assert row["status"] == "done"
    assert row["translated_text"] == DRAFT


async def test_discarded_refinement_never_cached_end_to_end(
    isolated_cache, monkeypatch,
):
    """Full path: queue ladder driving the REAL refine_chapter against a
    backend that violates twice. The discard must leave no cache entry, so a
    later retry with a well-behaved backend gets fresh good output."""
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_refinable_chapter(refiner_id)

    backend = _ScriptedBackend([BAD_REFINED, BAD_REFINED, GOOD_REFINED])
    monkeypatch.setattr(
        "backend.services.refiner.get_translator", lambda provider: backend,
    )

    async with open_conn() as conn:
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT refinement_status, refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
        assert row["refinement_status"] == "error"
        assert row["refined_text"] is None
        assert len(backend.prompts) == 2  # first call + one corrective retry

        # User retries: flip back to pending and run again. The backend now
        # behaves; nothing stale may come out of the cache.
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'pending', "
            "refinement_error = NULL WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT refinement_status, refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "done"
    assert row["refined_text"] is not None
    assert "polished" in row["refined_text"]


async def test_refiner_template_states_the_count_contract():
    assert (
        "same number of blank-line-separated paragraphs"
        in refiner_svc._REFINER_USER_TEMPLATE
    )
