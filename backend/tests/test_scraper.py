"""Phase 5 tests: URL scraping with SSRF / size / timeout guards.

Covers:
- SSRF: 127.0.0.1 / 10.x / 169.254.169.254 / ::1 / fc00:: all rejected.
- Scheme allowlist: file://, ftp://, gopher://, javascript: rejected.
- Hostname resolution failure → clean error.
- 10 MB response cap aborts mid-stream.
- Timeout fires.
- Happy path: trafilatura extracts a static HTML fixture.
- No-content HTML returns the "no extractable article" message.
- Wrong content-type (image, binary) rejected.

Network-touching tests use httpx's MockTransport so no real DNS / TCP.
SSRF tests use real DNS — the resolver is what we're checking.
"""

from __future__ import annotations

# ---- SSRF guard --------------------------------------------------------------
import ipaddress

import httpx
import pytest

from backend.services import scraper
from backend.services.scraper import (
    MAX_RESPONSE_BYTES,
    ScrapeError,
    _is_unsafe_ip,
    scrape_url,
)


@pytest.mark.parametrize("addr", [
    "127.0.0.1",
    "127.5.6.7",
    "::1",
])
def test_loopback_is_unsafe(addr):
    assert _is_unsafe_ip(ipaddress.ip_address(addr)) is not None


@pytest.mark.parametrize("addr", [
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "fc00::1",
    "fd12:3456:789a::1",
])
def test_private_is_unsafe(addr):
    assert _is_unsafe_ip(ipaddress.ip_address(addr)) is not None


@pytest.mark.parametrize("addr", [
    "169.254.169.254",  # AWS metadata
    "169.254.1.1",
    "fe80::1",
])
def test_link_local_is_unsafe(addr):
    assert _is_unsafe_ip(ipaddress.ip_address(addr)) is not None


@pytest.mark.parametrize("addr", [
    "0.0.0.0",
    "::",
])
def test_unspecified_is_unsafe(addr):
    assert _is_unsafe_ip(ipaddress.ip_address(addr)) is not None


@pytest.mark.parametrize("addr", [
    "8.8.8.8",     # Google DNS — public
    "1.1.1.1",     # Cloudflare DNS — public
    "2001:4860:4860::8888",  # Google DNS IPv6 — public
])
def test_public_ip_is_safe(addr):
    assert _is_unsafe_ip(ipaddress.ip_address(addr)) is None


# ---- scheme allowlist --------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "javascript:alert(1)",
    "data:text/html,<h1>x</h1>",
])
async def test_non_http_schemes_rejected(url):
    with pytest.raises(ScrapeError, match="scheme"):
        await scrape_url(url)


@pytest.mark.asyncio
async def test_empty_url_rejected():
    with pytest.raises(ScrapeError, match="empty"):
        await scrape_url("")


@pytest.mark.asyncio
async def test_url_without_host_rejected():
    with pytest.raises(ScrapeError, match="no hostname"):
        await scrape_url("http:///path-only")


# ---- SSRF integration: mock the resolver -------------------------------------

@pytest.mark.asyncio
async def test_loopback_url_rejected(monkeypatch):
    """A URL whose hostname resolves to 127.0.0.1 must fail at the SSRF
    guard, BEFORE any HTTP request goes out."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('127.0.0.1', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ScrapeError, match="loopback"):
        await scrape_url("http://attacker-controlled-dns.example/")


@pytest.mark.asyncio
async def test_aws_metadata_url_rejected(monkeypatch):
    """The canonical SSRF target (169.254.169.254) must be rejected."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('169.254.169.254', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ScrapeError, match="link-local"):
        await scrape_url("http://metadata.example/latest/")


@pytest.mark.asyncio
async def test_rfc1918_url_rejected(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('10.0.0.5', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ScrapeError, match="private"):
        await scrape_url("http://internal-app.example/")


@pytest.mark.asyncio
async def test_dns_failure_returns_clean_error(monkeypatch):
    import socket as _socket
    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise _socket.gaierror("nodename nor servname provided")
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ScrapeError, match="could not resolve"):
        await scrape_url("http://this-domain-definitely-does-not-exist.example/")


# ---- response size cap -------------------------------------------------------

@pytest.mark.asyncio
async def test_response_size_cap_aborts(monkeypatch):
    """A server returning > 10 MB must fail mid-stream with a clean
    ScrapeError, not buffer the whole body."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    # Mock transport returns a body just over the cap.
    oversized = b"a" * (MAX_RESPONSE_BYTES + 100)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=oversized,
            headers={"content-type": "text/html"},
        )
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="exceeded.*MB cap"):
        await scrape_url("http://big.example/")


# ---- happy path --------------------------------------------------------------

_SAMPLE_HTML = """<!DOCTYPE html>
<html><head>
<title>Chapter 1: The Beginning - Some Novel Site</title>
<meta name="description" content="A sample chapter">
</head><body>
<header><nav>Home | Library</nav></header>
<article>
<h1>Chapter 1: The Beginning</h1>
<p>It was a bright cold day in April, and the clocks were striking thirteen.
Winston Smith, his chin nuzzled into his breast in an effort to escape the
vile wind, slipped quickly through the glass doors of Victory Mansions, though
not quickly enough to prevent a swirl of gritty dust from entering along with
him.</p>
<p>The hallway smelt of boiled cabbage and old rag mats. At one end of it a
coloured poster, too large for indoor display, had been tacked to the wall.
It depicted simply an enormous face, more than a metre wide.</p>
<p>It was no use trying the lift. Even at the best of times it was seldom
working, and at present the electric current was cut off during daylight
hours.</p>
</article>
<footer>Copyright Some Novel Site</footer>
</body></html>"""


@pytest.mark.asyncio
async def test_happy_path_extracts_article(monkeypatch):
    """trafilatura should pull the <article> body cleanly, dropping the
    nav header and footer."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    result = await scrape_url("http://example-novel-site.com/ch1")
    # Body content should land.
    assert "Winston Smith" in result.text
    assert "Victory Mansions" in result.text
    # Nav / footer should be dropped (trafilatura's job).
    assert "Home | Library" not in result.text
    # Title is the trafilatura-extracted version (may include the site
    # suffix; check it at least contains the chapter title).
    assert "Chapter 1: The Beginning" in result.title
    assert result.source_url == "http://example-novel-site.com/ch1"


# ---- no-extractable-content --------------------------------------------------

@pytest.mark.asyncio
async def test_no_article_content_returns_clean_error(monkeypatch):
    """A page with no extractable article (e.g. just nav / boilerplate)
    must return 'no extractable article content' — not silent success."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    # `favor_recall=True` means trafilatura is aggressive about
    # extracting anything that LOOKS like prose. A truly empty body is
    # the test case for "page has no content at all" — nav-only pages
    # with link text would get extracted as content because the recall
    # setting prioritizes keeping ambiguous text over dropping it.
    bare = b"""<!DOCTYPE html><html><head><title>x</title></head><body></body></html>"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=bare, headers={"content-type": "text/html"},
        )
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="no extractable article"):
        await scrape_url("http://bare.example/")


# ---- wrong content-type ------------------------------------------------------

@pytest.mark.asyncio
async def test_image_content_type_rejected(monkeypatch):
    """Server returning image/jpeg or similar binary should be rejected
    cleanly, not fed to trafilatura."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\xff\xd8\xff\xe0",  # JPEG magic
            headers={"content-type": "image/jpeg"},
        )
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="not an HTML page"):
        await scrape_url("http://image.example/photo.jpg")


# ---- HTTP error status ------------------------------------------------------

@pytest.mark.asyncio
async def test_http_404_returns_clean_error(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="HTTP 404"):
        await scrape_url("http://gone.example/")


# ---- redirect SSRF -----------------------------------------------------------

@pytest.mark.asyncio
async def test_redirect_to_private_ip_rejected(monkeypatch):
    """A public URL that 302s to an internal IP must be REJECTED before
    httpx dials the loopback. Regression for the auto-redirect SSRF
    bypass — with follow_redirects=True the dial happened before user
    code could see the redirect target."""
    # Public IP for the initial URL, loopback for the redirect target.
    resolutions = {
        "public-site.example": [(2, 1, 6, '', ('8.8.8.8', 0))],
        "evil-redirect-target.example": [(2, 1, 6, '', ('127.0.0.1', 0))],
    }
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return resolutions.get(host, [(2, 1, 6, '', ('8.8.8.8', 0))])
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        if "public-site.example" in str(request.url):
            return httpx.Response(
                302,
                headers={"location": "http://evil-redirect-target.example/leak"},
            )
        # If we ever reach the loopback request, the SSRF guard failed.
        return httpx.Response(200, content=b"<html><body>secret</body></html>",
                              headers={"content-type": "text/html"})
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="loopback"):
        await scrape_url("http://public-site.example/")


@pytest.mark.asyncio
async def test_redirect_to_aws_metadata_rejected(monkeypatch):
    """The canonical exfil target — 169.254.169.254 reached via 302 —
    must trip the link-local check on the redirect hop."""
    resolutions = {
        "innocent.example": [(2, 1, 6, '', ('8.8.8.8', 0))],
        "metadata-grab.example": [(2, 1, 6, '', ('169.254.169.254', 0))],
    }
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return resolutions.get(host, [(2, 1, 6, '', ('8.8.8.8', 0))])
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        if "innocent.example" in str(request.url):
            return httpx.Response(
                301,
                headers={"location": "http://metadata-grab.example/latest/meta-data/"},
            )
        return httpx.Response(200, content=b"secret token")
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="link-local"):
        await scrape_url("http://innocent.example/")


@pytest.mark.asyncio
async def test_redirect_to_non_http_scheme_rejected(monkeypatch):
    """A 302 with `Location: file:///etc/passwd` must hit the scheme
    allowlist on the next hop, not be followed."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "file:///etc/passwd"},
        )
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="scheme"):
        await scrape_url("http://redirector.example/")


@pytest.mark.asyncio
async def test_redirect_chain_limit_enforced(monkeypatch):
    """More than MAX_REDIRECTS hops → clean ScrapeError, no infinite
    loop. Guards against a server that 302s endlessly."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    hop_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        hop_count["n"] += 1
        return httpx.Response(
            302,
            headers={"location": f"http://redirector.example/hop/{hop_count['n']}"},
        )
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="too many redirects"):
        await scrape_url("http://redirector.example/start")


@pytest.mark.asyncio
async def test_redirect_to_public_target_follows(monkeypatch):
    """Sanity: a public-to-public redirect must NOT trip the SSRF guard
    — sites use 301 to canonicalize URLs routinely."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/old-url" in str(request.url):
            return httpx.Response(
                301, headers={"location": "http://canonical.example/new-url"},
            )
        return httpx.Response(
            200,
            content=_SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    result = await scrape_url("http://canonical.example/old-url")
    assert "Winston Smith" in result.text


# ---- timeout -----------------------------------------------------------------

# ---- route: source_url persistence ------------------------------------------

@pytest.mark.asyncio
async def test_scrape_route_persists_source_type_and_source_url(monkeypatch):
    """POST /api/translate/scrape must store BOTH source_type='url' AND
    the actual URL in novels.source_url. Earlier route only set the type,
    leaving the URL invisible in the library."""
    from fastapi.testclient import TestClient

    from backend.db import init_db, open_conn

    # Stub the lifespan so TestClient doesn't probe a real provider /
    # drain the queue.
    async def _no_probe(default_provider):
        return None
    async def _no_drain():
        return None
    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    # Stub the scraper so we don't actually fetch.
    async def fake_scrape(url, **kwargs):
        return scraper.ScrapeResult(
            text="Chapter 1: The Beginning\n\nIt was a bright cold day.",
            title="The Beginning",
            source_url=url,
        )
    monkeypatch.setattr("backend.routes.translate.scrape_url", fake_scrape)

    await init_db()
    # Clean slate.
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
            json={"url": "https://novel-site.example/chapter/1"},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    novel_id = payload["novel_id"]
    assert payload["scraped_url"] == "https://novel-site.example/chapter/1"

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT source_type, source_url FROM novels WHERE id = ?",
            (novel_id,),
        )
        row = await cur.fetchone()
    assert row["source_type"] == "url"
    assert row["source_url"] == "https://novel-site.example/chapter/1"


# ---- timeout -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_clean_error(monkeypatch):
    """A server that takes longer than the timeout must produce a clean
    'timed out' ScrapeError, not an httpx exception leak."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, '', ('8.8.8.8', 0))]
    monkeypatch.setattr(scraper.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")
    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient
    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)
    monkeypatch.setattr(scraper.httpx, "AsyncClient", patched_client)

    with pytest.raises(ScrapeError, match="timed out"):
        await scrape_url("http://slow.example/", timeout=0.1)
