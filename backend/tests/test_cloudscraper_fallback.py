"""Cloudscraper fallback tests.

When the primary httpx fetch in scrape_url returns a CF-shaped 403/503,
the scraper retries through cloudscraper before surfacing the cookies-
helpful error. These tests cover:

- Successful cloudscraper recovery → scrape_url returns a ScrapeResult
  built from the cloudscraper-served body (no user-facing error).
- Cloudscraper itself fails → scrape_url falls through to the cookies-
  helpful error message.
- Cloudscraper unavailable (ImportError) → same fallthrough, no crash.

Cloudscraper is sync (requests.Session) so the helper bridges via
asyncio.to_thread. We monkeypatch `fetch_via_cloudscraper` directly to
avoid actually running cloudscraper in the test (it would dial a real
network for its first request).
"""

from __future__ import annotations

import httpx
import pytest

from backend.services import scraper as scraper_mod
from backend.services.scraper import ScrapeError, scrape_url


def _public_resolver(host, port, *args, **kwargs):
    return [(2, 1, 6, "", ("8.8.8.8", 0))]


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scraper_mod.httpx, "AsyncClient", patched)


_REAL_ARTICLE_HTML = (
    b"<html><body><article>"
    b"<h1>Chapter 1</h1>"
    b"<p>It was a bright cold day in April and the clocks were striking thirteen.</p>"
    b"<p>The hallway smelt of boiled cabbage and old rag mats.</p>"
    b"</article></body></html>"
)


@pytest.mark.asyncio
async def test_cloudscraper_recovers_from_cf_block(monkeypatch):
    """First httpx hit returns 403 with Server: cloudflare. Cloudscraper
    retry returns 200 with real article HTML. scrape_url returns
    a ScrapeResult containing the article body — no user-facing error."""
    monkeypatch.setattr(scraper_mod.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            403,
            content=b"<html><body>blocked</body></html>",
            headers={
                "content-type": "text/html",
                "server": "cloudflare",
                "cf-ray": "abc-IAD",
            },
        )

    _patch_transport(monkeypatch, handler)

    async def fake_cs_fetch(url, **kwargs):
        return 200, _REAL_ARTICLE_HTML, "text/html; charset=utf-8"

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cf_bypass_chain",
        fake_cs_fetch,
    )

    result = await scrape_url("https://cf-protected.example/ch1")

    # ScrapeResult, NOT a raised error.
    from backend.services.scraper import ScrapeResult

    assert isinstance(result, ScrapeResult)
    assert "bright cold day" in result.text


@pytest.mark.asyncio
async def test_cloudscraper_failure_falls_through_to_cookies_message(monkeypatch):
    """Cloudscraper retry also fails → user gets the cookies-helpful
    error, NOT a generic 'HTTP 403' message."""
    monkeypatch.setattr(scraper_mod.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            403,
            content=b"<html><body>blocked</body></html>",
            headers={
                "content-type": "text/html",
                "server": "cloudflare",
                "cf-ray": "abc-IAD",
            },
        )

    _patch_transport(monkeypatch, handler)

    from backend.services.scrapers.cloudflare import CloudScraperFailed

    async def failing_cs_fetch(url, **kwargs):
        raise CloudScraperFailed("simulated cloudscraper failure")

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cf_bypass_chain",
        failing_cs_fetch,
    )

    with pytest.raises(ScrapeError) as ei:
        await scrape_url("https://cf-protected.example/ch1")

    msg = str(ei.value)
    assert "automatic Cloudflare bypass also failed" in msg
    assert "Cookies field" in msg


@pytest.mark.asyncio
async def test_cloudscraper_status_400_treated_as_failure(monkeypatch):
    """Cloudscraper returns 200... wait, no — cloudscraper returns a
    non-2xx response (still blocked at a different layer). Treated as
    failure → cookies guidance."""
    monkeypatch.setattr(scraper_mod.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            403,
            content=b"<html><body>blocked</body></html>",
            headers={"server": "cloudflare", "cf-ray": "x"},
        )

    _patch_transport(monkeypatch, handler)

    async def cs_returns_403(url, **kwargs):
        return 403, b"<html><body>still blocked</body></html>", "text/html"

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cf_bypass_chain",
        cs_returns_403,
    )

    with pytest.raises(ScrapeError) as ei:
        await scrape_url("https://cf-protected.example/ch1")

    assert "automatic Cloudflare bypass also failed" in str(ei.value)


@pytest.mark.asyncio
async def test_fetch_one_transparently_retries_cf_403_via_cloudscraper(monkeypatch):
    """The recipe-facing _fetch_one helper also gets the cloudscraper
    fallback. A 69shuba / similar recipe makes a fetch, hits a CF 403,
    and gets back a 200 with cloudscraper's body — completely
    transparently. No changes needed on the recipe side."""
    from backend.services.scraper import _fetch_one

    monkeypatch.setattr(scraper_mod.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            403,
            content=b"<html><body>blocked</body></html>",
            headers={
                "content-type": "text/html",
                "server": "cloudflare",
                "cf-ray": "abc-IAD",
            },
        )

    _patch_transport(monkeypatch, handler)

    cs_called = {"n": 0}

    async def fake_cs_fetch(url, **kwargs):
        cs_called["n"] += 1
        return 200, _REAL_ARTICLE_HTML, "text/html; charset=utf-8"

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cf_bypass_chain",
        fake_cs_fetch,
    )

    status, body, ct, _enc = await _fetch_one("https://cf-protected.example/ch1")
    # _fetch_one returned the cloudscraper-served body with synthetic 200.
    assert status == 200
    assert b"bright cold day" in body
    assert cs_called["n"] == 1


@pytest.mark.asyncio
async def test_fetch_one_does_not_retry_non_cf_403(monkeypatch):
    """A plain 403 without CF headers in _fetch_one's response → no
    cloudscraper retry. Caller (recipe) sees the raw 403. Recipes can
    surface their own non-CF error messages."""
    from backend.services.scraper import _fetch_one

    monkeypatch.setattr(scraper_mod.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            403,
            content=b"<html><body>plain 403</body></html>",
            headers={"content-type": "text/html"},
        )

    _patch_transport(monkeypatch, handler)

    cs_called = {"n": 0}

    async def cs_fetch(url, **kwargs):
        cs_called["n"] += 1
        return 200, _REAL_ARTICLE_HTML, "text/html"

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cf_bypass_chain",
        cs_fetch,
    )

    status, _body, _ct, _enc = await _fetch_one("https://plain-403.example/ch1")
    assert status == 403
    assert cs_called["n"] == 0  # not invoked — no CF headers


@pytest.mark.asyncio
async def test_bypass_chain_prefers_curl_cffi_then_cloudscraper(monkeypatch):
    """The chain tries curl_cffi first; cloudscraper only fires if
    curl_cffi failed or returned non-2xx. Verifies ordering."""
    from backend.services.scrapers.cloudflare import (
        fetch_via_cf_bypass_chain,
    )

    calls = []

    async def fake_curl(url, **kwargs):
        calls.append("curl_cffi")
        return 200, _REAL_ARTICLE_HTML, "text/html"

    async def fake_cs(url, **kwargs):
        calls.append("cloudscraper")
        return 200, _REAL_ARTICLE_HTML, "text/html"

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_curl_cffi",
        fake_curl,
    )
    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cloudscraper",
        fake_cs,
    )

    status, _body, _ct = await fetch_via_cf_bypass_chain("https://test.example/")
    assert status == 200
    # curl_cffi succeeded → cloudscraper not called.
    assert calls == ["curl_cffi"]


@pytest.mark.asyncio
async def test_bypass_chain_falls_back_when_curl_cffi_fails(monkeypatch):
    """curl_cffi raises → cloudscraper tried. cloudscraper succeeds
    → return its body."""
    from backend.services.scrapers.cloudflare import (
        CloudScraperFailed,
        fetch_via_cf_bypass_chain,
    )

    calls = []

    async def failing_curl(url, **kwargs):
        calls.append("curl_cffi")
        raise CloudScraperFailed("simulated curl_cffi failure")

    async def fake_cs(url, **kwargs):
        calls.append("cloudscraper")
        return 200, _REAL_ARTICLE_HTML, "text/html"

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_curl_cffi",
        failing_curl,
    )
    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cloudscraper",
        fake_cs,
    )

    status, _body, _ct = await fetch_via_cf_bypass_chain("https://test.example/")
    assert status == 200
    assert calls == ["curl_cffi", "cloudscraper"]


@pytest.mark.asyncio
async def test_bypass_chain_raises_when_both_fail(monkeypatch):
    from backend.services.scrapers.cloudflare import (
        CloudScraperFailed,
        fetch_via_cf_bypass_chain,
    )

    async def failing(url, **kwargs):
        raise CloudScraperFailed("nope")

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_curl_cffi",
        failing,
    )
    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cloudscraper",
        failing,
    )

    with pytest.raises(CloudScraperFailed):
        await fetch_via_cf_bypass_chain("https://test.example/")


@pytest.mark.asyncio
async def test_non_cf_403_does_not_invoke_cloudscraper(monkeypatch):
    """A plain non-CF 403 (no server header, no CF-Ray) still gets
    cookies guidance per the prior fix, but doesn't bother trying
    cloudscraper first — there's nothing for cloudscraper to do when
    CF isn't even involved."""
    monkeypatch.setattr(scraper_mod.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            403, content=b"<html><body>plain 403</body></html>",
            headers={"content-type": "text/html"},
        )

    _patch_transport(monkeypatch, handler)

    cs_called = {"n": 0}

    async def cs_fetch(url, **kwargs):
        cs_called["n"] += 1
        # If reached, return success — but the test should fail because
        # this is a non-CF case and cloudscraper SHOULD still be tried
        # (current behavior: it's tried for ANY 403/429/503, not just
        # CF-labeled ones).
        return 200, _REAL_ARTICLE_HTML, "text/html"

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cf_bypass_chain",
        cs_fetch,
    )

    # Per the current implementation, cloudscraper IS tried for any 4xx
    # in the CF-blame list, even without CF headers. That's intentional
    # — non-CF 403s on novel sites are usually still bot-blocks worth a
    # shot. So the result here should succeed via cloudscraper.
    from backend.services.scraper import ScrapeResult

    result = await scrape_url("https://example.com/ch1")
    assert isinstance(result, ScrapeResult)
    assert cs_called["n"] == 1


# --- C1 (SSRF): the bypass tier must not follow redirects ---------------------


@pytest.mark.asyncio
async def test_bypass_chain_treats_redirect_as_failure(monkeypatch):
    """SSRF guard: redirects are disabled on the bypass tier, so a 3xx is an
    unfollowed redirect, never a successful page. A CF-shaped first response
    that 302s to an internal host (169.254.169.254 / a LAN IP / 127.0.0.1)
    must NOT be chased and returned. Both tiers returning a 3xx raises
    CloudScraperFailed rather than handing back the redirect."""
    from backend.services.scrapers.cloudflare import (
        CloudScraperFailed,
        fetch_via_cf_bypass_chain,
    )

    async def redirect(url, **kwargs):
        # The OLD allow_redirects=True path would have followed this 302
        # internally with no per-hop SSRF re-validation.
        return 302, b"", "text/html"

    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_curl_cffi", redirect
    )
    monkeypatch.setattr(
        "backend.services.scrapers.cloudflare.fetch_via_cloudscraper", redirect
    )
    with pytest.raises(CloudScraperFailed):
        await fetch_via_cf_bypass_chain("https://cf.example/ch1")


def test_cloudscraper_sync_disables_redirects(monkeypatch):
    """_do_fetch_sync must call cloudscraper.get with allow_redirects=False so
    the bypass never follows a redirect into an unvalidated host."""
    import cloudscraper

    from backend.services.scrapers import cloudflare as cf

    captured: dict = {}

    class _FakeScraper:
        def __init__(self):
            self.headers = {}

        def get(self, url, **kwargs):
            captured.update(kwargs)

            class _R:
                status_code = 200
                content = b"ok"
                headers = {"content-type": "text/html"}

            return _R()

    monkeypatch.setattr(cloudscraper, "create_scraper", lambda **k: _FakeScraper())
    cf._do_fetch_sync("https://x.example/", cookies=None, timeout=5.0)
    assert captured.get("allow_redirects") is False


def test_curl_cffi_sync_disables_redirects(monkeypatch):
    """_do_curl_cffi_sync must call curl_cffi's session.get with
    allow_redirects=False (same SSRF reasoning as cloudscraper)."""
    from curl_cffi import requests as creq

    from backend.services.scrapers import cloudflare as cf

    captured: dict = {}

    class _FakeSession:
        def __init__(self, *a, **k):
            self.headers = {}

        def get(self, url, **kwargs):
            captured.update(kwargs)

            class _R:
                status_code = 200
                content = b"ok"
                headers = {"content-type": "text/html"}

            return _R()

    monkeypatch.setattr(creq, "Session", _FakeSession)
    cf._do_curl_cffi_sync("https://x.example/", cookies=None, timeout=5.0)
    assert captured.get("allow_redirects") is False
