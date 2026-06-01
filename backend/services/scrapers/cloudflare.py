"""Cloudflare-bypass fetch chain.

Two complementary techniques wrapped behind one async helper:

1. **curl_cffi** (PRIMARY): real Chrome TLS handshake via curl-impersonate.
   Beats CF deployments that check the JA3/JA4 fingerprint — which is
   what modern CF actually uses to identify Python's stdlib SSL as a bot
   even when the UA + headers look like Chrome. This is the technique
   that successfully fetched 69shuba.com where cloudscraper failed.
2. **cloudscraper** (SECONDARY): solves older CF JS challenges (v1, v2)
   that don't rely on TLS fingerprinting. Kept as a backup because some
   sites use challenge cookies (cf_chl_*) that curl_cffi passes through
   unsolved.

`fetch_via_cf_bypass_chain` tries curl_cffi first; on its failure or a
non-2xx return, falls back to cloudscraper. Returns ``(body, content_type,
encoding)`` from whichever succeeded, or raises ``CloudScraperFailed`` if
both fail (caller falls through to the cookies-helpful error).

Called from two places:
  1. `scraper.py::scrape_url` — when the primary httpx attempt comes
     back with a CF-shaped 403 / 503 / 429, we run the bypass chain
     before surfacing the cookies-helpful error.
  2. `scraper.py::fetch_one` (the recipe-facing fetcher) — same logic
     on a per-fetch basis so site recipes benefit too.

Both libraries are sync, so we bridge to async via `asyncio.to_thread`.

SSRF / scheme / size guarantees:
- Caller MUST have already run `_check_url_safety(url)` and
  `_resolve_and_validate(parsed.hostname)` before invoking this.
- Redirects are NOT followed (`allow_redirects=False`). This tier can only
  validate the INITIAL url, not redirect targets, and curl_cffi / cloudscraper
  resolve+dial redirects internally with no per-hop SSRF re-check — so a
  CF-shaped first response that 302s to 169.254.169.254 / a LAN IP / 127.0.0.1
  would otherwise be chased into an internal host. A 3xx here is therefore
  treated as a failure. The primary httpx path in scraper.py is the one that
  follows redirects and re-validates every hop; this degraded fallback does not.
- Body size cap enforced after the sync library returns (neither curl_cffi
  nor cloudscraper expose a streaming primitive to cut mid-stream;
  acceptable because both hold the body in memory anyway).
- One-shot per tier: never loops on a failure.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


# Public limits — kept in sync with the values scraper.py uses for its
# httpx path so that cloudscraper-served pages don't slip past a smaller
# cap than the primary fetch would have enforced.
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


class CloudScraperFailed(Exception):
    """Raised when cloudscraper fails to fetch the URL (HTTP error,
    timeout, the bypass itself didn't work, etc.). Caller catches this
    and falls through to the next-best error message — never let it
    propagate to the HTTP route as a 500."""


def _do_fetch_sync(
    url: str, *, cookies: str | None, timeout: float,
    headers: dict | None = None,
) -> tuple[int, bytes, str]:
    """Synchronous body of the fetch — runs on a worker thread.

    Returns `(status_code, body_bytes, content_type)`. Raises
    CloudScraperFailed on any failure.
    """
    try:
        import cloudscraper  # noqa: PLC0415 — runtime import keeps the EXE bundle smaller when CF path is unused
    except Exception as e:
        raise CloudScraperFailed(
            f"cloudscraper not importable: {e}"
        ) from e

    # browser= drives cloudscraper's user-agent + JS interpreter choice.
    # Chrome-on-Windows is the safest default for novel sites. Disabling
    # delay='4' (its default polite wait between requests) is fine for a
    # one-shot retry from our path; we already failed the httpx call.
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True},
        delay=0,
    )
    # Inherit caller's headers (including any site-specific Referer that
    # the recipe set). cloudscraper's create_scraper sets its own
    # User-Agent we want to keep; only override what the caller passed.
    if headers:
        for k, v in headers.items():
            scraper.headers[k] = v
    if cookies:
        # cloudscraper inherits requests.Session; setting the Cookie
        # header on the session passes it through to every request.
        scraper.headers["Cookie"] = cookies.strip()

    try:
        # allow_redirects=False: see the module docstring's SSRF note. This
        # tier cannot re-validate redirect targets, so it must not follow them.
        resp = scraper.get(url, timeout=timeout, allow_redirects=False)
    except Exception as e:
        raise CloudScraperFailed(
            f"cloudscraper raised on GET {url!r}: {e}"
        ) from e

    return (
        resp.status_code,
        resp.content,
        (resp.headers.get("content-type") or "").lower(),
    )


async def fetch_via_cloudscraper(
    url: str,
    *,
    headers: dict | None = None,
    cookies: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, bytes, str]:
    """Async wrapper around cloudscraper's sync GET.

    Returns `(status_code, body_bytes, content_type)` on success.
    Raises CloudScraperFailed when cloudscraper itself errors, when the
    response exceeds `max_bytes`, or when the asyncio timeout fires.

    Caller MUST have SSRF-validated the URL before calling this.
    """
    try:
        async with asyncio.timeout(timeout + 2.0):
            status, body, content_type = await asyncio.to_thread(
                _do_fetch_sync,
                url,
                cookies=cookies,
                timeout=timeout,
                headers=headers,
            )
    except TimeoutError as e:
        raise CloudScraperFailed(
            f"cloudscraper timed out after {timeout:.0f}s"
        ) from e

    if len(body) > max_bytes:
        raise CloudScraperFailed(
            f"cloudscraper response exceeded {max_bytes // (1024*1024)} MB cap "
            f"({len(body)} bytes)"
        )

    return status, body, content_type


def _do_curl_cffi_sync(
    url: str, *, cookies: str | None, timeout: float,
    headers: dict | None = None,
) -> tuple[int, bytes, str]:
    """Synchronous curl_cffi GET. Runs on a worker thread.

    Returns `(status, body, content_type)`. Raises CloudScraperFailed
    on any failure.
    """
    try:
        from curl_cffi import requests as creq  # noqa: PLC0415
    except Exception as e:
        raise CloudScraperFailed(
            f"curl_cffi not importable: {e}"
        ) from e

    # impersonate="chrome" gives the curl-impersonate Chrome TLS
    # fingerprint, which is what CF actually checks at the network
    # layer. cloudscraper's Python-stdlib SSL fails this check; this
    # is the breakthrough that gets past 69shuba and similar.
    try:
        session = creq.Session(impersonate="chrome")
    except Exception as e:
        raise CloudScraperFailed(
            f"curl_cffi session init failed: {e}"
        ) from e

    # Pass caller's headers verbatim — recipes set things like Referer
    # (critical for 69shuba's per-path CF rule) that the bypass MUST
    # carry. Without this the curl_cffi retry would re-fetch with only
    # the default UA + Accept headers and 69shuba's chapter pages 403.
    if headers:
        for k, v in headers.items():
            session.headers[k] = v
    if cookies:
        session.headers["Cookie"] = cookies.strip()

    try:
        # allow_redirects=False: see the module docstring's SSRF note. This
        # tier cannot re-validate redirect targets, so it must not follow them.
        resp = session.get(url, timeout=timeout, allow_redirects=False)
    except Exception as e:
        raise CloudScraperFailed(
            f"curl_cffi raised on GET {url!r}: {e}"
        ) from e

    return (
        resp.status_code,
        resp.content,
        (resp.headers.get("content-type") or "").lower(),
    )


async def fetch_via_curl_cffi(
    url: str,
    *,
    headers: dict | None = None,
    cookies: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, bytes, str]:
    """Async wrapper around curl_cffi's sync GET.

    Same contract as fetch_via_cloudscraper. Returns
    `(status, body, content_type)` or raises CloudScraperFailed.

    Caller MUST have SSRF-validated the URL.
    """
    try:
        async with asyncio.timeout(timeout + 2.0):
            status, body, content_type = await asyncio.to_thread(
                _do_curl_cffi_sync,
                url,
                cookies=cookies,
                timeout=timeout,
                headers=headers,
            )
    except TimeoutError as e:
        raise CloudScraperFailed(
            f"curl_cffi timed out after {timeout:.0f}s"
        ) from e

    if len(body) > max_bytes:
        raise CloudScraperFailed(
            f"curl_cffi response exceeded {max_bytes // (1024*1024)} MB cap "
            f"({len(body)} bytes)"
        )

    return status, body, content_type


async def fetch_via_cf_bypass_chain(
    url: str,
    *,
    headers: dict | None = None,
    cookies: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, bytes, str]:
    """Try curl_cffi (primary), then cloudscraper (secondary). Return
    whichever succeeds with a 2xx; raise CloudScraperFailed if both
    fail or return non-2xx.

    `headers` are passed through verbatim to each tier. Site recipes
    rely on this — e.g. 69shuba's chapter pages require
    `Referer: <book-overview-url>` and would 403 without it.

    Why this tier order: curl_cffi defeats TLS-fingerprint blocks which
    is what modern CF deployments (including 69shuba) actually use.
    cloudscraper defeats older JS-only challenges that curl_cffi can't
    solve (because curl_cffi doesn't run JS). Different failure modes,
    so trying both gives the best practical coverage.
    """
    # Tier 1: curl_cffi (Chrome TLS impersonation).
    try:
        status, body, content_type = await fetch_via_curl_cffi(
            url, headers=headers, cookies=cookies,
            max_bytes=max_bytes, timeout=timeout,
        )
        if 200 <= status < 300:
            logger.info(
                "cf bypass chain: curl_cffi succeeded for %s (status=%d)",
                url, status,
            )
            return status, body, content_type
        # A 3xx counts as non-success here: redirects are disabled (SSRF), so a
        # 3xx is an unfollowed redirect, not a page. Fall through to the next tier.
        logger.info(
            "cf bypass chain: curl_cffi got HTTP %d for %s, trying cloudscraper",
            status, url,
        )
    except CloudScraperFailed as e:
        logger.info(
            "cf bypass chain: curl_cffi failed for %s (%s), trying cloudscraper",
            url, e,
        )

    # Tier 2: cloudscraper (legacy JS challenge solver).
    try:
        status, body, content_type = await fetch_via_cloudscraper(
            url, headers=headers, cookies=cookies,
            max_bytes=max_bytes, timeout=timeout,
        )
        if 200 <= status < 300:
            logger.info(
                "cf bypass chain: cloudscraper succeeded for %s (status=%d)",
                url, status,
            )
            return status, body, content_type
        raise CloudScraperFailed(
            f"both curl_cffi and cloudscraper returned non-2xx for {url}"
        )
    except CloudScraperFailed:
        raise
