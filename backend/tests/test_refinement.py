"""Phase 4 tests: refinement state machine.

Covers:
- Translator + refinement in one _run_translate call (chains under the lock).
- Novel WITHOUT refinement_provider_id: refinement_status stays 'none' end-to-end.
- Novel WITH refinement_provider_id: status transitions to pending → in_progress → done,
  refined_text populated, refined_at timestamp set.
- Refiner exception → refinement_status='error', refinement_error set,
  refined_text stays NULL, draft remains visible.
- Empty draft skips refinement cleanly.
- drain_on_startup resets stuck in_progress rows → pending and re-spawns workers.
- retry-refinement route flips error → pending and re-spawns worker.
- Refiner cache: identical (provider, draft) hits cache; different provider misses.
"""

from __future__ import annotations

import pytest

from backend.db import init_db, open_conn
from backend.models import TranslationResult
from backend.services import providers as providers_svc
from backend.services import queue as queue_svc
from backend.services import refiner as refiner_svc

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


async def _make_novel_with_chapter(refinement_provider_id: int | None = None) -> tuple[int, int]:
    """Insert a novel + one pending chapter, return (novel_id, chapter_id)."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, refinement_provider_id) "
            "VALUES (?, ?, ?)",
            ("test novel", "paste", refinement_provider_id),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "status, translate_queued) VALUES (?, 1, '原文', 'pending', 1)",
            (novel_id,),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _seed_translator_provider() -> int:
    """Create the default translator provider; return its id."""
    p = await providers_svc.create_provider(
        name="translator",
        provider_type="gemini",
        model_id="m",
        is_default=True,
    )
    return p.id


async def _seed_refiner_provider() -> int:
    """Create a second provider that will play the refiner role."""
    p = await providers_svc.create_provider(
        name="refiner",
        provider_type="gemini",
        model_id="r",
    )
    return p.id


# Default fixture body is comfortably above _REFINEMENT_MIN_DRAFT_CHARS so
# the refinement path runs end-to-end. Tests that need to exercise the
# tiny-draft skip path pass a short body explicitly.
_FIXTURE_DRAFT_BODY = "A normal-length draft paragraph " * 12


def _stub_translate(monkeypatch, body: str = _FIXTURE_DRAFT_BODY) -> None:
    """Replace queue.translate_chapter with a fake that returns a fixed result."""
    async def _fake(*args, **kwargs):
        return TranslationResult(
            title_en="t", translated_text=body, new_terms=[],
        )
    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake)


# ----- happy path: no refinement provider ------------------------------------

async def test_no_refinement_provider_keeps_status_none(monkeypatch):
    """A novel with refinement_provider_id=NULL must finish with
    refinement_status='none', refined_text NULL. The two-pass chain
    becomes a one-pass no-op for these novels."""
    await _seed_translator_provider()
    novel_id, chapter_id = await _make_novel_with_chapter()
    _stub_translate(monkeypatch)
    # Refiner must NOT be called when no provider is configured.
    async def _fail(*a, **kw):
        raise AssertionError("refiner ran for a novel without refinement_provider_id")
    monkeypatch.setattr("backend.services.queue.refine_chapter", _fail)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        # Chain into refine same as _run_translate does.
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status, refinement_status, refined_text, refined_at "
            "FROM chapters WHERE id = ?", (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["status"] == "done"
    assert row["refinement_status"] == "none"
    assert row["refined_text"] is None
    assert row["refined_at"] is None


# ----- happy path: refinement configured -------------------------------------

async def test_refinement_pipeline_end_to_end(monkeypatch):
    """Novel with refinement_provider_id set runs translator → pending →
    in_progress → done with refined_text populated."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    draft_body = _FIXTURE_DRAFT_BODY
    _stub_translate(monkeypatch, body=draft_body)

    async def _fake_refine(draft, provider, glossary=None, use_cache=True):
        return f"REFINED({draft})"
    monkeypatch.setattr("backend.services.queue.refine_chapter", _fake_refine)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        # After translate-commit: status='done', refinement_status='pending'.
        cur = await conn.execute(
            "SELECT status, refinement_status, translated_text, refined_text "
            "FROM chapters WHERE id = ?", (chapter_id,),
        )
        post_translate = await cur.fetchone()
        assert post_translate["status"] == "done"
        assert post_translate["refinement_status"] == "pending"
        assert post_translate["translated_text"] == draft_body
        assert post_translate["refined_text"] is None

        # Now run refine in the same lock acquisition (mirrors _run_translate).
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT refinement_status, refined_text, refined_at, "
            "refinement_error, refined_by_provider_id "
            "FROM chapters WHERE id = ?", (chapter_id,),
        )
        post_refine = await cur.fetchone()
    assert post_refine["refinement_status"] == "done"
    assert post_refine["refined_text"] == f"REFINED({draft_body})"
    assert post_refine["refined_at"] is not None
    assert post_refine["refinement_error"] is None
    # Design v2 Phase B: the refiner provider id must be stamped on the
    # chapter so the reader's bilingual pane label can show attribution.
    assert post_refine["refined_by_provider_id"] == refiner_id


# ----- refiner error ---------------------------------------------------------

async def test_refiner_exception_marks_error_keeps_draft(monkeypatch):
    """When the refiner raises, the chapter ends with status='done',
    refinement_status='error', refinement_error set, refined_text NULL.
    The user can still read the draft."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    _stub_translate(monkeypatch)

    async def _broken_refine(*a, **kw):
        raise RuntimeError("simulated upstream 500")
    monkeypatch.setattr("backend.services.queue.refine_chapter", _broken_refine)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status, translated_text, refinement_status, "
            "refined_text, refinement_error FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["status"] == "done", "draft must remain visible after refine error"
    assert row["translated_text"] == _FIXTURE_DRAFT_BODY
    assert row["refinement_status"] == "error"
    assert row["refined_text"] is None
    assert "simulated upstream 500" in (row["refinement_error"] or "")


# ----- empty draft -----------------------------------------------------------

async def test_empty_draft_skips_refinement(monkeypatch):
    """An empty translated_text shouldn't be sent to the refiner — clear the
    pending flag and move on. Prevents the refiner returning a hallucinated
    body for nothing."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    # Set chapter directly into pending state with empty draft.
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = '', "
            "refinement_status = 'pending' WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()

    refiner_called = {"hit": False}
    async def _should_not_run(*a, **kw):
        refiner_called["hit"] = True
        return "x"
    monkeypatch.setattr("backend.services.queue.refine_chapter", _should_not_run)

    async with open_conn() as conn:
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT refinement_status FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "none"
    assert refiner_called["hit"] is False


async def test_tiny_draft_skips_refinement(monkeypatch):
    """Non-empty but below the min-draft threshold — most likely a parse
    error or short 番外 / author-note row. Refining would burn a paid call
    on text the reader displays as-is. Behavior mirrors the empty-draft
    path: status → 'none', refiner never called."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    tiny = "Author's note: short."  # well below _REFINEMENT_MIN_DRAFT_CHARS
    assert len(tiny) < queue_svc._REFINEMENT_MIN_DRAFT_CHARS
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = ?, "
            "refinement_status = 'pending' WHERE id = ?",
            (tiny, chapter_id),
        )
        await conn.commit()

    refiner_called = {"hit": False}
    async def _should_not_run(*a, **kw):
        refiner_called["hit"] = True
        return "x"
    monkeypatch.setattr("backend.services.queue.refine_chapter", _should_not_run)

    async with open_conn() as conn:
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT refinement_status, refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "none"
    assert row["refined_text"] is None
    assert refiner_called["hit"] is False


async def test_normal_draft_still_refines(monkeypatch):
    """Sanity check the threshold isn't blocking real chapters — a draft
    above _REFINEMENT_MIN_DRAFT_CHARS reaches the refiner normally."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    normal = "A" * (queue_svc._REFINEMENT_MIN_DRAFT_CHARS + 50)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = ?, "
            "refinement_status = 'pending' WHERE id = ?",
            (normal, chapter_id),
        )
        await conn.commit()

    async def _stub_refine(draft, provider, glossary=None, **_kw):
        return draft + " [refined]"
    monkeypatch.setattr("backend.services.queue.refine_chapter", _stub_refine)

    async with open_conn() as conn:
        await queue_svc._refine_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT refinement_status, refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "done"
    assert row["refined_text"].endswith("[refined]")


# ----- crash recovery via drain_on_startup -----------------------------------

async def test_drain_resets_stuck_in_progress(monkeypatch):
    """drain_on_startup must reset refinement_status='in_progress' (a
    crashed mid-refinement row) to 'pending' so the worker re-runs on the
    next boot. Confirmed-user decision 2026-05-23: auto-recovery, not
    manual retry."""
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = 'd', "
            "translate_queued = 0, refinement_status = 'in_progress', "
            "refinement_error = 'partial output discarded' WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()

    # Stub the spawned worker so drain doesn't actually try to run.
    spawned: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "backend.services.queue._spawn",
        lambda coro: (spawned.append(("spawned",)), coro.close()),
    )

    await queue_svc.drain_on_startup()

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT refinement_status, refinement_error FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "pending"
    assert row["refinement_error"] is None
    assert spawned, "drain did not spawn a refine task for the recovered row"


# ----- refiner cache --------------------------------------------------------

async def test_refiner_cache_hit_avoids_call(monkeypatch):
    """Identical (provider, draft) inputs must hit the on-disk cache and
    skip the LLM round-trip. Prevents paying for the same polish twice
    when the user retranslates without changing anything upstream."""
    p = await providers_svc.create_provider(
        name="cached-r", provider_type="gemini", model_id="m",
    )

    call_count = {"n": 0}

    # Stub the backend's _complete_plain so we can count calls without
    # touching Google.
    class _FakeBackend:
        name = "fake"
        model_id = "m"
        system_instruction = ""
        def cache_identity(self):
            return f"{self.name}:{self.model_id}"
        async def _complete_plain(self, prompt):
            call_count["n"] += 1
            return "POLISHED OUTPUT"

    monkeypatch.setattr(
        "backend.services.refiner.get_translator",
        lambda provider: _FakeBackend(),
    )

    out1 = await refiner_svc.refine_chapter("the draft", p)
    out2 = await refiner_svc.refine_chapter("the draft", p)
    assert out1 == "POLISHED OUTPUT"
    assert out2 == "POLISHED OUTPUT"
    assert call_count["n"] == 1, (
        f"refine_chapter ignored cache; called backend {call_count['n']} times"
    )


async def test_refiner_cache_misses_on_different_draft(monkeypatch):
    p = await providers_svc.create_provider(
        name="miss-r", provider_type="gemini", model_id="m",
    )

    seen_prompts: list[str] = []

    class _FakeBackend:
        name = "fake"
        model_id = "m"
        system_instruction = ""
        def cache_identity(self):
            return f"{self.name}:{self.model_id}"
        async def _complete_plain(self, prompt):
            seen_prompts.append(prompt)
            return f"polished:{len(prompt)}"

    monkeypatch.setattr(
        "backend.services.refiner.get_translator",
        lambda provider: _FakeBackend(),
    )

    await refiner_svc.refine_chapter("draft A", p)
    await refiner_svc.refine_chapter("draft B", p)
    assert len(seen_prompts) == 2, "different drafts must miss the cache"


# ----- retry-refinement route -----------------------------------------------

@pytest.fixture
def quiet_app(monkeypatch):
    """TestClient triggers FastAPI's lifespan which runs _probe_backends
    AND queue.drain_on_startup. Stub both:
    - probe: would try to round-trip a real provider with the fake 'r' model.
    - drain: would reset refinement_status='in_progress' → 'pending' and
      spawn a refine worker, racing the route assertion. The test_409
      case explicitly verifies the in_progress short-circuit; drain
      mutates the state out from under it."""
    async def _no_probe(default_provider):
        return None
    async def _no_drain():
        return None
    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)
    from backend.main import app
    return app


async def test_retry_refinement_flips_error_to_pending(quiet_app):
    """POST /retry-refinement on an errored row must clear the error and
    set refinement_status='pending'. The worker spawn happens via
    queue_svc.spawn_refine_worker; we don't run it here, just verify the
    DB state transition."""
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = 'd', "
            "refinement_status = 'error', refinement_error = 'old error', "
            "refined_text = NULL WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()

    from fastapi.testclient import TestClient

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/retry-refinement"
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "queued"}

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT refinement_status, refinement_error, refined_text "
            "FROM chapters WHERE id = ?", (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "pending"
    assert row["refinement_error"] is None
    assert row["refined_text"] is None


async def test_retry_refinement_409_when_novel_has_no_refiner(quiet_app):
    """Retry must refuse cleanly when the novel has no
    refinement_provider_id — the worker would just clear the pending flag
    again, so spawning is pointless."""
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=None,
    )
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = 'd' "
            "WHERE id = ?", (chapter_id,),
        )
        await conn.commit()

    from fastapi.testclient import TestClient

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/retry-refinement"
        )
    assert resp.status_code == 409
    assert "no refinement provider" in resp.json()["detail"].lower()


async def test_retry_refinement_409_while_in_progress(quiet_app):
    """No retry while refinement is mid-run — would race the worker."""
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = 'd', "
            "refinement_status = 'in_progress' WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()

    from fastapi.testclient import TestClient

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/retry-refinement"
        )
    assert resp.status_code == 409


async def test_refine_preclaim_crash_recovery(monkeypatch):
    """A crash BEFORE the atomic pending->in_progress claim (here
    _resolve_refinement_provider raising) must land the row in 'error', not
    leave it stuck at 'pending'. Regression: _run_refine's recovery UPDATE
    once rescued only 'in_progress', so a pre-claim failure re-spawned a
    failing task on every drain_on_startup instead of surfacing for retry."""
    await _seed_translator_provider()
    refiner_id = await _seed_refiner_provider()
    novel_id, chapter_id = await _make_novel_with_chapter(
        refinement_provider_id=refiner_id,
    )
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'done', translated_text = ?, "
            "refinement_status = 'pending' WHERE id = ?",
            (_FIXTURE_DRAFT_BODY, chapter_id),
        )
        await conn.commit()

    async def _boom(_conn, _novel_id):
        raise RuntimeError("provider resolve failed")

    # The crash lands at line ~1016 in _refine_chapter_in_db: after the
    # draft-length check, before the atomic pending->in_progress claim.
    monkeypatch.setattr(
        "backend.services.queue._resolve_refinement_provider", _boom
    )

    # _run_refine owns the except-recovery handler under test.
    await queue_svc._run_refine(novel_id, chapter_id)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT refinement_status, refinement_error FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refinement_status"] == "error"
    assert row["refinement_error"]
