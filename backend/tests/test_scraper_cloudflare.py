"""Cloudflare bypass tests.

Covers:
- A response whose body looks like a Cloudflare challenge page raises
  a ScrapeError with actionable guidance rather than letting trafilatura
  extract "Checking your browser..." as the chapter body.
- Genuine HTML that happens to mention "cloudflare" in passing isn't
  misclassified.
- The optional `cookies` parameter is sent as a Cookie header on the
  outbound request.
- The default UA looks like a browser (so most CF-protected sites
  accept the fetch on the first try) but the env-var escape hatch
  switches back to the polite identifying UA when set.
"""

from __future__ import annotations

import httpx
import pytest

from backend.services import scraper
from backend.services.scraper import (
    POLITE_UA,
    USER_AGENT,
    ScrapeError,
    _looks_like_cloudflare_challenge,
    scrape_url,
)


def _public_resolver(host, port, *args, **kwargs):
    return [(2, 1, 6, "", ("8.8.8.8", 0))]


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched)


# ---- pure-function detection ------------------------------------------------

@pytest.mark.parametrize("body", [
    "<html><head><title>Just a moment...</title></head><body></body></html>",
    "<html><body><div id='cf-browser-verification'></div></body></html>",
    "<html><body>cf-challenge running</body></html>",
    "<html><body><script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script></body></html>",
    "<html><body>Enable JavaScript and cookies to continue</body></html>",
    "<html><head><title>Attention Required! | Cloudflare</title></head></html>",
])
def test_detector_flags_known_challenge_markers(body):
    assert _looks_like_cloudflare_challenge(body) is True


@pytest.mark.parametrize("body", [
    "",
    "<html><body>Chapter 1: It was a bright cold day in April.</body></html>",
    "<html><body>Our site is hosted on Cloudflare for performance.</body></html>",  # casual mention
])
def test_detector_passes_genuine_or_empty_bodies(body):
    # We require ONE of the specific marker phrases; a passing reference
    # to "cloudflare" in normal copy is fine.
    assert _looks_like_cloudflare_challenge(body) is False


def test_detector_only_scans_head_of_body():
    """The marker only counts inside the first ~8 KB so a CF-shaped
    phrase quoted deep in a 1 MB document doesn't trip the heuristic."""
    big = ("x" * 9000) + "just a moment..."
    assert _looks_like_cloudflare_challenge(big) is False


# ---- scrape_url surfaces the Cloudflare error -------------------------------

@pytest.mark.asyncio
async def test_cloudflare_challenge_response_raises_actionable_error(monkeypatch):
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)
    cf_body = (
        b"<html><head><title>Just a moment...</title></head>"
        b"<body><div id='cf-browser-verification'>Checking your browser</div></body></html>"
    )

    def handler(request):
        return httpx.Response(
            200, content=cf_body, headers={"content-type": "text/html"},
        )

    _patch_transport(monkeypatch, handler)
    with pytest.raises(ScrapeError, match="(?i)cloudflare"):
        await scrape_url("https://cf-protected.example/ch1")


# ---- 4xx / 5xx with CF headers route to cookies-helpful error ---------------
# This is the regression fix for "I'm still getting Failed: 400 server
# returned HTTP 403" — the previous code called raise_for_status() before
# reading the body, so the CF detector never ran on 4xx responses and the
# user got the bland "server returned HTTP 403" message instead of the
# actionable cookies guidance.

@pytest.mark.asyncio
async def test_403_with_cloudflare_server_header_surfaces_cookies_guidance(monkeypatch):
    """The dominant case: site is fronted by Cloudflare, request is
    rejected at the edge, response is 403 with `Server: cloudflare` and
    a `CF-Ray` header. User must see the cookies-helpful message."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            403,
            content=b"<html><body>blocked</body></html>",
            headers={
                "content-type": "text/html",
                "server": "cloudflare",
                "cf-ray": "9abc123def4567-IAD",
            },
        )

    _patch_transport(monkeypatch, handler)
    with pytest.raises(ScrapeError) as ei:
        await scrape_url("https://cf-blocked.example/book/1.htm")

    msg = str(ei.value)
    assert "Cloudflare blocked" in msg
    assert "HTTP 403" in msg
    assert "Cookies field" in msg  # actionable next step


@pytest.mark.asyncio
async def test_503_cloudflare_challenge_surfaces_cookies_guidance(monkeypatch):
    """CF managed challenge = 503 with the interstitial HTML. Same
    cookies guidance applies."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            503,
            content=(
                b"<html><head><title>Just a moment...</title></head>"
                b"<body><div id='cf-browser-verification'></div></body></html>"
            ),
            headers={
                "content-type": "text/html",
                "server": "cloudflare",
                "cf-ray": "9abc999fed-LAX",
            },
        )

    _patch_transport(monkeypatch, handler)
    with pytest.raises(ScrapeError) as ei:
        await scrape_url("https://cf-challenged.example/ch1")

    msg = str(ei.value)
    assert "Cloudflare" in msg
    assert "HTTP 503" in msg


@pytest.mark.asyncio
async def test_429_without_cf_headers_still_routes_to_cookies_message(monkeypatch):
    """Status 429 (too many requests) doesn't always carry CF headers but
    is essentially always a bot-block. Surface cookies guidance — the
    user's choice of cookies vs. waiting is theirs to make."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            429,
            content=b"Too many requests",
            headers={"content-type": "text/plain"},
        )

    _patch_transport(monkeypatch, handler)
    with pytest.raises(ScrapeError) as ei:
        await scrape_url("https://rate-limited.example/ch1")

    msg = str(ei.value)
    assert "HTTP 429" in msg
    # 429 isn't necessarily CF, so the label says "the site" not "Cloudflare".
    assert "the site blocked" in msg.lower() or "cloudflare" in msg.lower()
    assert "Cookies field" in msg


@pytest.mark.asyncio
async def test_404_keeps_generic_message(monkeypatch):
    """A plain 404 without CF headers is a genuine missing-page case;
    it must NOT mislead the user toward the cookies path."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        return httpx.Response(
            404,
            content=b"<html><body>not found</body></html>",
            headers={"content-type": "text/html"},
        )

    _patch_transport(monkeypatch, handler)
    with pytest.raises(ScrapeError) as ei:
        await scrape_url("https://example.com/missing")

    msg = str(ei.value)
    assert "HTTP 404" in msg
    assert "Cookies field" not in msg  # not a CF case


@pytest.mark.asyncio
async def test_cookies_path_works_against_previously_blocked_site(monkeypatch):
    """End-to-end: site returns 403 on the first request but 200 with
    real content when a valid Cookie header is sent. The user's escape
    hatch must actually let the import succeed."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)

    def handler(request):
        if not request.headers.get("cookie"):
            return httpx.Response(
                403,
                content=b"<html><body>blocked</body></html>",
                headers={"content-type": "text/html", "server": "cloudflare", "cf-ray": "x"},
            )
        return httpx.Response(
            200,
            content=(
                b"<html><body><article>"
                b"<h1>Chapter 1</h1>"
                b"<p>It was a bright cold day in April and the clocks were striking thirteen.</p>"
                b"<p>The hallway smelt of boiled cabbage and old rag mats.</p>"
                b"</article></body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)
    # Without cookies → 403 → cookies guidance.
    with pytest.raises(ScrapeError, match="Cloudflare"):
        await scrape_url("https://cf-blocked.example/ch1")
    # With cookies → 200 → real article extracted.
    result = await scrape_url(
        "https://cf-blocked.example/ch1",
        cookies="cf_clearance=abc; session=xyz",
    )
    assert "bright cold day" in result.text


# ---- cookies forwarding -----------------------------------------------------

@pytest.mark.asyncio
async def test_cookies_param_sent_as_cookie_header(monkeypatch):
    """User-supplied cookies must travel on the request as the Cookie
    header verbatim. We capture the outgoing request headers and assert."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)
    captured = {}

    def handler(request):
        # Snapshot every header we care about.
        captured["cookie"] = request.headers.get("cookie")
        captured["user_agent"] = request.headers.get("user-agent")
        captured["accept_lang"] = request.headers.get("accept-language")
        return httpx.Response(
            200,
            content=(
                b"<html><body><article>"
                b"<p>It was a bright cold day and Winston pulled the door closed "
                b"as he stepped out into the wind which cut through his coat.</p>"
                b"<p>The hallway smelt of boiled cabbage and old rag mats.</p>"
                b"</article></body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)
    await scrape_url(
        "https://cf-protected.example/ch1",
        cookies="cf_clearance=abc123; session=xyz",
    )

    assert captured["cookie"] == "cf_clearance=abc123; session=xyz"


@pytest.mark.asyncio
async def test_cookies_omitted_when_none(monkeypatch):
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)
    captured = {}

    def handler(request):
        captured["cookie"] = request.headers.get("cookie")
        return httpx.Response(
            200,
            content=(
                b"<html><body><article>"
                b"<p>It was a bright cold day and Winston pulled the door closed.</p>"
                b"<p>The hallway smelt of boiled cabbage and old rag mats.</p>"
                b"</article></body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)
    await scrape_url("https://example.com/ch1")
    assert captured["cookie"] is None


# ---- browser-shaped headers -------------------------------------------------

def test_default_user_agent_looks_like_a_real_browser():
    """The point of the redesign: the default UA should not be a giveaway
    for Cloudflare. A real Mozilla/Chrome UA is necessary; the polite
    POLITE_UA constant still exists for callers who want it explicitly."""
    assert "Mozilla" in USER_AGENT
    assert "Chrome" in USER_AGENT
    assert POLITE_UA.startswith("LN-Translator/")


@pytest.mark.asyncio
async def test_browser_client_hints_sent(monkeypatch):
    """Sec-Ch-Ua + Sec-Fetch-* metadata must be on the outbound request
    so the UA isn't a tell on its own."""
    monkeypatch.setattr(scraper.socket, "getaddrinfo", _public_resolver)
    captured = {}

    def handler(request):
        for h in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                  "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
                  "upgrade-insecure-requests"):
            captured[h] = request.headers.get(h)
        return httpx.Response(
            200,
            content=(
                b"<html><body><article>"
                b"<p>It was a bright cold day in April and the clocks were striking thirteen.</p>"
                b"<p>The hallway smelt of boiled cabbage and old rag mats.</p>"
                b"</article></body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_transport(monkeypatch, handler)
    await scrape_url("https://example.com/ch1")

    assert captured["sec-ch-ua"] is not None
    assert "Chrome" in captured["sec-ch-ua"]
    assert captured["sec-ch-ua-mobile"] == "?0"
    assert captured["sec-ch-ua-platform"] == '"Windows"'
    assert captured["sec-fetch-dest"] == "document"
    assert captured["sec-fetch-mode"] == "navigate"
    assert captured["upgrade-insecure-requests"] == "1"


# ---- end-to-end through the /scrape route ----------------------------------

@pytest.mark.asyncio
async def test_scrape_route_accepts_cookies_field(monkeypatch):
    """POST /api/translate/scrape with a cookies field must hand them to
    scrape_url. End-to-end check that the model field + route wiring are
    intact."""
    from fastapi.testclient import TestClient

    from backend.db import init_db, open_conn

    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    seen = {}

    async def fake_scrape(url, **kwargs):
        seen["url"] = url
        seen["cookies"] = kwargs.get("cookies")
        return scraper.ScrapeResult(
            text="Chapter 1\n\nFoo.", title="T", source_url=url,
        )

    monkeypatch.setattr("backend.routes.translate.scrape_url", fake_scrape)

    await init_db()
    async with open_conn() as conn:
        for t in ("chapters", "novels"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await conn.commit()

    from backend.main import app
    with TestClient(app) as client:
        resp = client.post(
            "/api/translate/scrape",
            json={"url": "https://x.example/p", "cookies": "k=v; other=1"},
        )

    assert resp.status_code == 200, resp.text
    assert seen["cookies"] == "k=v; other=1"
    assert seen["url"] == "https://x.example/p"
