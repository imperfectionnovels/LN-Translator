"""69shuba.com (+ mirrors) site recipe.

Ported from lncrawl/sources/zh/69shuba.py (MIT-licensed upstream;
acknowledgement: https://github.com/lncrawl/lightnovel-crawler).

What this recipe handles vs the generic trafilatura scraper:
- **Encoding**: 69shuba serves GBK-encoded HTML. trafilatura's auto-
  detection sometimes misreads the headers and produces mojibake; this
  recipe hard-codes `gbk` and decodes manually.
- **Firefox UA**: empirically the site rejects a wide Chrome client-hints
  stack with 403 but accepts a Firefox UA without client hints. The
  generic scraper now sends Chrome 130 — works for most sites but is
  the wrong shape for this one.
- **Book index → chapter crawl**: a URL like `/book/88724.htm` is a
  novel overview page, not a chapter. lncrawl transforms `/txt/<id>.htm`
  → `/<id>/` to land on the chapter-list page, then walks
  `div#catalog ul li` to enumerate every chapter, fetching each in
  series.
- **Chapter body extraction**: the chapter HTML wraps the prose in
  `div.txtnav` together with title (`h1`), translator notes
  (`div.txtinfo`), and navigation (`div#txtright`). We strip the noise
  and keep the prose.

Crawling a 2000+ chapter novel takes minutes. We rate-limit to
~5 req/s (per lncrawl's polite-citizen default) so we don't get the
IP banned.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from backend.services.parser import has_author_note_markers
from backend.services.scrapers.base import (
    BaseRecipe,
    FetchedChapter,
    PlannedChapterRef,
    ProgressFn,
    RecipePlan,
    extract_printed_num_cn,
)

logger = logging.getLogger(__name__)


_HOSTS = (
    "69shuba.com",
    "69shu.com",
    "69xinshu.com",
    "69shu.pro",
    "69shuba.pro",
)

# Headers verbatim from lncrawl/sources/zh/69shuba.py — Firefox 101 UA
# without Chrome's Sec-Ch-Ua hints. 69shuba's edge accepts this and 403s
# on the generic Chrome stack our scrape_url sends.
_HEADERS_FIREFOX = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:101.0) "
        "Gecko/20100101 Firefox/101.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Polite throttle between consecutive chapter fetches when crawling an
# index. 1.0s = 1 req/s. lncrawl uses 5 req/s; that triggers 69shuba's
# Cloudflare rate limiter after ~30 chapters and aborts the import.
# 1 req/s stays under the threshold in practice. Slower than ideal —
# a 1500-chapter novel takes ~25 min instead of ~5 — but successful
# imports beat fast aborts. Tune lower only if you're sure your IP /
# Cloudflare bucket can take it.
_CHAPTER_FETCH_INTERVAL = 1.0

# Retry strategy for 4xx/5xx on a chapter fetch. Cloudflare rate limits
# typically lift after a brief cooldown; one or two long pauses recover
# the import where an immediate retry would still 403. Each entry is
# how long to sleep BEFORE the next retry attempt.
_CHAPTER_RETRY_BACKOFFS = (15.0, 45.0, 90.0)

# Hard cap on chapter count per import. A 10,000-chapter web novel
# would take ~30 minutes to crawl at the rate above and tie up the
# server. The cap is generous enough for the longest real novels
# (Mortal Cultivation is ~2,500 chapters); anything bigger probably
# wants pagination through multiple imports anyway.
_MAX_CHAPTERS_PER_CRAWL = 5000


class SixNineShuRecipe(BaseRecipe):
    name = "69shuba"
    # 69shuba is a Chinese cultivation/xianxia-dominant site. The user can
    # always override via the novel overview page; this is the import-time
    # default when the user doesn't pick one explicitly.
    default_genre = "xianxia"

    def matches(self, hostname: str) -> bool:
        # Dispatcher pre-strips "www.". Allow exact host OR any subdomain
        # match — m.69shuba.com routes to the same recipe.
        return any(hostname == h or hostname.endswith("." + h) for h in _HOSTS)

    async def plan(
        self,
        url: str,
        *,
        cookies: str | None,
        fetch: Any,
        progress: ProgressFn = None,
    ) -> RecipePlan:
        # Step 1: normalize the URL to the canonical chapter-list page.
        # 69shuba's URL shapes:
        #   /book/<numeric_id>.htm   -> book overview page (chapter list
        #                                in <div id="catalog">)
        #   /txt/<alnum_id>.htm      -> alternate overview
        #   /<id>/                   -> chapter list (no .htm)
        #   /<id>/<chap>.html        -> single chapter
        #
        # For the first three shapes we crawl the chapter list. For an
        # individual chapter URL we'd just import that one chapter — but
        # that's a degenerate case for this recipe; the user would more
        # naturally paste the overview URL.
        from backend.services.scraper import ScrapeError  # noqa: PLC0415 — avoid circular

        if progress:
            await progress("fetching_overview", 0, 0)

        normalized_index_url = _normalize_to_chapter_list(url)
        if normalized_index_url is None:
            raise ScrapeError(
                "URL is not a recognized 69shuba book or chapter-list page. "
                "Paste a URL like https://www.69shuba.com/book/88724.htm or "
                "https://www.69shuba.com/<id>/."
            )

        # Step 2: fetch the overview (the one with /book/N.htm — has the
        # cover + metadata) AND the chapter-list page (often a different
        # URL on the same site). lncrawl visits both because the cover +
        # author live on /book/N.htm and the chapter list lives on /N/.
        overview_url = _to_overview_url(url) or normalized_index_url
        status, body, content_type, _enc = await fetch(
            overview_url,
            headers=_HEADERS_FIREFOX,
            cookies=cookies,
        )
        if status >= 400:
            raise ScrapeError(
                f"69shuba returned HTTP {status} for the overview page. "
                "The site may be blocking the request — try the cookies "
                "field in Import."
            )
        overview_html = _decode_gbk(body)
        overview = BeautifulSoup(overview_html, "html.parser")

        # Metadata pulled from the overview. The recipe falls back to
        # sensible defaults if a selector misses — never refuses the
        # import for missing-author / missing-tags.
        title = _extract_title(overview)
        if not title:
            raise ScrapeError(
                "69shuba overview page didn't contain a recognizable title — "
                "the site layout may have changed."
            )
        cover_url = _extract_cover_url(overview, base_url=overview_url)

        # Step 3: fetch the chapter-list page if it's a different URL.
        if normalized_index_url != overview_url:
            status, body, content_type, _enc = await fetch(
                normalized_index_url,
                headers=_HEADERS_FIREFOX,
                cookies=cookies,
            )
            if status >= 400:
                raise ScrapeError(
                    f"69shuba returned HTTP {status} for the chapter list."
                )
            list_soup = BeautifulSoup(_decode_gbk(body), "html.parser")
        else:
            list_soup = overview

        chapter_links = _extract_chapter_links(list_soup, base_url=normalized_index_url)
        if not chapter_links:
            raise ScrapeError(
                "69shuba chapter list was empty — the site layout may have "
                "changed, or this novel has no published chapters yet."
            )
        if len(chapter_links) > _MAX_CHAPTERS_PER_CRAWL:
            raise ScrapeError(
                f"69shuba novel has {len(chapter_links)} chapters which "
                f"exceeds the {_MAX_CHAPTERS_PER_CRAWL}-chapter import cap. "
                "Open a feature request if you need bigger imports."
            )
        if len(chapter_links) < 3:
            logger.warning(
                "69shuba: only %d chapter links extracted for %r — selector "
                "may be matching a widget rather than the full catalog. "
                "Aborting to avoid a silent partial import.",
                len(chapter_links), title,
            )
            raise ScrapeError(
                f"69shuba: only {len(chapter_links)} chapter links found, "
                "which looks like a widget match rather than the full catalog. "
                "Refusing to import a partial novel — the site layout may have changed."
            )

        logger.info(
            "69shuba: planned %r with %d chapters from %s",
            title, len(chapter_links), overview_url,
        )

        planned = tuple(
            PlannedChapterRef(
                chapter_num=i,
                title_zh=ch_title,
                source_url=ch_url,
                printed_num=extract_printed_num_cn(ch_title),
            )
            for i, (ch_title, ch_url) in enumerate(chapter_links, start=1)
        )

        return RecipePlan(
            title=title,
            source_url=overview_url,
            catalog_url=normalized_index_url,
            cover_url=cover_url,
            chapters=planned,
            # 69shuba's Cloudflare rule requires Referer: <overview_url>
            # on chapter fetches — without it every chapter 403s. Stash
            # it in recipe_state so resumed runs (which re-create the
            # state dict via discover_state_for_resume) get it too.
            recipe_state={"referer": overview_url},
        )

    async def fetch_chapter(
        self,
        planned: PlannedChapterRef,
        *,
        cookies: str | None,
        fetch: Any,
        recipe_state: dict,
    ) -> FetchedChapter:
        from backend.services.scraper import ScrapeError  # noqa: PLC0415

        # Polite throttle — 1 req/s keeps us under 69shuba's CF rate limit.
        await asyncio.sleep(_CHAPTER_FETCH_INTERVAL)

        referer = recipe_state.get("referer") or planned.source_url
        chapter_headers = {**_HEADERS_FIREFOX, "Referer": referer}
        ch_status, ch_body = await _fetch_chapter_with_backoff(
            fetch,
            planned.source_url,
            chapter_headers=chapter_headers,
            cookies=cookies,
            position=planned.chapter_num,
            total=0,  # No total here; backoff helper only uses for logs.
            title=planned.title_zh or "",
        )
        if ch_status >= 400:
            raise ScrapeError(
                f"69shuba: HTTP {ch_status} fetching "
                f"{planned.title_zh or planned.source_url}. Aborting import "
                "after backoff retries failed."
            )
        ch_soup = BeautifulSoup(_decode_gbk(ch_body), "html.parser")
        body_text = _extract_chapter_body(ch_soup)
        if not body_text:
            logger.warning(
                "69shuba: empty body for %s (%s)",
                planned.title_zh, planned.source_url,
            )
            body_text = ""
        return FetchedChapter(
            title_zh=planned.title_zh,
            original_text=body_text,
        )


# ---- Helpers ----------------------------------------------------------------

async def _fetch_chapter_with_backoff(
    fetch: Any,
    url: str,
    *,
    chapter_headers: dict,
    cookies: str | None,
    position: int,
    total: int,
    title: str,
) -> tuple[int, bytes]:
    """Fetch a chapter URL with exponential backoff on transient 4xx/5xx.

    69shuba's Cloudflare aggressively rate-limits the IP after a burst
    of fast requests — even with curl_cffi's Chrome TLS fingerprint, a
    sustained 5 req/s eats through CF's bucket and hits 403 around
    chapter 30. The rate limit lifts within ~1 minute, so backing off
    and retrying recovers the import. Without this the recipe would
    abort the entire 1500-chapter crawl over one transient 403.

    Returns ``(status, body)``. On all retries failing, returns the
    last ``(status, body)`` so the caller decides whether to abort.
    Network errors (raised exceptions) propagate after exhausting
    retries.
    """
    last_status = 0
    last_body = b""
    last_exc: Exception | None = None
    # First attempt + the backoff retries.
    attempts = [0.0, *_CHAPTER_RETRY_BACKOFFS]
    for attempt_idx, sleep_before in enumerate(attempts):
        if sleep_before > 0:
            logger.info(
                "69shuba: backing off %.0fs before retry %d for chapter %d/%d (%s)",
                sleep_before, attempt_idx, position, total, title,
            )
            await asyncio.sleep(sleep_before)
        try:
            status, body, _ct, _enc = await fetch(
                url, headers=chapter_headers, cookies=cookies,
            )
        except Exception as e:
            last_exc = e
            logger.warning(
                "69shuba: fetch error on chapter %d/%d attempt %d (%s): %s",
                position, total, attempt_idx + 1, title, e,
            )
            continue
        last_status = status
        last_body = body
        last_exc = None
        if status < 400:
            return status, body
        logger.warning(
            "69shuba: HTTP %d on chapter %d/%d attempt %d (%s)",
            status, position, total, attempt_idx + 1, title,
        )
    # Exhausted retries.
    if last_exc is not None:
        from backend.services.scraper import ScrapeError  # noqa: PLC0415
        raise ScrapeError(
            f"69shuba: failed fetching chapter {position}/{total} "
            f"({title}) after {len(attempts)} attempts: {last_exc}"
        ) from last_exc
    return last_status, last_body


def _decode_gbk(body: bytes) -> str:
    """Decode GBK with replacement for invalid bytes. 69shuba is GBK-
    encoded; ignoring this gives mojibake."""
    try:
        return body.decode("gbk", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _normalize_to_chapter_list(url: str) -> str | None:
    """Transform a user-pasted URL into the canonical chapter-list URL.

    Empirically verified against /book/88724.htm vs /88724/ (May 2026):
    the /book/N.htm page is a metadata-only overview carrying just the
    'recent updates' widget (~5 entries). The full catalog (1500+
    chapters) lives at /<id>/. lncrawl's recipe does the same transform
    for /txt/<id>.htm overviews — we extend it to numeric /book/ IDs.

    /book/<id>.htm   → /<id>/   (was: returned unchanged — silent partial)
    /txt/<id>.htm    → /<id>/
    /<id>/           → /<id>/
    /<id>/<ch>...    → /<id>/
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    # /book/<numeric>.htm — overview page; real catalog is /<numeric>/
    m = re.match(r"^/book/(\d+)\.htm$", path)
    if m:
        return f"{parsed.scheme}://{parsed.hostname}/{m.group(1)}/"
    # /txt/<alnum-or-numeric>.htm — transform per lncrawl
    m = re.match(r"^/txt/([A-Za-z0-9]+)\.htm$", path)
    if m:
        return f"{parsed.scheme}://{parsed.hostname}/{m.group(1)}/"
    # /<id>/ chapter list (root-level alphanumeric id)
    m = re.match(r"^/([A-Za-z0-9]+)/?$", path)
    if m:
        return f"{parsed.scheme}://{parsed.hostname}/{m.group(1)}/"
    # /<id>/<chap_id>[.html] — strip down to the catalog. 69shuba serves
    # individual chapters at /txt/<book>/<chap_id> (no extension); the
    # /A12345/123.html shape is the older form.
    m = re.match(r"^/txt/(\d+)/\d+/?$", path)
    if m:
        return f"{parsed.scheme}://{parsed.hostname}/{m.group(1)}/"
    m = re.match(r"^/([A-Za-z0-9]+)/\d+\.html?$", path)
    if m:
        return f"{parsed.scheme}://{parsed.hostname}/{m.group(1)}/"
    return None


def _to_overview_url(url: str) -> str | None:
    """Return the /book/<id>.htm URL when possible — that's the page
    with the cover and author metadata. Returns None when the URL
    shape doesn't carry a numeric book id (e.g. /<alnum_id>/)."""
    parsed = urllib.parse.urlparse(url)
    m = re.match(r"^/book/(\d+)\.htm$", parsed.path)
    if m:
        return f"{parsed.scheme}://{parsed.hostname}/book/{m.group(1)}.htm"
    return None


def _extract_title(soup: BeautifulSoup) -> str:
    """Title from `div.booknav2 h1` (overview page) or `<title>` tag
    fallback. Trimmed + de-tagged."""
    el = soup.select_one("div.booknav2 h1")
    if el:
        return el.get_text(strip=True)
    el = soup.select_one("title")
    if el:
        # The <title> often has "novel name_69shuba" — strip the suffix.
        t = el.get_text(strip=True)
        return re.sub(r"[_\-]\s*69.*$", "", t).strip()
    return ""


def _extract_cover_url(soup: BeautifulSoup, *, base_url: str) -> str | None:
    """Cover from `div.bookimg2 img[src]`. Resolves relative URLs."""
    el = soup.select_one("div.bookimg2 img")
    if el and el.get("src"):
        return urllib.parse.urljoin(base_url, el["src"])
    # og:image fallback (some 69shuba mirrors include it).
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        return urllib.parse.urljoin(base_url, og["content"])
    return None


def _extract_chapter_links(
    soup: BeautifulSoup, *, base_url: str,
) -> list[tuple[str, str]]:
    """Return ordered list of (title, absolute_url) tuples from the
    catalog page, in **ascending** chapter order with announcements
    filtered out.

    The `div#catalog` selector matches lncrawl's. The historical
    `div.qustime` fallback was removed in May 2026: it never matched
    real catalogs, only the 5-entry 'recent updates' widget on the
    /book/N.htm overview page, which caused silent 5-chapter imports.
    If both catalog selectors miss, the caller raises rather than
    importing a partial novel.

    69shuba lists chapters latest-first; we reverse so chapter 1 lands
    at index 0. Author-note entries (中奖名单, 公告, 求月票, …) sit
    interleaved with real chapters and are dropped via
    `has_author_note_markers` — they'd otherwise occupy chapter numbers
    and shift every subsequent chapter.
    """
    rows = soup.select("div#catalog ul li a")
    selector = "div#catalog"
    if not rows:
        rows = soup.select("div.catalog ul li a")
        selector = "div.catalog"
    if not rows:
        logger.warning(
            "69shuba: catalog selector miss (tried div#catalog, div.catalog)"
        )
        return []

    out: list[tuple[str, str]] = []
    dropped = 0
    for a in rows:
        href = a.get("href")
        if not href:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        if has_author_note_markers(title):
            dropped += 1
            continue
        out.append((title, urllib.parse.urljoin(base_url, href)))
    out.reverse()
    logger.info(
        "69shuba: catalog %s -> %d chapter links (dropped %d announcements)",
        selector, len(out), dropped,
    )
    return out


def _extract_chapter_body(soup: BeautifulSoup) -> str:
    """Pull the prose out of `div.txtnav` (lncrawl's selector). Strip
    the `h1` (duplicates the title), `div.txtinfo` (translator notes),
    and `div#txtright` (next/prev navigation).

    Returns paragraph-separated plain text — the shape the chapter
    inserter expects in `original_text`."""
    container = soup.select_one("div.txtnav")
    if container is None:
        return ""
    for sel in ("h1", "div.txtinfo", "div#txtright", "script", "style"):
        for el in container.select(sel):
            el.decompose()
    # Get the text with newline breaks at <br> and block boundaries.
    text = container.get_text("\n", strip=False)
    # Collapse runs of 3+ newlines down to a paragraph break (\n\n).
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Trim each line, drop empties, rejoin as paragraphs.
    lines = [ln.strip() for ln in text.split("\n")]
    paragraphs: list[str] = []
    buf: list[str] = []
    for ln in lines:
        if not ln:
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
        else:
            buf.append(ln)
    if buf:
        paragraphs.append(" ".join(buf))
    return "\n\n".join(paragraphs).strip()


# Register the recipe with the dispatcher at import time.
from backend.services.scrapers import register  # noqa: E402

register(SixNineShuRecipe())
