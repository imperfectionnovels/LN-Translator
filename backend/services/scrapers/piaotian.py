"""piaotian (piaotia.com + mirrors) site recipe.

Ported from lncrawl/sources/zh/piaotian.py (MIT-licensed upstream).

Mirrors live on multiple TLDs:
- www.piaotia.com — main (verified live May 2026)
- www.ptwxz.com   — alternate (verified live)
- www.piaotian.cc — alternate (verified live)
- www.piaotian.com — exists but ~4.5KB only; likely a redirect/splash

Site shape (verified empirically May 2026 against
piaotia.com/html/1/1705/ = 傲世九重天 / Aoshi Jiu Chong Tian):

- URL forms:
  - Book overview: /bookinfo/<a>/<b>.html  (metadata page)
  - Catalog:       /html/<a>/<b>/          (chapter list)
  - Chapter:       /html/<a>/<b>/<N>.html  (individual chapter)
  The recipe transforms /bookinfo/ → /html/ to land on the catalog.
- Catalog selector: `div.centent ul li a` (sic — "centent" is the typo
  in the site's own markup, kept verbatim from lncrawl).
- Title: `div.title` with trailing "最新章节" suffix stripped.
- Cover: constructed from path IDs as
  `https://<host>/files/article/image/<a>/<b>/<b>s.jpg`.
- Chapter body: the chapter HTML doesn't carry a `<div id="content">`.
  Instead, the prose follows an inline script:
      <script language="javascript">GetFont();</script>
  lncrawl's hack — which still works May 2026 — is to string-replace
  that script with `<div id="content">` before parsing, so BS4
  auto-closes the div around the prose. Then h1/script/div/table inside
  are decomposed (they're navigation + ad shims).
- Encoding: GBK.
- Cloudflare: not present on the verified mirrors. The recipe routes
  through `fetch_one`'s CF bypass chain anyway so future protection
  is handled transparently.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from backend.services.scrapers.base import (
    BaseRecipe,
    FetchedChapter,
    PlannedChapterRef,
    ProgressFn,
    RecipePlan,
)

logger = logging.getLogger(__name__)


_HOSTS = (
    "piaotia.com",
    "ptwxz.com",
    "piaotian.cc",
    "piaotian.com",  # weak signal — kept for completeness; users rarely hit it
)

_CHAPTER_FETCH_INTERVAL = 0.2
_MAX_CHAPTERS_PER_CRAWL = 5000

# lncrawl's GetFont() JS marker. The site injects ad-loader scripts
# inline through the page, and the chapter prose immediately follows
# this one. The string is stable enough across mirrors to use as the
# replacement anchor; if it changes the recipe falls back to a regex.
_GETFONT_SHIM = '<script language="javascript">GetFont();</script>'
_GETFONT_RE = re.compile(
    r"<script\s+language=[\"']javascript[\"']\s*>\s*GetFont\(\)\s*;\s*</script>",
    re.IGNORECASE,
)

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
}


class PiaotianRecipe(BaseRecipe):
    name = "piaotian"
    # piaotian's catalogue is dominated by xianxia / wuxia / cultivation
    # novels (傲世九重天, 武动乾坤, 奥术神座 …). User can override.
    default_genre = "xianxia"

    def matches(self, hostname: str) -> bool:
        return any(hostname == h or hostname.endswith("." + h) for h in _HOSTS)

    async def plan(
        self,
        url: str,
        *,
        cookies: str | None,
        fetch: Any,
        progress: ProgressFn = None,
    ) -> RecipePlan:
        from backend.services.scraper import ScrapeError  # noqa: PLC0415

        if progress:
            await progress("fetching_overview", 0, 0)

        catalog_url = _normalize_to_catalog(url)
        if catalog_url is None:
            raise ScrapeError(
                "URL is not a recognized piaotian novel page. Paste a URL "
                "like https://www.piaotia.com/bookinfo/1/1705.html or "
                "https://www.piaotia.com/html/1/1705/"
            )

        status, body, _ct, _enc = await fetch(
            catalog_url, headers=_HEADERS_FIREFOX, cookies=cookies,
        )
        if status >= 400:
            raise ScrapeError(
                f"piaotian returned HTTP {status} for the catalog page."
            )
        catalog_html = body.decode("gbk", errors="replace")
        soup = BeautifulSoup(catalog_html, "html.parser")

        title = _extract_title(soup)
        if not title:
            raise ScrapeError(
                "piaotian page didn't contain a recognizable title — the "
                "site layout may have changed."
            )

        chapter_links = _extract_chapter_links(soup, base_url=catalog_url)
        if not chapter_links:
            raise ScrapeError(
                "piaotian chapter list was empty — the site layout may "
                "have changed."
            )
        if len(chapter_links) > _MAX_CHAPTERS_PER_CRAWL:
            raise ScrapeError(
                f"piaotian novel has {len(chapter_links)} chapters which "
                f"exceeds the {_MAX_CHAPTERS_PER_CRAWL}-chapter import cap."
            )
        if len(chapter_links) < 3:
            logger.warning(
                "piaotian: only %d chapter links for %r — aborting to "
                "avoid a silent partial import.",
                len(chapter_links), title,
            )
            raise ScrapeError(
                f"piaotian: only {len(chapter_links)} chapter links found. "
                "Refusing to import a partial novel."
            )

        cover_url = _construct_cover_url(catalog_url)

        logger.info(
            "piaotian: planned %r with %d chapters from %s",
            title, len(chapter_links), catalog_url,
        )

        planned = tuple(
            PlannedChapterRef(
                chapter_num=i,  # placeholder; runner reconciles
                title_zh=ch_title,
                source_url=ch_url,
                printed_num=_extract_printed_num(ch_title),
            )
            for i, (ch_title, ch_url) in enumerate(chapter_links, start=1)
        )

        return RecipePlan(
            title=title,
            source_url=catalog_url,
            catalog_url=catalog_url,
            cover_url=cover_url,
            chapters=planned,
            # Per-novel state: the Referer is the catalog URL — every
            # chapter fetch needs it. Carried in recipe_state so the
            # runner can persist it (today: re-derived on resume from
            # the novel's source_url; tomorrow if we add a state table
            # this is where it lands).
            recipe_state={"referer": catalog_url},
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

        # Polite delay between fetches — caller-driven loop; we don't
        # know the position here, so the runner is responsible for
        # interval enforcement. Recipe-side rate limiting on a per-
        # chapter basis is built into the per-call sleep below.
        await asyncio.sleep(_CHAPTER_FETCH_INTERVAL)

        referer = recipe_state.get("referer") or planned.source_url
        chapter_headers = {**_HEADERS_FIREFOX, "Referer": referer}
        try:
            ch_status, ch_body, _ct, _enc = await fetch(
                planned.source_url, headers=chapter_headers, cookies=cookies,
            )
        except Exception as e:
            raise ScrapeError(
                f"piaotian: failed fetching {planned.title_zh or planned.source_url}: {e}"
            ) from e
        if ch_status >= 400:
            raise ScrapeError(
                f"piaotian: HTTP {ch_status} fetching "
                f"{planned.title_zh or planned.source_url}. Aborting import."
            )
        body_text = _extract_chapter_body(
            ch_body.decode("gbk", errors="replace"),
        )
        if not body_text:
            logger.warning(
                "piaotian: empty body for %s (%s)",
                planned.title_zh, planned.source_url,
            )
            body_text = ""
        return FetchedChapter(
            title_zh=planned.title_zh,
            original_text=body_text,
        )


# ---- Helpers ---------------------------------------------------------------

def _normalize_to_catalog(url: str) -> str | None:
    """Map a user-pasted URL to the canonical catalog form.

    /bookinfo/<a>/<b>.html  → /html/<a>/<b>/
    /html/<a>/<b>/index.html → /html/<a>/<b>/
    /html/<a>/<b>/           → /html/<a>/<b>/
    /html/<a>/<b>            → /html/<a>/<b>/
    /html/<a>/<b>/<N>.html   → /html/<a>/<b>/   (chapter URL → catalog)
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname is None:
        return None
    if not any(
        parsed.hostname == h or parsed.hostname.endswith("." + h)
        for h in _HOSTS
    ):
        return None
    path = parsed.path
    # /bookinfo/<a>/<b>.html
    m = re.match(r"^/bookinfo/(\d+)/(\d+)\.html?$", path)
    if m:
        a, b = m.group(1), m.group(2)
        return f"{parsed.scheme}://{parsed.hostname}/html/{a}/{b}/"
    # /html/<a>/<b>/[index.html | <N>.html | <empty>]
    m = re.match(r"^/html/(\d+)/(\d+)/(?:|index\.html?|\d+\.html?)$", path)
    if m:
        a, b = m.group(1), m.group(2)
        return f"{parsed.scheme}://{parsed.hostname}/html/{a}/{b}/"
    # /html/<a>/<b>
    m = re.match(r"^/html/(\d+)/(\d+)$", path)
    if m:
        a, b = m.group(1), m.group(2)
        return f"{parsed.scheme}://{parsed.hostname}/html/{a}/{b}/"
    return None


def _construct_cover_url(catalog_url: str) -> str | None:
    """piaotian doesn't expose a cover via meta tags; the path is
    constructed from the catalog IDs. lncrawl uses the same pattern."""
    parsed = urllib.parse.urlparse(catalog_url)
    m = re.match(r"^/html/(\d+)/(\d+)/?$", parsed.path)
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    return f"{parsed.scheme}://{parsed.hostname}/files/article/image/{a}/{b}/{b}s.jpg"


def _extract_title(soup: BeautifulSoup) -> str:
    """`div.title` carries the title with a trailing "最新章节" suffix
    that we strip. lncrawl does the same."""
    el = soup.select_one("div.title")
    if not el:
        return ""
    return el.get_text(strip=True).replace("最新章节", "").strip()


def _extract_chapter_links(
    soup: BeautifulSoup, *, base_url: str,
) -> list[tuple[str, str]]:
    """Walk `div.centent ul li a` (typo intentional — matches site
    markup). Already in ascending order on the live site."""
    out: list[tuple[str, str]] = []
    for a in soup.select("div.centent ul li a"):
        href = a.get("href")
        if not href:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        out.append((title, urllib.parse.urljoin(base_url, href)))
    logger.info(
        "piaotian: div.centent ul li a -> %d chapter links",
        len(out),
    )
    return out


def _extract_chapter_body(html: str) -> str:
    """Pull prose out of the chapter page.

    piaotian doesn't wrap the prose in any container element. lncrawl's
    fix — which still applies May 2026 — is to string-replace the
    `GetFont()` script tag that precedes the prose with an opening
    `<div id="content">` tag, then let BeautifulSoup auto-close the div
    at the next sibling block. Inside that synthetic div we decompose
    h1 / script / div / table (navigation + ad shims).
    """
    if _GETFONT_SHIM in html:
        hacked = html.replace(_GETFONT_SHIM, '<div id="content">')
    else:
        # Marker has been edited; try the regex form.
        hacked, n = _GETFONT_RE.subn('<div id="content">', html, count=1)
        if n == 0:
            logger.warning("piaotian: GetFont() shim not found; selector miss")
            return ""

    soup = BeautifulSoup(hacked, "html.parser")
    container = soup.select_one("div#content")
    if container is None:
        return ""
    # Strip noise. h1 carries the chapter title (duplicates), div/table
    # wrap ad blocks, script is the rest of the loader chain.
    for sel in ("h1", "H1", "script", "div", "table"):
        for el in container.select(sel):
            el.decompose()
    text = container.get_text("\n", strip=False)
    text = re.sub(r"\n{3,}", "\n\n", text)
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


def _extract_printed_num(title: str) -> int | None:
    """Same patterns as 69shuba / uukanshu."""
    if not title:
        return None
    m = re.search(r"第\s*(\d+)\s*[章回節]", title)
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*([一二三四五六七八九十百千万零]+)\s*[章回節]", title)
    if m:
        try:
            from backend.services.scrapers.sixnineshu import _han_digits_to_int
            return _han_digits_to_int(m.group(1))
        except (ValueError, ImportError):
            return None
    return None


from backend.services.scrapers import register  # noqa: E402

register(PiaotianRecipe())
