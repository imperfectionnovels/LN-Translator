"""Syosetu (ncode.syosetu.com) site recipe.

Ported from lncrawl/sources/ja/syosetu.py (MIT-licensed upstream;
acknowledgement: https://github.com/lncrawl/lightnovel-crawler).

Site shape (verified empirically May 2026 against `n9669bk`):
- Hostnames: `ncode.syosetu.com` (the canonical web-novel host).
- Novel URLs: `https://ncode.syosetu.com/<ncode>/` — the catalog page.
  `<ncode>` is a short alnum slug like `n9669bk`.
- Catalog pagination: long novels split the TOC across multiple pages
  via `?p=N`. The last-page link sits in `.c-pager__item--last[href]`
  (e.g. `/n9669bk/?p=3` → 3 total pages).
- Chapter rows: `.p-eplist__sublist a` — clean title text, href like
  `/n9669bk/<num>/`. Numeric chapter slugs ascend in source order.
- Volume headers: `.p-eplist__chapter-title` — interspersed between
  chapter groups. We skip these (they're not chapters); the per-novel
  genre / overview captures volume structure via a separate path if
  needed (current schema doesn't model volumes, so chapters land flat).
- Chapter body: `.p-novel__body` on individual chapter pages.
- Encoding: UTF-8 native. No CF / WAF blocking observed.

Why a recipe instead of trafilatura: the catalog page is multi-page
and lists 100 chapters per page in a structured list; trafilatura
would extract the visible page-1 chapters as flat text and lose every
href. The recipe walks all pages, preserves the chapter order, and
fetches each chapter body via the recipe path so the standard fetch
guards apply.
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
    han_digits_to_int,
)

logger = logging.getLogger(__name__)


_HOST = "ncode.syosetu.com"

# Polite throttle between consecutive chapter fetches. Syosetu doesn't
# publish an explicit rate limit; lncrawl uses a 2-task executor. We
# stay sequential at 0.2s to be safe on long crawls.
_CHAPTER_FETCH_INTERVAL = 0.2

# Hard cap on chapter count per import. Matches the 69shuba recipe.
# `n9669bk` (Mushoku Tensei) has ~286 chapters; even a long syosetu
# novel rarely passes 1000.
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
    "Accept-Language": "ja,en;q=0.8",
}


class SyosetuRecipe(BaseRecipe):
    name = "syosetu"
    # Syosetu hosts every genre — isekai, slice-of-life, romance,
    # mystery, anything. We don't presume; the user picks on the novel
    # overview page. `generic` is the safe fallback default.
    default_genre = "generic"

    def matches(self, hostname: str) -> bool:
        # ncode.syosetu.com is the canonical web-novel subdomain. The
        # other syosetu subdomains (yomou, novel18, mid) host search
        # and adult content respectively and aren't covered here.
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
                "URL is not a recognized syosetu novel. Paste a URL like "
                "https://ncode.syosetu.com/n9669bk/"
            )

        # Step 1: fetch page 1 of the catalog. This page also carries
        # the novel title + author so we don't need a separate metadata
        # fetch.
        status, body, _ct, _enc = await fetch(
            catalog_url, headers=_HEADERS, cookies=cookies,
        )
        if status >= 400:
            raise ScrapeError(
                f"syosetu returned HTTP {status} for the novel page."
            )
        page1 = BeautifulSoup(body.decode("utf-8", errors="replace"), "html.parser")

        title = _extract_title(page1)
        if not title:
            raise ScrapeError(
                "syosetu page didn't contain a recognizable title — the "
                "site layout may have changed."
            )

        # Step 2: walk the paginated TOC. Each page contributes ≤100
        # chapter rows; .c-pager__item--last on page 1 tells us how
        # many pages exist. Single-page TOCs (short novels) don't carry
        # the pager.
        all_pages: list[BeautifulSoup] = [page1]
        last_page = _extract_last_page(page1)
        if last_page > 1:
            logger.info(
                "syosetu: %r paginates over %d TOC pages",
                title, last_page,
            )
            for p in range(2, last_page + 1):
                await asyncio.sleep(_CHAPTER_FETCH_INTERVAL)
                page_url = f"{catalog_url}?p={p}"
                status, body, _ct, _enc = await fetch(
                    page_url, headers=_HEADERS, cookies=cookies,
                )
                if status >= 400:
                    raise ScrapeError(
                        f"syosetu returned HTTP {status} for TOC page {p}."
                    )
                all_pages.append(BeautifulSoup(
                    body.decode("utf-8", errors="replace"), "html.parser"
                ))

        chapter_links: list[tuple[str, str]] = []
        for soup in all_pages:
            chapter_links.extend(_extract_chapter_links(
                soup, base_url=catalog_url,
            ))

        if not chapter_links:
            raise ScrapeError(
                "syosetu chapter list was empty — the site layout may have "
                "changed, or this novel has no published chapters yet."
            )
        if len(chapter_links) > _MAX_CHAPTERS_PER_CRAWL:
            raise ScrapeError(
                f"syosetu novel has {len(chapter_links)} chapters which "
                f"exceeds the {_MAX_CHAPTERS_PER_CRAWL}-chapter import cap."
            )

        logger.info(
            "syosetu: planned %r with %d chapters from %s",
            title, len(chapter_links), catalog_url,
        )

        planned = tuple(
            PlannedChapterRef(
                chapter_num=i,
                title_zh=ch_title,
                source_url=ch_url,
                printed_num=_extract_printed_num(ch_title),
            )
            for i, (ch_title, ch_url) in enumerate(chapter_links, start=1)
        )
        # Syosetu has no cover image embedded on the catalog (lncrawl: "No
        # novel cover"). cover_url=None — the runner skips the fetch.
        return RecipePlan(
            title=title,
            source_url=catalog_url,
            catalog_url=catalog_url,
            cover_url=None,
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
        try:
            ch_status, ch_body, _ct, _enc = await fetch(
                planned.source_url, headers=_HEADERS, cookies=cookies,
            )
        except Exception as e:
            raise ScrapeError(
                f"syosetu: failed fetching "
                f"{planned.title_zh or planned.source_url}: {e}"
            ) from e
        if ch_status >= 400:
            raise ScrapeError(
                f"syosetu: HTTP {ch_status} fetching "
                f"{planned.title_zh or planned.source_url}. Aborting import."
            )
        ch_soup = BeautifulSoup(
            ch_body.decode("utf-8", errors="replace"), "html.parser"
        )
        body_text = _extract_chapter_body(ch_soup)
        if not body_text:
            logger.warning(
                "syosetu: empty body for %s (%s)",
                planned.title_zh, planned.source_url,
            )
            body_text = ""
        return FetchedChapter(
            title_zh=planned.title_zh,
            original_text=body_text,
        )


# ---- Helpers ---------------------------------------------------------------

def _normalize_to_catalog(url: str) -> str | None:
    """Canonical catalog URL for a syosetu novel.

    /<ncode>/                → /<ncode>/    (catalog root)
    /<ncode>/?p=N            → /<ncode>/    (any TOC page)
    /<ncode>/<chapter_num>/  → /<ncode>/    (individual chapter)
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname is None:
        return None
    if not (parsed.hostname == _HOST or parsed.hostname.endswith("." + _HOST)):
        return None
    # Strip trailing slash, split parts, look for the ncode segment.
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    ncode = parts[0]
    if not re.fullmatch(r"n[A-Za-z0-9]+", ncode):
        return None
    return f"{parsed.scheme}://{parsed.hostname}/{ncode}/"


def _extract_title(soup: BeautifulSoup) -> str:
    """Novel title sits in `.p-novel__title` on the catalog page. lncrawl
    uses the same selector."""
    el = soup.select_one(".p-novel__title")
    if el:
        return el.get_text(strip=True)
    return ""


def _extract_last_page(soup: BeautifulSoup) -> int:
    """`.c-pager__item--last[href]` points at `/<ncode>/?p=N`. When the
    pager is absent (single-page TOC), return 1."""
    a = soup.select_one(".c-pager__item--last")
    if not a:
        return 1
    href = a.get("href") or ""
    m = re.search(r"\?p=(\d+)", href)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 1
    return 1


def _extract_chapter_links(
    soup: BeautifulSoup, *, base_url: str,
) -> list[tuple[str, str]]:
    """Return ordered list of (title, absolute_url) from one TOC page.
    `.p-eplist__sublist a` carries the clean chapter title (no
    timestamp / edit-marker noise) and the chapter href. Volume
    headers (.p-eplist__chapter-title) sit as siblings and are
    skipped automatically because they have no nested `<a>`."""
    out: list[tuple[str, str]] = []
    for a in soup.select(".p-eplist__sublist a"):
        href = a.get("href")
        if not href:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        out.append((title, urllib.parse.urljoin(base_url, href)))
    return out


def _extract_chapter_body(soup: BeautifulSoup) -> str:
    """Pull the prose out of `.p-novel__body`. Syosetu wraps each
    paragraph in a `<p>` so a plain get_text("\\n") preserves the
    paragraph breaks. Collapse 3+ newlines down to 2 (paragraph)."""
    container = soup.select_one(".p-novel__body")
    if container is None:
        return ""
    # Strip the author's note containers if present (.p-novel__body
    # contains both prose and afterword on chapters that have one;
    # syosetu marks them via .p-novel__a-foot but the structure varies).
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


def _extract_printed_num(title: str) -> int | None:
    """Best-effort printed-number extraction. Syosetu chapter titles
    follow several conventions:

    第一話「もしかして：異世界」  → 1
    第１章 幼年期               → 1
    プロローグ                  → None (numberless, OK — falls to last+1)
    間話                        → None (interlude — same)

    We only honour the `第N話` / `第N章` patterns; the structural ordering
    from the catalog is the authoritative chapter sequence."""
    if not title:
        return None
    m = re.search(r"第\s*(\d+)\s*[話章節]", title)
    if m:
        return int(m.group(1))
    # Full-width digits → ASCII
    title_normalized = title.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = re.search(r"第\s*(\d+)\s*[話章節]", title_normalized)
    if m:
        return int(m.group(1))
    # Han numerals (一二三...). syosetu chapter titles also use
    # 第十一話 / 第一話 forms, so the shared base parser applies.
    m = re.search(r"第\s*([一二三四五六七八九十百千万零]+)\s*[話章節]", title)
    if m:
        try:
            return han_digits_to_int(m.group(1))
        except ValueError:
            return None
    return None


# Register the recipe with the dispatcher at import time.
from backend.services.scrapers import register  # noqa: E402

register(SyosetuRecipe())
