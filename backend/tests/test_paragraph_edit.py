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
        for t in ("style_edits", "chapter_segments", "chapters", "novels", "providers"):
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


# ---------------------------------------------------------------------------
# CAT Phase 3: reroute through the segment write path
# ---------------------------------------------------------------------------

_ZH_SRC = "甲" * 29 + "。\n\n" + "乙" * 29 + "。\n\n" + "丙" * 29 + "。"


async def _make_1to1_chapter(body: str) -> tuple[int, int]:
    """A chapter whose source splits 1:1 against `body`, so the segment
    store builds with segments_state='ok'."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("reroute-test", "paste"),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, ?, ?, 'done')",
            (novel_id, _ZH_SRC, body),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _build_segments(novel_id: int) -> dict:
    from backend.services import segments as segments_svc
    async with open_conn() as conn:
        payload = await segments_svc.get_segments(conn, novel_id, 1)
        await conn.commit()
    return payload


async def _segment_rows(chapter_id: int) -> list:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT seg_index, target_text, status, origin "
            "FROM chapter_segments WHERE chapter_id = ? ORDER BY seg_index",
            (chapter_id,),
        )
        return list(await cur.fetchall())


@pytest.mark.asyncio
async def test_edit_paragraph_reroutes_through_segments_when_ok(quiet_app):
    """With a clean segment store, the reader's paragraph edit lands as an
    update_segment save: the row flips to edited/human, the body
    rematerializes from the store, and the legacy contract (response shape,
    style_edits row) is unchanged."""
    body = "Alpha paragraph.\n\nBeta paragraph.\n\nGamma paragraph."
    novel_id, chapter_id = await _make_1to1_chapter(body)
    payload = await _build_segments(novel_id)
    assert payload["segments_state"] == "ok"

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "Beta paragraph.",
                "after_text": "Beta paragraph, humanized.",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    rows = await _segment_rows(chapter_id)
    assert rows[1]["status"] == "edited"
    assert rows[1]["origin"] == "human"
    assert rows[1]["target_text"] == "Beta paragraph, humanized."
    assert rows[0]["status"] == "machine" and rows[2]["status"] == "machine"

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,)
        )
        row = await cur.fetchone()
    assert row["translated_text"] == (
        "Alpha paragraph.\n\nBeta paragraph, humanized.\n\nGamma paragraph."
    )
    # I1: the body IS the join of the targets.
    assert row["translated_text"] == "\n\n".join(
        r["target_text"] for r in rows
    )
    assert await _count_style_edits(novel_id) == 1


@pytest.mark.asyncio
async def test_edit_paragraph_legacy_path_without_segment_store(quiet_app):
    """A chapter whose segments were never built keeps the legacy
    direct-text splice; the reroute must not create rows on the fly."""
    novel_id, chapter_id = await _make_chapter_with_body(
        "P0.\n\nP1.\n\nP2."
    )
    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "P1.",
                "after_text": "P1 edited.",
            },
        )
    assert resp.status_code == 200
    assert await _segment_rows(chapter_id) == []
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,)
        )
        row = await cur.fetchone()
    assert row["translated_text"] == "P0.\n\nP1 edited.\n\nP2."
    assert await _count_style_edits(novel_id) == 1


@pytest.mark.asyncio
async def test_edit_paragraph_reroute_refined_variant(quiet_app):
    """source='refined' with a segment store built on the refined body:
    the edit routes through the store and mutates refined_text only."""
    draft = "Draft a.\n\nDraft b.\n\nDraft c."
    refined = "Refined a.\n\nRefined b.\n\nRefined c."
    novel_id, chapter_id = await _make_1to1_chapter(draft)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refined_text = ?, refinement_status = 'done' "
            "WHERE id = ?",
            (refined, chapter_id),
        )
        await conn.commit()
    payload = await _build_segments(novel_id)
    assert payload["variant"] == "refined"
    assert payload["segments_state"] == "ok"

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 0,
                "before_md": "Refined a.",
                "after_text": "Refined a, edited.",
                "source": "refined",
            },
        )
    assert resp.status_code == 200, resp.text
    rows = await _segment_rows(chapter_id)
    assert rows[0]["status"] == "edited"
    assert rows[0]["target_text"] == "Refined a, edited."
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text, refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["refined_text"] == "Refined a, edited.\n\nRefined b.\n\nRefined c."
    assert row["translated_text"] == draft


@pytest.mark.asyncio
async def test_edit_paragraph_409_still_fires_with_segment_store(quiet_app):
    """The endpoint's stale before_md guard is unchanged by the reroute."""
    body = "Alpha paragraph.\n\nBeta paragraph.\n\nGamma paragraph."
    novel_id, chapter_id = await _make_1to1_chapter(body)
    await _build_segments(novel_id)
    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "A stale snapshot.",
                "after_text": "edited",
            },
        )
    assert resp.status_code == 409
    rows = await _segment_rows(chapter_id)
    assert all(r["status"] == "machine" for r in rows)
    assert await _count_style_edits(novel_id) == 0


@pytest.mark.asyncio
async def test_edit_paragraph_stale_store_after_refinement_takes_legacy_path(quiet_app):
    """Freshness predicate regression: the store was built while the chapter
    displayed the DRAFT; a refinement lands later (the refiner does not
    touch segments, so segments_state stays 'ok' with a stale
    segments_rev); the user edits a paragraph the refiner left
    byte-identical. Without the segments_rev freshness check every other
    precondition passes and the reroute would rematerialize refined_text
    from the stale DRAFT targets, silently reverting the whole refinement.
    The edit must take the LEGACY splice and leave the other refined
    paragraphs untouched."""
    draft = "Alpha paragraph.\n\nBeta paragraph.\n\nGamma paragraph."
    novel_id, chapter_id = await _make_1to1_chapter(draft)
    payload = await _build_segments(novel_id)
    assert payload["variant"] == "draft"
    assert payload["segments_state"] == "ok"

    # Refinement lands AFTER the store was built; slot 1 is byte-identical
    # to the draft's, the other slots differ.
    refined = "Refined alpha.\n\nBeta paragraph.\n\nRefined gamma."
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET refined_text = ?, refinement_status = 'done' "
            "WHERE id = ?",
            (refined, chapter_id),
        )
        await conn.commit()

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "Beta paragraph.",
                "after_text": "Beta paragraph, edited.",
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
    # The refinement survives; only the edited paragraph changed.
    assert row["refined_text"] == (
        "Refined alpha.\n\nBeta paragraph, edited.\n\nRefined gamma."
    )
    assert row["translated_text"] == draft
    # The stale store was NOT written through (self-heal fixes it on the
    # next editor open).
    rows = await _segment_rows(chapter_id)
    assert all(r["status"] == "machine" for r in rows)
    assert [r["target_text"] for r in rows] == [
        "Alpha paragraph.", "Beta paragraph.", "Gamma paragraph.",
    ]


@pytest.mark.asyncio
async def test_edit_paragraph_stale_store_after_body_mutation_takes_legacy_path(quiet_app):
    """Same freshness hole, plain-mutation shape: the body was rewritten
    out-of-band (retranslate-like) with the SAME paragraph count and one
    byte-identical slot. The edit must splice the NEW body, not
    rematerialize the old targets over it."""
    v1 = "One red.\n\nShared middle.\n\nThree red."
    novel_id, chapter_id = await _make_1to1_chapter(v1)
    payload = await _build_segments(novel_id)
    assert payload["segments_state"] == "ok"

    v2 = "One blue.\n\nShared middle.\n\nThree blue."
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE id = ?",
            (v2, chapter_id),
        )
        await conn.commit()

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 1,
                "before_md": "Shared middle.",
                "after_text": "Shared middle, edited.",
            },
        )
    assert resp.status_code == 200, resp.text
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,)
        )
        row = await cur.fetchone()
    # The v2 body survives with only the edited slot changed; a stale-store
    # write-through would have reverted the other paragraphs to v1.
    assert row["translated_text"] == (
        "One blue.\n\nShared middle, edited.\n\nThree blue."
    )
    rows = await _segment_rows(chapter_id)
    assert all(r["status"] == "machine" for r in rows)


@pytest.mark.asyncio
async def test_edit_paragraph_legacy_when_state_not_ok(quiet_app):
    """A 'partial' chapter keeps the legacy splice: the display-paragraph
    to seg_index mapping is only defined for 'ok' chapters; the store
    self-heals on the next editor open."""
    src = "\n\n".join(
        ["甲" * 29 + "。", "乙" * 29 + "。", "丙" * 29 + "。", "丁" * 29 + "。"]
    )
    tgt = "\n\n".join(["A" * 60, "B" * 60, "C" * 60])
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('p', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, ?, ?, 'done')",
            (novel_id, src, tgt),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    payload = await _build_segments(novel_id)
    assert payload["segments_state"] == "partial"

    with TestClient(quiet_app) as client:
        resp = client.post(
            f"/api/novels/{novel_id}/chapters/1/edit-paragraph",
            json={
                "paragraph_index": 0,
                "before_md": "A" * 60,
                "after_text": "Legacy spliced.",
            },
        )
    assert resp.status_code == 200
    # The reroute did NOT run: no row flipped to edited.
    rows = await _segment_rows(chapter_id)
    assert all(r["status"] == "machine" for r in rows)
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,)
        )
        row = await cur.fetchone()
    assert row["translated_text"].startswith("Legacy spliced.")


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
