"""uukanshu (uukanshu.cc) site recipe.

Inspired by lncrawl/sources/zh/uukanshu.py but the live site has been
through a redesign since lncrawl's recipe was last updated:

- The legacy uukanshu.net is dead (DNS doesn't resolve). The currently
  reachable mirror is uukanshu.cc.
- The legacy `dl.jieshao` info block + `ul#chapterList` reversed list
  have been replaced with a flat `dl.book.chapterlist > div > dd > a`
  structure that's already in ascending order. The recipe extracts
  chapters in document order without reversing.
- Encoding is UTF-8 on .cc (lncrawl said GBK on .net) — we decode UTF-8.

Verified empirically May 2026 against book 17474 (斗羅大陸V重生唐三 —
Doulu Dalu V).

Site shape:
- Hostname: `uukanshu.cc`. Subdomain mirrors (tw., m., …) get matched
  by the recipe via endswith — selectors are the same across them.
- Novel URL: `https://www.uukanshu.cc/book/<id>/`.
- Title: `h1` on the novel page (no longer wrapped in `dl.jieshao`).
- Cover: `img[src]` under `.bookcover` or `.bookintro` — both wrap the
  same image.
- Chapter list: `dl.book.chapterlist dd a` — ascending source order.
- Chapter body: `div.readcotent` (sic — the typo lives in the site's
  own markup).
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
    extract_printed_num_cn,
)

logger = logging.getLogger(__name__)


_HOST = "uukanshu.cc"

_CHAPTER_FETCH_INTERVAL = 0.2
_MAX_CHAPTERS_PER_CRAWL = 5000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:101.0) "
        "Gecko/20100101 Firefox/101.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class UukanshuRecipe(BaseRecipe):
    name = "uukanshu"
    # uukanshu's catalogue skews xianxia / wuxia / cultivation novels —
    # same as 69shuba. User can override on the novel page.
    default_genre = "xianxia"

    def matches(self, hostname: str) -> bool:
        return hostname == _HOST or hostname.endswith("." + _HOST)

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
                "URL is not a recognized uukanshu novel page. Paste a URL "
                "like https://www.uukanshu.cc/book/17474/"
            )

        status, body, _ct, _enc = await fetch(
            catalog_url, headers=_HEADERS, cookies=cookies,
        )
        if status >= 400:
            raise ScrapeError(
                f"uukanshu returned HTTP {status} for the novel page."
            )
        soup = BeautifulSoup(body.decode("utf-8", errors="replace"), "html.parser")

        title = _extract_title(soup)
        if not title:
            raise ScrapeError(
                "uukanshu page didn't contain a recognizable title — the "
                "site layout may have changed."
            )
        cover_url = _extract_cover_url(soup, base_url=catalog_url)

        chapter_links = _extract_chapter_links(soup, base_url=catalog_url)
        if not chapter_links:
            raise ScrapeError(
                "uukanshu chapter list was empty — the site layout may have "
                "changed, or this novel has no published chapters yet."
            )
        if len(chapter_links) > _MAX_CHAPTERS_PER_CRAWL:
            raise ScrapeError(
                f"uukanshu novel has {len(chapter_links)} chapters which "
                f"exceeds the {_MAX_CHAPTERS_PER_CRAWL}-chapter import cap."
            )
        if len(chapter_links) < 3:
            logger.warning(
                "uukanshu: only %d chapter links for %r — selector may be "
                "matching a navigation widget; aborting.",
                len(chapter_links), title,
            )
            raise ScrapeError(
                f"uukanshu: only {len(chapter_links)} chapter links found. "
                "Refusing to import a partial novel."
            )

        logger.info(
            "uukanshu: planned %r with %d chapters from %s",
            title, len(chapter_links), catalog_url,
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
            source_url=catalog_url,
            catalog_url=catalog_url,
            cover_url=cover_url,
            chapters=planned,
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

        await asyncio.sleep(_CHAPTER_FETCH_INTERVAL)
        referer = recipe_state.get("referer") or planned.source_url
        chapter_headers = {**_HEADERS, "Referer": referer}
        try:
            ch_status, ch_body, _ct, _enc = await fetch(
                planned.source_url, headers=chapter_headers, cookies=cookies,
            )
        except Exception as e:
            raise ScrapeError(
                f"uukanshu: failed fetching "
                f"{planned.title_zh or planned.source_url}: {e}"
            ) from e
        if ch_status >= 400:
            raise ScrapeError(
                f"uukanshu: HTTP {ch_status} fetching "
                f"{planned.title_zh or planned.source_url}. Aborting import."
            )
        ch_soup = BeautifulSoup(
            ch_body.decode("utf-8", errors="replace"), "html.parser"
        )
        body_text = _extract_chapter_body(ch_soup)
        if not body_text:
            logger.warning(
                "uukanshu: empty body for %s (%s)",
                planned.title_zh, planned.source_url,
            )
            body_text = ""
        return FetchedChapter(
            title_zh=planned.title_zh,
            original_text=body_text,
        )


# ---- Helpers ---------------------------------------------------------------

def _normalize_to_catalog(url: str) -> str | None:
    """Transform a user-pasted URL into the canonical /book/<id>/.

    /book/<id>/                → /book/<id>/
    /book/<id>                 → /book/<id>/   (missing trailing slash)
    /book/<id>/<chap>.html     → /book/<id>/
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname is None:
        return None
    if not (parsed.hostname == _HOST or parsed.hostname.endswith("." + _HOST)):
        return None
    m = re.match(r"^/book/(\d+)/?$", parsed.path)
    if m:
        return f"{parsed.scheme}://{parsed.hostname}/book/{m.group(1)}/"
    m = re.match(r"^/book/(\d+)/\d+\.html?$", parsed.path)
    if m:
        return f"{parsed.scheme}://{parsed.hostname}/book/{m.group(1)}/"
    return None


def _extract_title(soup: BeautifulSoup) -> str:
    """The novel title sits in the top-level `<h1>` on the catalog page.
    Confirmed against the captured fixture (斗羅大陸V重生唐三)."""
    el = soup.select_one("h1")
    if el:
        return el.get_text(strip=True)
    return ""


def _extract_cover_url(soup: BeautifulSoup, *, base_url: str) -> str | None:
    """Cover lives under `.bookcover img` (also mirrored under
    `.bookintro img` — same src). Returns the absolute URL."""
    for sel in (".bookcover img", ".bookintro img"):
        el = soup.select_one(sel)
        if el and el.get("src"):
            return urllib.parse.urljoin(base_url, el["src"])
    return None


def _extract_chapter_links(
    soup: BeautifulSoup, *, base_url: str,
) -> list[tuple[str, str]]:
    """Walk `dl.book.chapterlist dd a` in document order. uukanshu.cc
    lists chapters ascending (prologue first, finale last) so no
    reversal is needed — unlike lncrawl's note on the legacy .net site.
    """
    rows = soup.select("dl.book.chapterlist dd a")
    out: list[tuple[str, str]] = []
    for a in rows:
        href = a.get("href")
        if not href:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        out.append((title, urllib.parse.urljoin(base_url, href)))
    logger.info(
        "uukanshu: dl.book.chapterlist -> %d chapter links",
        len(out),
    )
    return out


def _extract_chapter_body(soup: BeautifulSoup) -> str:
    """Pull prose out of `div.readcotent` (sic — the typo is in the
    site's own markup). Returns paragraph-separated text."""
    container = soup.select_one("div.readcotent")
    if container is None:
        return ""
    for sel in ("script", "style"):
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


# Register the recipe with the dispatcher at import time.
from backend.services.scrapers import register  # noqa: E402

register(UukanshuRecipe())
