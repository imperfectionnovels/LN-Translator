"""URL scraping for the importer.

`scrape_url(url)` fetches a web page, extracts the main article body via
trafilatura, and returns `(text, title, source_url)` suitable for the
existing parser → chapter-creation pipeline. The text shape is exactly
what `parse_chapters` expects — paragraphs separated by `\\n\\n`, chapter
heading conventions (第N章 / Chapter N) preserved if present in the source.

Security hardening (mandatory):

- **SSRF guard**: the hostname is resolved BEFORE the fetch and every
  resulting IP is checked against private / loopback / link-local /
  reserved / multicast ranges. A URL whose hostname resolves to an
  internal address is rejected with `ScrapeError`. This protects against
  the common attack — URL literally pointing at an internal service
  (e.g. `http://169.254.169.254/` for cloud-metadata theft, or
  `http://10.x.x.x/`).

- **Manual redirect validation**: httpx auto-redirects are DISABLED.
  Each `Location` header is parsed, scheme-checked, and SSRF-validated
  before the next hop is followed. Without this, a public URL could
  302 to `http://127.0.0.1:6379/` and httpx would dial the loopback
  before user code saw the new host.

- **Scheme allowlist**: only `http://` and `https://`. `file://`, `ftp://`,
  `gopher://`, etc. are rejected (initial URL AND every redirect target).

- **Response size cap**: 10 MB streamed; aborts mid-stream on overrun.
  Stops a malicious page from exhausting memory.

- **Wall-clock timeout**: 15 s for the entire fetch (connect + redirects +
  streaming the body), enforced via `asyncio.timeout`. httpx's per-op
  timeout would not cap a slow-drip server sending small chunks each
  under the read timeout; the asyncio wrapper does.

- **Identifying User-Agent**: requests advertise as `LN-Translator/<ver>`
  so a site operator can see who's hitting them and block / rate-limit
  cooperatively. Not a forged browser UA.

- **No credential follow**: cookies / auth headers are NOT forwarded
  across redirect hops (manual redirect handling means we build each hop
  from scratch).

Residual risk — DNS rebinding:
  The pre-fetch IP validation uses `socket.getaddrinfo`, but httpx
  performs its own DNS lookup when it opens the socket. A DNS-rebinding
  attacker who returns a public IP at validation time and a private IP
  at fetch time can bypass the guard. Fully closing this requires DNS
  pinning via a custom httpx transport (resolve once, dial the validated
  IP, send `Host: <hostname>` and SNI for the original hostname). Not
  yet implemented. Acceptable risk given the target threat model (local
  single-user desktop app), but worth tightening if the app is ever
  exposed publicly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Awaitable, Callable, TYPE_CHECKING, Union

import aiosqlite
import httpx
import trafilatura

if TYPE_CHECKING:
    # Import only for the type-annotation forward-reference. The runtime
    # path imports the same class lazily inside scrape_url so module
    # import order stays straight (scrapers/__init__.py imports this
    # module).
    from backend.services.scrapers.base import RecipeResult  # noqa: F401

from backend.services.covers import MAX_COVER_BYTES, sniff_image_ext

logger = logging.getLogger(__name__)


# ---- limits / config -------------------------------------------------------
# 10 MB hard cap on response size. Any reasonable novel chapter page is
# orders of magnitude smaller; an HTML response larger than this is almost
# certainly either a binary blob, a misrouted download, or an attempt to
# starve the server.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# Combined connect + read + total timeout. A non-malicious page returns
# inside 5 s on average; 15 s gives slow shared hosting a chance without
# letting a hung server tie up a worker.
DEFAULT_TIMEOUT_SECONDS = 15.0

# Identifying UA reserved for tests / internal uses that explicitly want
# the polite citizen profile. Kept as a constant so test_scraper.py can
# still assert against a stable string when needed.
POLITE_UA = "LN-Translator/0.1 (+https://github.com/ImperfectionNovels/LN-Translator)"

# Originally we shipped a transparent "LN-Translator/0.1" UA so site
# operators could see who was hitting them and rate-limit cooperatively.
# That's polite — and it gets reflexively blocked by Cloudflare and
# every other bot-detection system in front of the novel sites this app
# actually scrapes (69shuba, the various fan-translation forums, etc.).
# So we switched to a current Chrome-on-Windows UA. The downside is real:
# we no longer self-identify to operators. The upside is the user can
# actually import the chapters they paid for / are translating for fun.
# If the polite mode ever comes back as an explicit opt-in, set the
# `LN_TRANSLATOR_POLITE_UA=1` env var to swap back to POLITE_UA.
USER_AGENT = (
    POLITE_UA if os.getenv("LN_TRANSLATOR_POLITE_UA", "").strip() == "1"
    else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Sec-Ch-Ua "client hints" + Sec-Fetch-* metadata that real Chrome sends.
# Cloudflare and other bot detectors check for these — sending the UA
# without the matching client hints is a tell. The values pair with
# UA's Chrome 130 above; bump together if the UA gets refreshed.
_BROWSER_HEADERS = {
    "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# Hard cap on redirect chain length. httpx's default is 20; for article
# fetching, more than a handful of hops is a sign of misconfiguration or
# an attempt to wear down the SSRF guard with many small hops.
MAX_REDIRECTS = 5


# Cloudflare interstitial markers. These are pieces of HTML that appear
# in CF's "Just a moment..." / "Checking your browser" challenge pages
# (managed challenge, JS challenge). We detect them after the fetch and
# turn the success-looking 200 into a clean ScrapeError telling the user
# what to do next — far more useful than letting trafilatura silently
# extract the boilerplate "checking your browser" sentence and shoving
# that into a chapter. The check tolerates Cloudflare changing minor
# string details over time by requiring any one of these markers, and
# uses lowercase substring matching to skip case-sensitivity issues.
_CLOUDFLARE_MARKERS = (
    "just a moment...",
    "cf-browser-verification",
    "cf-challenge",
    "challenge-platform",
    "/cdn-cgi/challenge",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
)


# ---- public types ----------------------------------------------------------

@dataclass(frozen=True)
class ScrapeResult:
    text: str
    title: str
    source_url: str
    # Best-effort cover image scraped from og:image / twitter:image /
    # image_src on the same page. Both fields are None when no candidate
    # meta tag is present OR when the candidate fetch fails for any reason
    # (SSRF block, oversize, wrong content-type, timeout). The route layer
    # treats a None cover as "import the text without a cover" — cover
    # fetch failure must never break the primary import flow.
    cover_bytes: bytes | None = None
    cover_ext: str | None = None


class ScrapeError(Exception):
    """User-facing scraping failure (bad URL, blocked target, no extractable
    content, response too large, timeout). The HTTP route turns this into a
    400/413/504 with the message body as `detail`.

    `error_kind` is a coarse classifier the frontend uses to render
    differentiated recovery UI per failure mode (CF block opens the
    cookies-paste tutorial; auth surfaces "this site requires login";
    timeout offers retry with a longer cap; etc.). Defaults to 'unknown'
    when callers raise without specifying — UI falls back to the generic
    error display in that case.
    """

    def __init__(self, message: str, *, error_kind: str = "unknown") -> None:
        super().__init__(message)
        self.error_kind = error_kind


# ---- SSRF guard ------------------------------------------------------------

def _is_unsafe_ip(ip: ipaddress._BaseAddress) -> str | None:
    """Return a reason string when `ip` is in a range we refuse to fetch
    from, or None when it's a safe public address.

    The check is conservative — a single category (private / loopback /
    link-local / reserved / multicast / unspecified) is enough to refuse.
    `is_global` would catch all of these, but the per-category reason makes
    the error message useful.
    """
    if ip.is_loopback:
        return "loopback (127/8, ::1)"
    if ip.is_link_local:
        # IPv4 169.254/16, IPv6 fe80::/10. The metadata endpoint
        # 169.254.169.254 is the canonical SSRF target.
        return "link-local (169.254/16, fe80::/10)"
    if ip.is_private:
        # IPv4 RFC1918 (10/8, 172.16/12, 192.168/16), IPv6 unique-local fc00::/7.
        return "private (RFC1918, fc00::/7)"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified (0.0.0.0, ::)"
    # Some IPv6 categories aren't covered by the above — check is_global
    # as a backstop.
    if hasattr(ip, "is_global") and not ip.is_global:
        return "not globally routable"
    return None


async def _resolve_and_validate(hostname: str) -> None:
    """Resolve `hostname` via DNS and ensure every returned IP is safe to
    fetch from. Raises ScrapeError on a blocked address or on DNS failure.

    Resolution runs once via `socket.getaddrinfo` (off the event loop via
    `asyncio.to_thread`). This is a NECESSARY-but-not-sufficient SSRF
    check: httpx performs its own DNS lookup later when opening the
    socket, so a DNS-rebinding attacker who returns a public IP at this
    validation and a private IP at fetch time can still bypass the guard.
    Fully closing rebinding would require DNS pinning via a custom
    transport (resolve once, force httpx to dial the validated IP with a
    Host header for the original hostname). That's a substantial change
    not yet implemented — residual risk documented in the module docstring.

    What this DOES close: the common "URL literally points at a private
    IP" attack (http://127.0.0.1/, http://169.254.169.254/, http://10.x/)
    AND, combined with the manual redirect handling in `scrape_url`,
    every redirect hop in the chain.
    """
    try:
        info = await asyncio.to_thread(
            socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as e:
        raise ScrapeError(f"could not resolve hostname {hostname!r}: {e}") from e

    if not info:
        raise ScrapeError(f"hostname {hostname!r} resolved to no addresses")

    for family, _, _, _, sockaddr in info:
        # sockaddr is (host, port[, flowinfo, scope_id]) — only the host
        # matters for the address check.
        addr_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            # Skip non-numeric (shouldn't happen from getaddrinfo, but
            # defensive).
            continue
        reason = _is_unsafe_ip(ip)
        if reason is not None:
            raise ScrapeError(
                f"refusing to fetch from {hostname!r}: resolved IP "
                f"{addr_str} is {reason}. The scraper rejects internal "
                "addresses to prevent SSRF.",
                error_kind="ssrf",
            )


# ---- main entry point ------------------------------------------------------

def _check_url_safety(url: str) -> urllib.parse.ParseResult:
    """Parse, scheme-check, and hostname-validate a single URL. Returns
    the parsed result on success; raises ScrapeError on bad scheme or
    missing host. Caller is responsible for the async SSRF resolve.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ScrapeError(
            f"unsupported URL scheme {parsed.scheme!r}; only http(s) is "
            "allowed (file://, ftp://, gopher://, javascript:, data: all "
            "rejected)."
        )
    if not parsed.hostname:
        raise ScrapeError("URL has no hostname")
    return parsed


async def _fetch_with_manual_redirects(
    client: httpx.AsyncClient, initial_url: str,
) -> httpx.Response:
    """Issue GET, follow up to MAX_REDIRECTS hops MANUALLY, SSRF-validating
    each hop's hostname before dialing it. Returns the final streaming
    Response (caller is responsible for iter+close).

    The httpx `follow_redirects=True` path is unsafe because it dials the
    redirect target BEFORE user code can inspect the host. We instead get
    a single response per call with redirects disabled, inspect any 3xx
    Location, validate, and loop.
    """
    current = initial_url
    for hop in range(MAX_REDIRECTS + 1):
        # Build a streaming request — closed below if a redirect comes.
        request = client.build_request("GET", current)
        resp = await client.send(request, stream=True)
        if not (300 <= resp.status_code < 400) or "location" not in resp.headers:
            # Terminal response (or redirect without Location — treat as
            # terminal so the caller sees the status).
            return resp
        # Found a redirect. Close this hop's stream cleanly, then
        # validate the next hop and loop.
        await resp.aclose()
        if hop >= MAX_REDIRECTS:
            raise ScrapeError(
                f"too many redirects (more than {MAX_REDIRECTS}); refusing "
                "to follow further."
            )
        location = resp.headers["location"]
        # Resolve relative redirects against the current URL.
        next_url = str(httpx.URL(current).join(location))
        next_parsed = _check_url_safety(next_url)
        await _resolve_and_validate(next_parsed.hostname)
        current = next_url
    # Loop exit without a terminal response — shouldn't reach here given
    # the explicit `hop >= MAX_REDIRECTS` raise above, but defensive:
    raise ScrapeError("redirect loop exited without a terminal response")


# Meta-tag patterns for the cover-image hunt. Listed in priority order:
# og:image is the de-facto standard for social cards (essentially every
# fan-translation blog / Wattpad-style site sets it); twitter:image is the
# common second-best; image_src is a legacy Pinterest convention. We stop at
# the first match. Patterns tolerate attribute order swapping (property
# before content vs content before property) and single OR double quotes.
_COVER_META_PATTERNS = (
    re.compile(
        r"""<meta[^>]+(?:property|name)\s*=\s*['"]og:image(?::secure_url)?['"][^>]+content\s*=\s*['"]([^'"]+)['"]""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""<meta[^>]+content\s*=\s*['"]([^'"]+)['"][^>]+(?:property|name)\s*=\s*['"]og:image(?::secure_url)?['"]""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""<meta[^>]+name\s*=\s*['"]twitter:image['"][^>]+content\s*=\s*['"]([^'"]+)['"]""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""<meta[^>]+content\s*=\s*['"]([^'"]+)['"][^>]+name\s*=\s*['"]twitter:image['"]""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""<link[^>]+rel\s*=\s*['"]image_src['"][^>]+href\s*=\s*['"]([^'"]+)['"]""",
        re.IGNORECASE,
    ),
)


def _looks_like_cloudflare_challenge(html_text: str) -> bool:
    """Heuristic: is this HTML body a Cloudflare interstitial / challenge
    page rather than the actual article? Returns True iff any of the
    known marker strings appears in the (lowercased) body. False
    positives are unlikely — these phrases don't appear in genuine
    Chinese novel chapters — but we err on the side of False to avoid
    breaking imports for sites that happen to mention "cloudflare" in
    their normal copy. The first ~8 KB is sufficient; CF puts the
    challenge content near the top of the page.
    """
    if not html_text:
        return False
    head = html_text[:8192].lower()
    return any(marker in head for marker in _CLOUDFLARE_MARKERS)


def _extract_cover_url(html_text: str, base_url: str) -> str | None:
    """Find the highest-priority cover URL in the page's meta tags, resolved
    against the page URL. Returns None when nothing matches. Cheap regex pass
    — we already have the HTML in memory and a full parse isn't worth the
    cost for what's a best-effort feature."""
    for pat in _COVER_META_PATTERNS:
        m = pat.search(html_text)
        if not m:
            continue
        candidate = (m.group(1) or "").strip()
        if not candidate:
            continue
        # Resolve relative URLs against the page URL. httpx.URL.join handles
        # absolute, protocol-relative, and root-relative cases.
        try:
            return str(httpx.URL(base_url).join(candidate))
        except (httpx.InvalidURL, ValueError):
            continue
    return None


# Image content-types we accept. Magic-byte sniffing is the real check
# downstream (sniff_image_ext); this is a cheap first filter so we don't
# bother fetching a 50 MB PDF that some misconfigured site put in og:image.
_ACCEPTABLE_IMAGE_CT_PREFIXES = ("image/",)


async def _fetch_cover_image(
    cover_url: str, *, timeout: float,
) -> tuple[bytes, str] | None:
    """Fetch a cover image with the SAME hardening the text fetch uses:
    scheme allowlist, SSRF guard, manual redirect validation, response-size
    cap, wall-clock timeout. Returns (bytes, ext) on success, None on any
    failure (logged, never raised). The 'never raised' invariant is the
    point: a cover-fetch failure must not break the surrounding import.

    Size cap matches the uploaded-cover route (MAX_COVER_BYTES) — anything
    larger gets rejected by write_cover_for_novel anyway, so we may as well
    cap mid-stream and free the bytes.
    """
    try:
        parsed = _check_url_safety(cover_url)
        await _resolve_and_validate(parsed.hostname)

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "image/*",
        }

        body_bytes = bytearray()
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,  # CRITICAL: same as the text fetch
                headers=headers,
            ) as client:
                resp = await _fetch_with_manual_redirects(client, cover_url)
                try:
                    resp.raise_for_status()
                    ct = (resp.headers.get("content-type") or "").lower()
                    if not any(ct.startswith(p) for p in _ACCEPTABLE_IMAGE_CT_PREFIXES):
                        logger.info(
                            "cover scrape: %s returned non-image content-type %r; skipping",
                            cover_url, ct,
                        )
                        return None
                    async for chunk in resp.aiter_bytes(chunk_size=16384):
                        body_bytes.extend(chunk)
                        if len(body_bytes) > MAX_COVER_BYTES:
                            logger.info(
                                "cover scrape: %s exceeded %d-byte cap; skipping",
                                cover_url, MAX_COVER_BYTES,
                            )
                            return None
                finally:
                    await resp.aclose()
    except ScrapeError as e:
        # ScrapeError from _check_url_safety / _resolve_and_validate / the
        # manual-redirect helper means the cover URL pointed at something
        # we refuse to fetch (private IP, bad scheme, too many redirects).
        # Logged at INFO because user input drove this, not a bug.
        logger.info("cover scrape rejected for %r: %s", cover_url, e)
        return None
    except (TimeoutError, httpx.TimeoutException):
        logger.info("cover scrape timed out for %r", cover_url)
        return None
    except httpx.HTTPStatusError as e:
        logger.info(
            "cover scrape got HTTP %d for %r", e.response.status_code, cover_url,
        )
        return None
    except httpx.RequestError as e:
        logger.info("cover scrape network error for %r: %s", cover_url, e)
        return None
    except Exception:
        # Defense-in-depth. The whole point of this helper is "covers are
        # best-effort"; an unhandled exception here would break the import.
        logger.exception("cover scrape unexpectedly failed for %r", cover_url)
        return None

    if not body_bytes:
        return None
    ext = sniff_image_ext(bytes(body_bytes[:16]))
    if ext is None:
        logger.info(
            "cover scrape: %s did not match a supported image format", cover_url,
        )
        return None
    return bytes(body_bytes), ext


async def _try_cf_bypass_fallback(
    url: str, *, cookies: str | None, timeout: float,
    headers: dict | None = None,
) -> tuple[bytes, str, str] | None:
    """Two-tier retry chain after a CF-shaped 4xx/5xx from the primary
    httpx fetch:
      1. curl_cffi (Chrome TLS impersonation) — beats CF's TLS
         fingerprint check, which is what blocks Python's stdlib SSL.
      2. cloudscraper (legacy JS challenge solver) — beats older v1/v2
         JS challenges that curl_cffi passes through unsolved.

    Returns ``(body, content_type, encoding)`` on success of either
    tier, ``None`` on both failing (caller falls through to the cookies-
    helpful error message).

    SSRF: the caller already validated the URL hostname before reaching
    here, so the bypass libraries are safe to invoke against it.
    """
    try:
        from backend.services.scrapers.cloudflare import (  # noqa: PLC0415
            CloudScraperFailed,
            fetch_via_cf_bypass_chain,
        )
    except Exception as e:
        logger.debug("cf bypass chain not importable: %s", e)
        return None

    try:
        status, body, content_type = await fetch_via_cf_bypass_chain(
            url, headers=headers, cookies=cookies, timeout=max(timeout, 25.0),
        )
    except CloudScraperFailed as e:
        logger.info("cf bypass chain failed for %s: %s", url, e)
        return None

    if status >= 400:
        logger.info(
            "cloudscraper fallback also got HTTP %d for %s", status, url,
        )
        return None

    # Best-effort encoding hint from the content-type charset; falls
    # back to utf-8 (cloudscraper doesn't expose response.encoding the
    # way httpx does).
    encoding = "utf-8"
    m = re.search(r"charset=([\w\-]+)", content_type)
    if m:
        encoding = m.group(1)

    return body, content_type, encoding


async def _fetch_one(
    url: str,
    *,
    headers: dict | None = None,
    cookies: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[int, bytes, str, str]:
    """Recipe-facing fetcher. SSRF-validates the URL, opens an httpx
    client, runs the manual-redirect chain, returns
    ``(status, body, content_type, encoding_hint)``.

    Site recipes call this for every fetch instead of constructing their
    own httpx / requests client so they inherit:
      - The scheme allowlist + SSRF guard.
      - Manual redirect validation (each hop re-SSRF-checked).
      - Body-size cap mid-stream.
      - Wall-clock timeout via asyncio.timeout.

    Headers override the default browser-shaped set on a per-recipe
    basis. Recipes that prefer Firefox over Chrome (e.g. 69shuba) pass
    their own headers dict; non-recipe scrapes use the defaults below.
    """
    parsed = _check_url_safety(url)
    await _resolve_and_validate(parsed.hostname)

    # Default headers identical to scrape_url's primary path so a recipe
    # that just wants browser-shaped requests gets them. Recipe can
    # override any key.
    referer = f"{parsed.scheme}://{parsed.hostname}/"
    effective_headers: dict = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        **_BROWSER_HEADERS,
    }
    if headers:
        effective_headers.update(headers)
    if cookies:
        effective_headers["Cookie"] = cookies.strip()

    body_bytes = bytearray()
    content_type = ""
    encoding_hint = "utf-8"
    status_code = 0
    is_cloudflare = False
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
                headers=effective_headers,
            ) as client:
                resp = await _fetch_with_manual_redirects(client, url)
                try:
                    status_code = resp.status_code
                    server_header = (resp.headers.get("server") or "").lower()
                    cf_ray = "cf-ray" in resp.headers
                    is_cloudflare = (
                        server_header == "cloudflare" or cf_ray
                        or bool(resp.headers.get("cf-mitigated"))
                    )
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        body_bytes.extend(chunk)
                        if len(body_bytes) > max_bytes:
                            raise ScrapeError(
                                f"recipe fetch exceeded {max_bytes // (1024 * 1024)} MB cap"
                            )
                    content_type = (resp.headers.get("content-type") or "").lower()
                    encoding_hint = resp.encoding or "utf-8"
                finally:
                    await resp.aclose()
    except TimeoutError as e:
        raise ScrapeError(
            f"recipe fetch timed out after {timeout:.0f}s for {url!r}",
            error_kind="timeout",
        ) from e
    except httpx.TimeoutException as e:
        raise ScrapeError(
            f"recipe fetch timed out after {timeout:.0f}s for {url!r}",
            error_kind="timeout",
        ) from e
    except httpx.RequestError as e:
        raise ScrapeError(
            f"recipe fetch network error for {url!r}: {e}"
        ) from e

    # Transparent cloudscraper retry on a CF-shaped 403/503/429. This
    # mirrors the fallback scrape_url's main path has — the recipe path
    # needs the same handling because 69shuba (and any future CF-fronted
    # site) hits 403 on the first Firefox-shaped request from some
    # IP / TLS combinations. Recipe sees the bypass transparently:
    # a 200 with cloudscraper's body, no awareness of the retry.
    if status_code in (403, 503, 429) and is_cloudflare:
        recovered = await _try_cf_bypass_fallback(
            url, cookies=cookies, timeout=timeout,
            headers=effective_headers,
        )
        if recovered is not None:
            body, ct, enc = recovered
            logger.info(
                "_fetch_one: cloudscraper bypass succeeded for %s (%d bytes)",
                url, len(body),
            )
            return 200, body, ct, enc

    return status_code, bytes(body_bytes), content_type, encoding_hint


async def scrape_url(
    url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cookies: str | None = None,
    conn: aiosqlite.Connection | None = None,
    progress: "Callable[[str, int, int], Awaitable[None]] | None" = None,
) -> "Union[ScrapeResult, 'RecipeResult']":
    """Fetch `url`, extract main article text, return ScrapeResult.

    `cookies` is an optional Cookie-header string ("name1=value1; name2=value2")
    that the user pasted from their browser's developer tools. This is the
    escape hatch for sites where the browser-shaped headers alone don't
    get past Cloudflare's challenge — once the user solves the challenge
    in their browser, they can copy the resulting cookies and let the
    scraper reuse the session.

    Raises ScrapeError for any user-actionable failure (bad scheme, blocked
    address, timeout, oversize response, no extractable content, HTTP
    error, Cloudflare challenge interstitial). The route layer turns these
    into 4xx responses with the message body as the user-visible detail.

    The whole fetch+stream phase is wrapped in `asyncio.timeout(timeout)`
    so the 15s cap is a true wall-clock deadline, not just a per-op limit.

    Recipe dispatch: when ``conn`` is provided AND a site recipe matches
    the URL's hostname (see ``backend/services/scrapers``), the recipe
    owns the entire import — fetches the index, walks the chapter list,
    creates the novel + chapters in one transaction, optionally writes
    a cover, and returns a ``RecipeResult``. The route layer
    distinguishes RecipeResult from ScrapeResult via isinstance and
    skips its own ``_create_novel_and_chapters`` call when it sees a
    RecipeResult. Callers that don't pass ``conn`` (smoke tests, direct
    callers) get the legacy ScrapeResult path even when a recipe could
    have handled the URL.
    """
    url = (url or "").strip()
    if not url:
        raise ScrapeError("URL is empty")

    parsed = _check_url_safety(url)
    # SSRF guard for the initial URL. Redirects are validated per-hop
    # inside _fetch_with_manual_redirects.
    await _resolve_and_validate(parsed.hostname)

    # Recipe dispatch: per-site code paths (encoding, URL transforms,
    # chapter-list crawl) for known hosts. Recipes own the full import
    # — they call `_atomic_create_novel` themselves and return a
    # RecipeResult. Only fires when the caller passed ``conn`` (without
    # it the recipe can't write to the DB).
    if conn is not None:
        from backend.services.scrapers import (
            dispatch,  # noqa: PLC0415 — avoid circular at module load
        )
        recipe = dispatch(parsed.hostname)
        if recipe is not None:
            logger.info(
                "scrape_url: dispatching %s to recipe %r", url, recipe.name,
            )
            return await recipe.scrape(
                url, conn, cookies=cookies, fetch=_fetch_one,
                progress=progress,
            )

    # Build a request that looks like a real Chrome navigation. The
    # Accept-Language puts zh-CN first because the overwhelming majority
    # of URLs the user feeds the scraper are Chinese novel sites; an
    # English-first locale header was getting some sites to redirect to
    # their non-existent English mirror. The Referer is set to the host
    # root so requests for /book/N.htm look like a click from /, which
    # matches how a user would actually navigate the site.
    referer = f"{parsed.scheme}://{parsed.hostname}/"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        **_BROWSER_HEADERS,
    }
    if cookies:
        # Pass through verbatim — httpx Cookie header. We don't try to
        # parse / validate because the user is pasting from a browser
        # devtools "Cookie" field and any normalization would just
        # mangle session tokens.
        headers["Cookie"] = cookies.strip()

    body_bytes = bytearray()
    content_type = ""
    resp_encoding = "utf-8"
    status_code = 0
    # Snapshot Cloudflare-shaped response headers BEFORE the response
    # closes so the error path can use them to decide whether a 4xx /
    # 5xx is CF blocking us (route to cookies guidance) vs. a generic
    # server error (route to the bland "URL wrong / login required"
    # message). CF stamps `Server: cloudflare` on every response —
    # blocked or not — and adds `CF-Ray` / `CF-Mitigated` headers.
    server_header = ""
    cf_ray = False
    cf_mitigated = False
    try:
        # asyncio.timeout wraps the entire fetch+redirect chase+stream so
        # the 15s cap is a true wall-clock deadline. httpx.Timeout alone
        # only bounds per-op (connect, read, write, pool), not the total —
        # a slow-drip server sending small chunks each under the read
        # cap could otherwise hold the worker indefinitely.
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,  # CRITICAL: validated manually below
                headers=headers,
            ) as client:
                resp = await _fetch_with_manual_redirects(client, url)
                try:
                    # Read the body BEFORE checking status. The Cloudflare
                    # detector below needs to scan the body even on 4xx /
                    # 5xx (CF often returns 403 / 503 with the challenge
                    # HTML as the body), and the server-snooping headers
                    # below need the response object to still be open.
                    status_code = resp.status_code
                    server_header = (resp.headers.get("server") or "").lower()
                    cf_ray = "cf-ray" in resp.headers
                    cf_mitigated = bool(resp.headers.get("cf-mitigated"))
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        body_bytes.extend(chunk)
                        if len(body_bytes) > MAX_RESPONSE_BYTES:
                            raise ScrapeError(
                                f"response exceeded {MAX_RESPONSE_BYTES // (1024*1024)} MB cap "
                                f"(server returned more than expected for an article page; "
                                f"refusing to load the rest)."
                            )
                    content_type = (resp.headers.get("content-type") or "").lower()
                    # httpx's resp.encoding pulls from charset= in the
                    # header OR falls back to apparent_encoding.
                    resp_encoding = (resp.encoding or "utf-8")
                finally:
                    await resp.aclose()
    except TimeoutError as e:
        # asyncio.timeout fires TimeoutError on wall-clock overrun.
        raise ScrapeError(
            f"timed out after {timeout:.0f}s — the server didn't return "
            "the page in time. Try again later or pick a different source.",
            error_kind="timeout",
        ) from e
    except httpx.TimeoutException as e:
        # httpx per-op timeout (connect / read / write). Same UX.
        raise ScrapeError(
            f"timed out after {timeout:.0f}s — the server didn't return "
            "the page in time. Try again later or pick a different source.",
            error_kind="timeout",
        ) from e
    except httpx.RequestError as e:
        raise ScrapeError(
            f"network error fetching {url!r}: {e}. Check the URL and "
            "that the site is reachable.",
            error_kind="network",
        ) from e

    # ---- Status-based error routing -------------------------------------
    # Decode just enough of the body to run the CF marker detector before
    # we decide how to phrase the error. errors='replace' so a wrong
    # encoding can't crash this branch — the user-facing message doesn't
    # need a perfect decode.
    try:
        preview_html = body_bytes[:8192].decode(resp_encoding, errors="replace")
    except LookupError:
        preview_html = body_bytes[:8192].decode("utf-8", errors="replace")

    is_cloudflare = (
        server_header == "cloudflare"
        or cf_ray
        or cf_mitigated
        or _looks_like_cloudflare_challenge(preview_html)
    )

    if status_code >= 400:
        if is_cloudflare or status_code in (403, 503, 429):
            # CF-shaped block OR one of the statuses CF uses for managed
            # challenges / rate limits. Before surfacing the cookies-
            # helpful error, give cloudscraper a single shot at the URL —
            # it can solve simple JS challenges that our raw httpx fetch
            # can't. If cloudscraper also fails, fall through to the
            # cookies guidance (which is the user's actual escape hatch).
            cloudscraper_recovered = await _try_cf_bypass_fallback(
                url, cookies=cookies, timeout=timeout, headers=headers,
            )
            if cloudscraper_recovered is not None:
                body_bytes = bytearray(cloudscraper_recovered[0])
                content_type = cloudscraper_recovered[1]
                resp_encoding = cloudscraper_recovered[2]
                status_code = 200  # synthetic OK — we have a body now
                logger.info(
                    "scrape_url: cloudscraper bypass succeeded for %s "
                    "(%d bytes)", url, len(body_bytes),
                )
                # Fall through to the success path below.
            else:
                cf_label = "Cloudflare" if is_cloudflare else "the site"
                raise ScrapeError(
                    f"{cf_label} blocked the request (HTTP {status_code}) "
                    f"and the automatic Cloudflare bypass also failed. "
                    f"This usually means the site's bot-detection rejected "
                    f"the scraper at the network layer. To work around it: "
                    f"open the URL in your browser, let any 'just a moment' "
                    f"check finish, then open devtools (F12 → Application → "
                    f"Storage → Cookies), copy ALL cookies for the site, "
                    f"and paste them into the Cookies field on the Import "
                    f"screen (the expandable section under the URL input). "
                    f"The scraper reuses your session for that one fetch. "
                    f"If you're already past the challenge in your browser "
                    f"and still get this error, also make sure the URL is "
                    f"the chapter page itself, not a redirect.",
                    error_kind="cf_blocked",
                )
        else:
            # 401 explicitly means auth required; other 4xx/5xx are
            # generic HTTP error and lump under 'http_error' for the UI.
            error_kind = "auth_required" if status_code == 401 else "http_error"
            raise ScrapeError(
                f"server returned HTTP {status_code}. The URL may be wrong, "
                "the page may require login, or the site may be blocking "
                "scrapers.",
                error_kind=error_kind,
            )

    if "html" not in content_type and not content_type.startswith("text/"):
        raise ScrapeError(
            f"content-type {content_type!r} is not an HTML page — the URL "
            "may point at a binary download or an image. Provide a chapter "
            "page URL instead.",
            error_kind="not_html",
        )

    try:
        html_text = body_bytes.decode(resp_encoding, errors="replace")
    except LookupError:
        # Unknown encoding name from the server header. Fall back to utf-8.
        html_text = body_bytes.decode("utf-8", errors="replace")

    # Cloudflare / challenge-page detection. Some bot-detection systems
    # return 200 with the challenge HTML in the body — without this
    # check, trafilatura would extract "Checking your browser before
    # accessing..." as a chapter and the user wouldn't understand why
    # their import looks like garbage. We bail with an actionable error
    # instead.
    if _looks_like_cloudflare_challenge(html_text):
        raise ScrapeError(
            "Cloudflare (or a similar bot-detection system) is challenging "
            "the request. To work around this: open the URL in your browser, "
            "let any 'just a moment' check finish, then open devtools "
            "(F12 → Application → Storage → Cookies), copy the cookies for "
            "the site, and paste them into the 'Cookies' field on the Import "
            "screen. The scraper will reuse your session. If the page lets "
            "you read it without a challenge in private/incognito mode, "
            "just retry — the site may have rate-limited the previous "
            "request.",
            error_kind="cf_blocked",
        )

    # trafilatura: main-article extraction. include_comments=False drops
    # comment sections that would otherwise pollute the chapter body;
    # include_tables=False likewise — tables are rarely chapter prose.
    # output_format='txt' produces paragraph-separated plain text matching
    # what parse_chapters expects on the upload path.
    extracted = trafilatura.extract(
        html_text,
        output_format="txt",
        include_comments=False,
        include_tables=False,
        include_links=False,
        favor_recall=True,  # prefer to keep ambiguous prose over discarding it
    )
    if not extracted or not extracted.strip():
        raise ScrapeError(
            "no extractable article content found on the page. The URL may "
            "point at an index / TOC page instead of a chapter, or the site "
            "may render its body via JavaScript that the scraper can't see.",
            error_kind="no_content",
        )

    # Title: trafilatura's metadata extractor gives a cleaner title than
    # whatever <title> the site emitted (which often appends " — Site Name").
    title = ""
    try:
        meta = trafilatura.extract_metadata(html_text)
        if meta and meta.title:
            title = meta.title.strip()
    except Exception as e:
        logger.debug("trafilatura metadata extraction failed: %s", e)
    if not title:
        title = parsed.hostname

    # Best-effort cover hunt. Any failure here MUST NOT break the import —
    # the user gets a coverless novel rather than a failed POST. _fetch_cover_image
    # catches every exception internally and returns None on any error path.
    cover_bytes: bytes | None = None
    cover_ext: str | None = None
    cover_url = _extract_cover_url(html_text, url)
    if cover_url:
        fetched = await _fetch_cover_image(cover_url, timeout=timeout)
        if fetched is not None:
            cover_bytes, cover_ext = fetched
            logger.info(
                "cover scrape ok: %s → %d bytes (%s)",
                cover_url, len(cover_bytes), cover_ext,
            )

    logger.info(
        "scrape ok: %s → %d chars (title=%r, cover=%s)",
        url, len(extracted), title, cover_ext or "none",
    )
    return ScrapeResult(
        text=extracted, title=title, source_url=url,
        cover_bytes=cover_bytes, cover_ext=cover_ext,
    )
