"""Cover-image scraping tests — Phase 1 of the wireframes redesign.

Validates that `scrape_url` also pulls a cover image from the page's
og:image / twitter:image / image_src meta tags, and that the cover fetch
reuses the same SSRF / scheme / size / timeout / redirect guards the text
fetch already enforces. The invariant under test: cover-scrape failure
must NEVER propagate out of `scrape_url` — it returns a ScrapeResult with
cover_bytes=None and lets the route layer continue.
"""

from __future__ import annotations

import httpx
import pytest

from backend.services import scraper
from backend.services.scraper import (
    _extract_cover_url,
    scrape_url,
)

# Small valid PNG: 1×1 transparent pixel. Magic bytes + minimum chunks.
# Used as a stand-in cover image so the magic-byte sniff in covers.py passes.
_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xfc\xcf\xc0P\x0f\x00\x04\x85\x01\x80"
    b"\x84\x90\x97\xab"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _public_resolver(host, port, *args, **kwargs):
    return [(2, 1, 6, "", ("8.8.8.8", 0))]


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)


# ---- _extract_cover_url (pure) -----------------------------------------------

def test_extract_og_image_property_first():
    html = '<html><head><meta property="og:image" content="https://cdn.x/c.jpg"></head></html>'
    assert _extract_cover_url(html, "https://x.example/p") == "https://cdn.x/c.jpg"


def test_extract_og_image_attribute_swapped():
    """content= before property= is the other common attribute order."""
    html = '<html><head><meta content="https://cdn.x/c.jpg" property="og:image"></head></html>'
    assert _extract_cover_url(html, "https://x.example/p") == "https://cdn.x/c.jpg"


def test_extract_twitter_image_fallback():
    html = '<html><head><meta name="twitter:image" content="https://cdn.x/t.png"></head></html>'
    assert _extract_cover_url(html, "https://x.example/p") == "https://cdn.x/t.png"


def test_extract_image_src_link_fallback():
    html = '<html><head><link rel="image_src" href="https://cdn.x/legacy.gif"></head></html>'
    assert _extract_cover_url(html, "https://x.example/p") == "https://cdn.x/legacy.gif"


def test_extract_resolves_relative_url():
    html = '<html><head><meta property="og:image" content="/static/cover.png"></head></html>'
    assert _extract_cover_url(html, "https://x.example/novel/ch1") == "https://x.example/static/cover.png"


def test_extract_resolves_protocol_relative_url():
    html = '<html><head><meta property="og:image" content="//cdn.x/c.jpg"></head></html>'
    assert _extract_cover_url(html, "https://x.example/p") == "https://cdn.x/c.jpg"


def test_extract_none_when_missing():
    html = "<html><head><title>x</title></head></html>"
    assert _extract_cover_url(html, "https://x.example/") is None


def test_extract_skips_empty_content():
    html = '<html><head><meta property="og:image" content=""></head></html>'
    assert _extract_cover_url(html, "https://x.example/") is None


def test_extract_og_image_secure_url_variant():
    html = '<html><head><meta property="og:image:secure_url" content="https://cdn.x/c.jpg"></head></html>'
    assert _extract_cover_url(html, "https://x.example/") == "https://cdn.x/c.jpg"


# ---- scrape_url with cover ---------------------------------------------------

_HTML_WITH_COVER = """<!DOCTYPE html>
<html><head>
<title>Chapter 1</title>
<meta property="og:image" content="https://cdn.example/cover.png">
</head><body>
<article>
<h1>Chapter 1: The Beginning</h1>
<p>It was a bright cold day in April and the clocks were striking thirteen.
Winston Smith pulled his collar tight against the wind and slipped through
the door of Victory Mansions, his hand brushing the gritty handle as he
went.</p>
<p>The hallway smelt of boiled cabbage and old rag mats. At one end of it
a coloured poster had been tacked to the wall.</p>
</article>
</body></html>"""


@pytest.mark.asyncio
async def test_scrape_returns_cover_bytes_on_og_image(monkeypatch):
    """og:image present + reachable + valid image bytes → ScrapeResult
    carries cover_bytes + cover_ext."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        if "/cover.png" in str(request.url):
            return httpx.Response(
                200, content=_PNG_1x1, headers={"content-type": "image/png"},
            )
        return httpx.Response(
            200,
            content=_HTML_WITH_COVER.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)

    result = await scrape_url("https://novel.example/ch1")
    assert result.text  # text fetch worked
    assert result.cover_bytes == _PNG_1x1
    assert result.cover_ext == "png"


@pytest.mark.asyncio
async def test_scrape_returns_none_cover_when_no_meta(monkeypatch):
    """Page without og:image / twitter:image → cover_bytes is None,
    text fetch still succeeds."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    html_no_cover = _HTML_WITH_COVER.replace(
        '<meta property="og:image" content="https://cdn.example/cover.png">',
        "",
    )

    def handler(request):
        return httpx.Response(
            200,
            content=html_no_cover.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)

    result = await scrape_url("https://novel.example/ch1")
    assert result.text
    assert result.cover_bytes is None
    assert result.cover_ext is None


@pytest.mark.asyncio
async def test_cover_fetch_failure_does_not_break_text_scrape(monkeypatch):
    """og:image present but the image URL 404s → text fetch still succeeds,
    cover_bytes is None. The whole point of best-effort: the import must
    not fail because the cover did."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        if "/cover.png" in str(request.url):
            return httpx.Response(404, content=b"not found")
        return httpx.Response(
            200,
            content=_HTML_WITH_COVER.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)

    result = await scrape_url("https://novel.example/ch1")
    assert result.text
    assert result.cover_bytes is None


@pytest.mark.asyncio
async def test_cover_url_pointing_at_private_ip_blocked(monkeypatch):
    """og:image resolving to a private IP must trip the SSRF guard on the
    cover-fetch path and result in cover_bytes=None. The text fetch (which
    resolves to a public IP) succeeds unaffected."""
    resolutions = {
        "novel.example": [(2, 1, 6, "", ("8.8.8.8", 0))],
        "internal-cdn.example": [(2, 1, 6, "", ("10.0.0.1", 0))],
    }

    def fake_resolver(host, port, *args, **kwargs):
        return resolutions.get(host, [(2, 1, 6, "", ("8.8.8.8", 0))])

    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_resolver)

    html = _HTML_WITH_COVER.replace(
        "https://cdn.example/cover.png", "http://internal-cdn.example/cover.png",
    )

    def handler(request):
        # We should NEVER reach the internal-cdn handler — the SSRF guard
        # rejects before httpx dials.
        if "internal-cdn.example" in str(request.url):
            raise AssertionError(
                "internal IP cover fetch must be blocked before dial"
            )
        return httpx.Response(
            200,
            content=html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)

    result = await scrape_url("https://novel.example/ch1")
    assert result.text
    assert result.cover_bytes is None


@pytest.mark.asyncio
async def test_cover_oversize_aborts_mid_stream(monkeypatch):
    """An image larger than MAX_COVER_BYTES must abort and return None for
    the cover — text fetch unaffected."""
    from backend.services.covers import MAX_COVER_BYTES

    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        if "/cover.png" in str(request.url):
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\n" + b"a" * (MAX_COVER_BYTES + 10),
                headers={"content-type": "image/png"},
            )
        return httpx.Response(
            200,
            content=_HTML_WITH_COVER.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)

    result = await scrape_url("https://novel.example/ch1")
    assert result.text
    assert result.cover_bytes is None


@pytest.mark.asyncio
async def test_cover_bad_magic_bytes_rejected(monkeypatch):
    """Server returns image/png content-type but the bytes aren't actually
    a PNG (e.g. a renamed binary). Magic-byte sniff in covers.sniff_image_ext
    must reject and we return cover_bytes=None."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        if "/cover.png" in str(request.url):
            return httpx.Response(
                200,
                content=b"MZ\x90\x00",  # PE/EXE magic, NOT PNG
                headers={"content-type": "image/png"},
            )
        return httpx.Response(
            200,
            content=_HTML_WITH_COVER.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)

    result = await scrape_url("https://novel.example/ch1")
    assert result.text
    assert result.cover_bytes is None


@pytest.mark.asyncio
async def test_cover_wrong_content_type_rejected(monkeypatch):
    """Server returns text/html for the cover URL (e.g. soft 404 redirect to
    an HTML error page). We refuse to feed that to the cover writer."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        if "/cover.png" in str(request.url):
            return httpx.Response(
                200, content=b"<html>not found</html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(
            200,
            content=_HTML_WITH_COVER.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)

    result = await scrape_url("https://novel.example/ch1")
    assert result.text
    assert result.cover_bytes is None
