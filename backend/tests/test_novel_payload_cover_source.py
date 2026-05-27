"""GET /api/novels surfaces `cover_source` so the library card can render
the source pip without an extra round-trip. Tests every ingestion path that
stamps the field — URL scrape ('url'), manual upload ('upload'), and the
absence of a stamp (paste import leaves it NULL).

The EPUB path also stamps 'epub' but the EPUB decoder needs a valid EPUB
fixture; routing the EPUB test through the full /upload endpoint would
duplicate the EPUB-test rig already in test_upload_epub.py. We exercise
the helper directly here for completeness instead.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.db import init_db, open_conn
from backend.services import scraper

# Reuse the 1×1 PNG fixture from the cover-scrape tests so the magic-byte
# sniff actually passes.
from backend.tests.test_scraper_cover import _PNG_1x1


def _public_resolver(host, port, *args, **kwargs):
    return [(2, 1, 6, "", ("8.8.8.8", 0))]


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched)


@pytest.fixture
def app_with_stubs(monkeypatch):
    """TestClient with lifespan stubs that skip the real provider probe and
    queue drain — both touch state we don't want in these tests."""
    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app
    return app


async def _wipe_novels():
    async with open_conn() as conn:
        for t in ("chapters", "novels"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await conn.commit()


@pytest.mark.asyncio
async def test_paste_import_leaves_cover_source_null(app_with_stubs):
    """A vanilla /paste import never produces a cover → cover_source NULL
    → API returns None."""
    await init_db()
    await _wipe_novels()

    with TestClient(app_with_stubs) as client:
        resp = client.post(
            "/api/translate/paste",
            json={
                "title": "Paste No Cover",
                "text": "Chapter 1\n\nIt was a cold day.",
            },
        )
        assert resp.status_code == 200, resp.text

        listing = client.get("/api/novels").json()

    assert len(listing) == 1
    assert listing[0]["title"] == "Paste No Cover"
    assert listing[0]["cover_source"] is None
    assert listing[0]["cover_image_path"] is None


@pytest.mark.asyncio
async def test_url_scrape_with_og_image_stamps_url_source(app_with_stubs, monkeypatch):
    """End-to-end: a scraped URL that exposes og:image lands in the library
    payload with cover_source='url' and a non-null cover_image_path."""
    await init_db()
    await _wipe_novels()

    html = (
        '<!DOCTYPE html><html><head>'
        '<title>Test</title>'
        '<meta property="og:image" content="https://cdn.example/cover.png">'
        '</head><body><article>'
        '<h1>Chapter 1</h1>'
        '<p>It was a bright cold day in April and the clocks were striking '
        'thirteen as Winston tried the lift door once more without success.</p>'
        '<p>The hallway smelt of boiled cabbage and old rag mats and it '
        'reminded him of the place he had grown up in years ago.</p>'
        '</article></body></html>'
    )

    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        if "/cover.png" in str(request.url):
            return httpx.Response(
                200, content=_PNG_1x1, headers={"content-type": "image/png"},
            )
        return httpx.Response(
            200, content=html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)

    with TestClient(app_with_stubs) as client:
        resp = client.post(
            "/api/translate/scrape",
            json={"url": "https://novel.example/ch1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cover_extracted"] is True

        listing = client.get("/api/novels").json()

    assert len(listing) == 1
    novel = listing[0]
    assert novel["cover_source"] == "url"
    assert novel["cover_image_path"] is not None


@pytest.mark.asyncio
async def test_manual_cover_upload_stamps_upload_source(app_with_stubs):
    """POST /api/covers/{novel_id}/cover (manual upload) sets
    cover_source='upload'. Validates the upload path independently of
    scraping."""
    await init_db()
    await _wipe_novels()

    with TestClient(app_with_stubs) as client:
        # First create a novel so we have an id to attach the cover to.
        create_resp = client.post(
            "/api/translate/paste",
            json={"title": "Manual Upload", "text": "Chapter 1\n\nFoo."},
        )
        assert create_resp.status_code == 200, create_resp.text
        novel_id = create_resp.json()["novel_id"]

        # Confirm cover_source starts NULL.
        before = client.get("/api/novels").json()[0]
        assert before["cover_source"] is None

        # Cover route is mounted at /api/novels/{id}/cover (see main.py).
        upload = client.post(
            f"/api/novels/{novel_id}/cover",
            files={"file": ("cover.png", _PNG_1x1, "image/png")},
        )
        assert upload.status_code == 200, upload.text

        after = client.get("/api/novels").json()[0]

    assert after["cover_source"] == "upload"
    assert after["cover_image_path"] is not None


@pytest.mark.asyncio
async def test_write_cover_for_novel_stamps_epub_source(app_with_stubs):
    """Direct helper test for the EPUB path: write_cover_for_novel called
    with source='epub' stamps the column accordingly. The /upload endpoint
    calls it with source=source_type which is 'epub' for EPUB imports."""
    from backend.services.covers import write_cover_for_novel

    await init_db()
    await _wipe_novels()

    with TestClient(app_with_stubs) as client:
        resp = client.post(
            "/api/translate/paste",
            json={"title": "Epub Test", "text": "Chapter 1\n\nFoo."},
        )
        novel_id = resp.json()["novel_id"]

    async with open_conn() as conn:
        written = await write_cover_for_novel(
            conn, novel_id, _PNG_1x1, source="epub",
        )
        await conn.commit()
        assert written is not None

        cur = await conn.execute(
            "SELECT cover_source, cover_image_path FROM novels WHERE id = ?",
            (novel_id,),
        )
        row = await cur.fetchone()

    assert row["cover_source"] == "epub"
    assert row["cover_image_path"] is not None
