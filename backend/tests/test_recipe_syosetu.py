"""Syosetu recipe tests.

Pure-function tests use the captured HTML fixtures under
backend/tests/fixtures/scrapers/syosetu/. The end-to-end test mocks the
fetcher with those same fixtures so the test runs offline.
"""

from __future__ import annotations

import pathlib
from urllib.parse import urlparse

import pytest

from backend.db import init_db, open_conn
from backend.services.scrapers.base import RecipeResult
from backend.services.scrapers.syosetu import (
    SyosetuRecipe,
    _extract_chapter_body,
    _extract_chapter_links,
    _extract_last_page,
    _extract_printed_num,
    _extract_title,
    _normalize_to_catalog,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "scrapers" / "syosetu"


# ---- Helpers ---------------------------------------------------------------

def _load_overview_soup():
    from bs4 import BeautifulSoup
    html = (FIXTURES / "overview.html").read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


def _load_chapter_soup():
    from bs4 import BeautifulSoup
    html = (FIXTURES / "chapter.html").read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


# ---- Hostname matching ------------------------------------------------------

def test_matches_canonical_host():
    r = SyosetuRecipe()
    assert r.matches("ncode.syosetu.com")


def test_matches_subdomains_of_ncode():
    r = SyosetuRecipe()
    assert r.matches("m.ncode.syosetu.com")


def test_does_not_match_unrelated_syosetu_subdomains():
    """yomou.syosetu.com (search) and novel18.syosetu.com (adult) are
    not covered by this recipe."""
    r = SyosetuRecipe()
    assert not r.matches("yomou.syosetu.com")
    assert not r.matches("novel18.syosetu.com")
    assert not r.matches("syosetu.com")


def test_does_not_match_unrelated_hosts():
    r = SyosetuRecipe()
    assert not r.matches("example.com")
    assert not r.matches("ncode.syosetu.com.evil.example")


# ---- URL normalization ------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    (
        "https://ncode.syosetu.com/n9669bk/",
        "https://ncode.syosetu.com/n9669bk/",
    ),
    (
        "https://ncode.syosetu.com/n9669bk",  # no trailing slash
        "https://ncode.syosetu.com/n9669bk/",
    ),
    (
        "https://ncode.syosetu.com/n9669bk/?p=3",
        "https://ncode.syosetu.com/n9669bk/",
    ),
    (
        "https://ncode.syosetu.com/n9669bk/100/",
        "https://ncode.syosetu.com/n9669bk/",
    ),
])
def test_normalize_to_catalog_accepts_expected_shapes(url, expected):
    assert _normalize_to_catalog(url) == expected


def test_normalize_rejects_non_ncode_paths():
    """A URL where the first path segment isn't an `n<alnum>` slug isn't
    a syosetu novel — return None so the recipe raises ScrapeError."""
    assert _normalize_to_catalog("https://ncode.syosetu.com/") is None
    assert _normalize_to_catalog("https://ncode.syosetu.com/random/page") is None


def test_normalize_rejects_other_hosts():
    assert _normalize_to_catalog("https://example.com/n9669bk/") is None


# ---- Selectors against captured fixtures -----------------------------------

def test_extract_title_against_fixture():
    soup = _load_overview_soup()
    title = _extract_title(soup)
    # Captured May 2026 — Mushoku Tensei.
    assert "無職転生" in title


def test_extract_last_page_against_fixture():
    """Mushoku Tensei spans 3 TOC pages — the captured page 1 carries
    `.c-pager__item--last` pointing at /n9669bk/?p=3."""
    soup = _load_overview_soup()
    assert _extract_last_page(soup) == 3


def test_extract_chapter_links_against_fixture():
    soup = _load_overview_soup()
    links = _extract_chapter_links(soup, base_url="https://ncode.syosetu.com/n9669bk/")
    # Page 1 carries 100 chapter rows (verified empirically).
    assert len(links) == 100
    # First chapter is the prologue.
    first_title, first_url = links[0]
    assert first_title == "プロローグ"
    assert first_url == "https://ncode.syosetu.com/n9669bk/1/"
    # Volume headers don't bleed in (they have no nested <a>, so the
    # .p-eplist__sublist selector skips them naturally).
    for title, _ in links:
        # Volume header text starts with 第N章 followed by a name — but
        # chapter titles use 第N話 (different counter word). Any 第N章
        # entries would indicate a volume header leak.
        if title.startswith("第") and "章" in title:
            # 第N章 in a chapter title would be unusual but not strictly
            # forbidden; we assert volume-header text doesn't appear by
            # checking that none of the captured entries match the
            # known volume-header pattern '第N章 [name]' without quotes.
            assert "「" in title or "話" in title, (
                f"suspected volume-header leak: {title!r}"
            )


def test_extract_chapter_body_against_fixture():
    soup = _load_chapter_soup()
    text = _extract_chapter_body(soup)
    assert len(text) > 1000  # real prose, not an empty selector miss
    # The captured chapter is the prologue of Mushoku Tensei; the
    # protagonist is 34 years old in the opening line.
    assert "34歳" in text or "三十四" in text


# ---- printed_num extraction -------------------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("第一話「もしかして：異世界」", 1),
    ("第二十話「ターニングポイント」", 20),
    ("第１章 幼年期", 1),         # full-width digit
    ("第100話 タイトル", 100),
    ("プロローグ", None),
    ("間話「後日談」", None),
])
def test_extract_printed_num(title, expected):
    assert _extract_printed_num(title) == expected


# ---- Recipe declares its surface contract -----------------------------------

def test_recipe_declares_generic_default_genre():
    """Syosetu hosts every genre — the recipe should default to
    `generic` and let the user override on the novel page."""
    assert SyosetuRecipe.default_genre == "generic"


def test_recipe_is_registered_with_dispatcher():
    """Importing the module should self-register so dispatch_for_url
    finds it."""
    from backend.services.scrapers import dispatch_for_url
    r = dispatch_for_url("https://ncode.syosetu.com/n9669bk/")
    assert isinstance(r, SyosetuRecipe)


# ---- End-to-end recipe run via mocked fetcher ------------------------------

@pytest.mark.asyncio
async def test_recipe_imports_novel_with_mocked_fetcher(monkeypatch):
    """Mock the fetcher with the captured overview + chapter fixtures.
    Assert the novel and 100 chapters land in the DB with the right
    title and source_language.

    Patches _CHAPTER_FETCH_INTERVAL to 0 — the 0.2s polite throttle is
    a live-site nicety, not load-bearing for the test."""
    from backend.services.scraper import scrape_url
    from backend.services.scrapers import syosetu as syo_mod
    monkeypatch.setattr(syo_mod, "_CHAPTER_FETCH_INTERVAL", 0)

    overview_bytes = (FIXTURES / "overview.html").read_bytes()
    chapter_bytes = (FIXTURES / "chapter.html").read_bytes()

    fetched_urls: list[str] = []

    async def fake_fetch(url, *, headers=None, cookies=None, **kwargs):
        fetched_urls.append(url)
        path = urlparse(url).path
        query = urlparse(url).query
        # Catalog page 1.
        if path == "/n9669bk/" and not query:
            return 200, overview_bytes, "text/html; charset=utf-8", "utf-8"
        # TOC pages 2 + 3: serve the same fixture so chapter rows
        # accumulate. This is a fixture limitation — in real use each
        # ?p=N page has a different 100-chapter slice; here we cap the
        # last_page at 1 by mutating the captured pager out, see below.
        if path == "/n9669bk/" and query.startswith("p="):
            # Return an empty-eplist version so we don't triple-count.
            return 200, _strip_eplist(overview_bytes), "text/html; charset=utf-8", "utf-8"
        # Individual chapter pages: every chapter url path matches
        # /n9669bk/<num>/. Serve the captured chapter body for all of
        # them — the test asserts shape, not per-chapter content.
        if path.startswith("/n9669bk/") and path.endswith("/"):
            return 200, chapter_bytes, "text/html; charset=utf-8", "utf-8"
        raise AssertionError(f"unexpected fetch URL: {url}")

    from backend.services import scraper as scraper_mod
    real_fetch = scraper_mod._fetch_one
    real_resolve = scraper_mod._resolve_and_validate
    scraper_mod._fetch_one = fake_fetch

    async def fake_resolve(host):
        return None

    scraper_mod._resolve_and_validate = fake_resolve

    try:
        await init_db()
        async with open_conn() as conn:
            for t in ("chapters", "novels"):
                try:
                    await conn.execute(f"DELETE FROM {t}")
                except Exception:
                    pass
            await conn.commit()

            result = await scrape_url(
                "https://ncode.syosetu.com/n9669bk/",
                cookies=None,
                conn=conn,
            )
    finally:
        scraper_mod._fetch_one = real_fetch
        scraper_mod._resolve_and_validate = real_resolve

    assert isinstance(result, RecipeResult)
    assert "無職転生" in result.title
    # The fixture page 1 has 100 chapters. Pages 2 + 3 (after _strip_eplist)
    # contribute 0. Total = 100.
    assert result.added_chapters == 100
    assert result.first_chapter_num == 1
    assert result.cover_extracted is False  # syosetu has no cover

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT source_language, genre FROM novels WHERE id = ?",
            (result.novel_id,),
        )
        novel = await cur.fetchone()
    assert novel["source_language"] == "ja"
    assert novel["genre"] == "generic"


def _strip_eplist(html_bytes: bytes) -> bytes:
    """Return the captured HTML with `.p-eplist__sublist` rows removed,
    so a mocked TOC page-2/3 fetch contributes zero chapters. Used by
    the test fixture to keep the chapter count predictable when the
    same overview is reused across pagination."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_bytes.decode("utf-8", errors="replace"), "html.parser")
    for el in soup.select(".p-eplist__sublist"):
        el.decompose()
    return str(soup).encode("utf-8")
