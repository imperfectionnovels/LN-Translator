"""Block 3.1: edit-paragraph race-guard regression.

The `POST /edit-paragraph` route at `backend/routes/chapters.py:293` uses
a strict `before_md` equality check on the indexed paragraph to detect
concurrent mutations of `translated_text` (typically a retranslate that
fired between the reader loading the page and the user clicking save).
The guard returns 409 instead of silently overwriting the fresh body.

Pre-test the suite only covered the happy path — these tests lock in
the contended path so a future refactor can't silently weaken the guard.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.db import init_db, open_conn


async def _reset_db() -> None:
    async with open_conn() as conn:
        for t in ("style_edits", "chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {t}")
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
def quiet_app(monkeypatch):
    """Stub the lifespan probe + drain so TestClient doesn't try to
    resolve a real provider or kick off background work."""
    async def _no_probe(_default):
        return None
    async def _no_drain():
        return None
    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)
    from backend.main import app
    return app


async def _make_chapter_with_body(body: str) -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("race-test", "paste"),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, '原文', ?, 'done')",
            (novel_id, body),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _count_style_edits(novel_id: int) -> int:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM style_edits WHERE novel_id = ?",
            (novel_id,),
        )
        row = await cur.fetchone()
    return int(row["n"] or 0)


@pytest.mark.asyncio
async def test_edit_paragraph_happy_path_writes_style_edit(quiet_app):
    """Sanity baseline: an edit against the current paragraph content
    succeeds and writes a style_edits row."""
    novel_id, _ = await _make_chapter_with_body(
        "Paragraph zero.\n\nParagraph one.\n\nParagraph two."
    )
    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "Paragraph one.",
                "after_text": "Paragraph one, edited.",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert await _count_style_edits(novel_id) == 1


@pytest.mark.asyncio
async def test_edit_paragraph_409_when_body_changed_under_us(quiet_app):
    """Simulate a concurrent retranslate: the reader posted with the
    pre-retranslate `before_md`, but `translated_text` has since been
    rewritten. The route MUST 409 and MUST NOT write a style_edits row.

    Without the guard, the route would either splice the user's edit
    into the wrong paragraph (because paragraph_index now points
    elsewhere) or silently drop the retranslate's work by overwriting
    `translated_text`."""
    novel_id, chapter_id = await _make_chapter_with_body(
        "Old paragraph zero.\n\nOld paragraph one.\n\nOld paragraph two."
    )
    # Simulate a retranslate landing between page-load and POST.
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE id = ?",
            (
                "New paragraph zero.\n\nNew paragraph one.\n\nNew paragraph two.",
                chapter_id,
            ),
        )
        await conn.commit()

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "Old paragraph one.",  # stale snapshot
                "after_text": "Edited at the user's keyboard.",
            },
        )
    assert resp.status_code == 409, resp.text
    assert "changed since the page loaded" in resp.json()["detail"]

    # No style_edits row should have been written.
    assert await _count_style_edits(novel_id) == 0

    # And the retranslate's body must survive untouched.
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["translated_text"].startswith("New paragraph zero")


@pytest.mark.asyncio
async def test_edit_paragraph_refined_source_writes_to_refined_column(quiet_app):
    """Section 8: source='refined' mutates refined_text, not the draft.
    Draft column stays untouched so a future retranslate still sees the
    translator's original output."""
    novel_id, chapter_id = await _make_chapter_with_body(
        "Draft p0.\n\nDraft p1.\n\nDraft p2."
    )
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refined_text = ?, refinement_status = 'done' "
            "WHERE id = ?",
            ("Refined p0.\n\nRefined p1.\n\nRefined p2.", chapter_id),
        )
        await conn.commit()

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "Refined p1.",
                "after_text": "Refined p1, polished further.",
                "source": "refined",
            },
        )
    assert resp.status_code == 200, resp.text

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text, refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    # Refined column got the edit.
    assert "Refined p1, polished further." in row["refined_text"]
    # Draft column is untouched — important so a Retranslate still has the
    # original translator output to work from.
    assert row["translated_text"] == "Draft p0.\n\nDraft p1.\n\nDraft p2."
    # style_edits row was written (one regardless of source).
    assert await _count_style_edits(novel_id) == 1


@pytest.mark.asyncio
async def test_edit_paragraph_refined_409_when_refined_text_missing(quiet_app):
    """source='refined' against a chapter without refined_text → 409, not
    a silent NULL splice. Reader and DB are out of sync (the user clicked
    edit on what they thought was refined text but the row never had any)."""
    novel_id, _ = await _make_chapter_with_body("Draft only.")
    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 0,
                "before_md": "Refined version.",
                "after_text": "edited",
                "source": "refined",
            },
        )
    assert resp.status_code == 409, resp.text
    assert "no refined text" in resp.json()["detail"]
    assert await _count_style_edits(novel_id) == 0


@pytest.mark.asyncio
async def test_edit_paragraph_refined_409_when_refined_body_changed(quiet_app):
    """Race guard works for refined-source edits too — if the refined body
    was rewritten (e.g. a retry-refinement landed) between page-load and
    POST, the stale before_md trips 409 and the new refinement survives."""
    novel_id, chapter_id = await _make_chapter_with_body("Draft body.")
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refined_text = ?, refinement_status = 'done' "
            "WHERE id = ?",
            ("Old refined p0.\n\nOld refined p1.", chapter_id),
        )
        await conn.commit()
    # Refinement retried in the background, body rewritten.
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refined_text = ? WHERE id = ?",
            ("New refined p0.\n\nNew refined p1.", chapter_id),
        )
        await conn.commit()

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "Old refined p1.",
                "after_text": "edited from stale page",
                "source": "refined",
            },
        )
    assert resp.status_code == 409, resp.text
    assert await _count_style_edits(novel_id) == 0
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT refined_text FROM chapters WHERE id = ?", (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refined_text"].startswith("New refined p0")


@pytest.mark.asyncio
async def test_edit_paragraph_default_source_is_draft(quiet_app):
    """Backward compat: an old client that omits `source` keeps editing
    the draft column. Tests the Pydantic default."""
    novel_id, chapter_id = await _make_chapter_with_body(
        "Draft p0.\n\nDraft p1."
    )
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refined_text = ?, refinement_status = 'done' "
            "WHERE id = ?",
            ("Refined p0.\n\nRefined p1.", chapter_id),
        )
        await conn.commit()

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 0,
                "before_md": "Draft p0.",
                "after_text": "Edited draft p0.",
                # no `source` field
            },
        )
    assert resp.status_code == 200, resp.text
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text, refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert "Edited draft p0." in row["translated_text"]
    # Refined column untouched.
    assert row["refined_text"] == "Refined p0.\n\nRefined p1."


@pytest.mark.asyncio
async def test_edit_paragraph_409_when_paragraph_index_out_of_range(quiet_app):
    """A retranslate that produces a SHORTER body (fewer paragraphs)
    leaves the user's stale `paragraph_index` pointing past the end.
    The route must 409 — not IndexError, not silently extend the list."""
    novel_id, chapter_id = await _make_chapter_with_body(
        "P0.\n\nP1.\n\nP2.\n\nP3.\n\nP4."  # 5 paragraphs
    )
    # Retranslate shrinks the body to 2 paragraphs.
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE id = ?",
            ("New P0.\n\nNew P1.", chapter_id),
        )
        await conn.commit()

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 3,
                "before_md": "P3.",
                "after_text": "edited",
            },
        )
    assert resp.status_code == 409, resp.text
    assert "out of range" in resp.json()["detail"]
    assert await _count_style_edits(novel_id) == 0
