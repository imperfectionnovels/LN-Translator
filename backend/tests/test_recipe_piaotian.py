"""piaotian recipe tests.

Fixtures captured May 2026 from www.piaotia.com — book 1/1705 (傲世九重
天). The chapter fixture exercises the GetFont() string-replace hack
since piaotian doesn't wrap chapter prose in any container element.
"""

from __future__ import annotations

import pathlib
from urllib.parse import urlparse

import pytest

from backend.db import init_db, open_conn
from backend.services.scrapers.base import RecipeResult
from backend.services.scrapers.piaotian import (
    PiaotianRecipe,
    _construct_cover_url,
    _extract_chapter_body,
    _extract_chapter_links,
    _extract_printed_num,
    _extract_title,
    _normalize_to_catalog,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "scrapers" / "piaotian"


def _load_soup(name: str):
    from bs4 import BeautifulSoup
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


# ---- Hostname matching ------------------------------------------------------

def test_matches_known_mirrors():
    r = PiaotianRecipe()
    assert r.matches("piaotia.com")
    assert r.matches("www.piaotia.com")
    assert r.matches("ptwxz.com")
    assert r.matches("piaotian.cc")
    assert r.matches("piaotian.com")


def test_does_not_match_unrelated_hosts():
    r = PiaotianRecipe()
    assert not r.matches("piaotia.net")
    assert not r.matches("example.com")


# ---- URL normalization ------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    (
        "https://www.piaotia.com/bookinfo/1/1705.html",
        "https://www.piaotia.com/html/1/1705/",
    ),
    (
        "https://www.piaotia.com/html/1/1705/",
        "https://www.piaotia.com/html/1/1705/",
    ),
    (
        "https://www.piaotia.com/html/1/1705/index.html",
        "https://www.piaotia.com/html/1/1705/",
    ),
    (
        "https://www.piaotia.com/html/1/1705/762992.html",
        "https://www.piaotia.com/html/1/1705/",
    ),
    (
        "https://www.piaotia.com/html/1/1705",  # missing trailing slash
        "https://www.piaotia.com/html/1/1705/",
    ),
])
def test_normalize_to_catalog(url, expected):
    assert _normalize_to_catalog(url) == expected


def test_normalize_rejects_unrecognized_paths():
    assert _normalize_to_catalog("https://www.piaotia.com/") is None
    assert _normalize_to_catalog("https://www.piaotia.com/random/page") is None


def test_normalize_rejects_other_hosts():
    assert _normalize_to_catalog("https://example.com/html/1/1705/") is None


# ---- Cover URL --------------------------------------------------------------

def test_construct_cover_url_from_catalog():
    """Cover paths are deterministic on piaotian — built from the
    catalog's path IDs."""
    assert _construct_cover_url(
        "https://www.piaotia.com/html/1/1705/"
    ) == "https://www.piaotia.com/files/article/image/1/1705/1705s.jpg"


def test_construct_cover_url_returns_none_for_non_catalog():
    assert _construct_cover_url("https://www.piaotia.com/bookinfo/1/1705.html") is None


# ---- Selectors against captured fixtures -----------------------------------

def test_extract_title_strips_latest_chapters_suffix():
    soup = _load_soup("overview.html")
    title = _extract_title(soup)
    # Captured title is "傲世九重天最新章节" — the recipe strips
    # "最新章节" leaving 傲世九重天.
    assert title == "傲世九重天"


def test_extract_chapter_links_against_fixture():
    soup = _load_soup("overview.html")
    links = _extract_chapter_links(
        soup, base_url="https://www.piaotia.com/html/1/1705/",
    )
    # Captured page lists every chapter (the novel ran 2000+ chapters).
    assert len(links) > 1000
    first_title, first_url = links[0]
    assert first_title.startswith("第一章") or first_title.startswith("第1章")
    assert first_url.startswith("https://www.piaotia.com/html/1/1705/")


def test_extract_chapter_body_uses_getfont_hack():
    """The chapter page has no div#content; the recipe string-replaces
    the GetFont() script tag with <div id=content> so BS4 captures the
    prose. Verify against the captured fixture."""
    html = (FIXTURES / "chapter.html").read_text(encoding="utf-8")
    text = _extract_chapter_body(html)
    assert len(text) > 1000  # real prose, not an empty selector miss
    # Opening line of 第一章 in the captured fixture mentions 九重天.
    assert "九重天" in text or "风雷台" in text


def test_extract_chapter_body_returns_empty_when_marker_missing():
    """When the GetFont() shim isn't in the HTML the recipe falls back
    to the regex; if that also misses, return empty string and log a
    warning rather than crash."""
    text = _extract_chapter_body("<html><body>no shim here</body></html>")
    assert text == ""


# ---- printed_num + recipe contract -----------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("第1章 出发", 1),
    ("第123章 中段", 123),
    ("第一章 开始", 1),
    ("第十章 平凡", 10),
    ("完本感言！", None),
    ("番外", None),
])
def test_extract_printed_num(title, expected):
    assert _extract_printed_num(title) == expected


def test_recipe_declares_xianxia_default_genre():
    assert PiaotianRecipe.default_genre == "xianxia"


def test_recipe_is_registered_with_dispatcher():
    from backend.services.scrapers import dispatch_for_url
    r = dispatch_for_url("https://www.piaotia.com/html/1/1705/")
    assert isinstance(r, PiaotianRecipe)


# ---- End-to-end recipe run via mocked fetcher ------------------------------

@pytest.mark.asyncio
async def test_recipe_imports_with_mocked_fetcher(monkeypatch):
    """Run the recipe against the captured catalog + chapter fixtures.
    Patch _CHAPTER_FETCH_INTERVAL to 0 so the test doesn't sleep for
    ~9 minutes (the captured fixture has 2000+ chapters)."""
    from backend.services.scrapers import piaotian as pt_mod
    from backend.services.scrapers.piaotian import PiaotianRecipe
    from backend.tests._recipe_atomic_helper import atomic_import_via_recipe
    monkeypatch.setattr(pt_mod, "_CHAPTER_FETCH_INTERVAL", 0)

    overview_bytes = (FIXTURES / "overview.html").read_text(encoding="utf-8").encode("gbk", errors="replace")
    chapter_bytes = (FIXTURES / "chapter.html").read_text(encoding="utf-8").encode("gbk", errors="replace")

    png_1x1 = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xfc\xcf\xc0P\x0f\x00\x04\x85\x01\x80"
        b"\x84\x90\x97\xab"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    async def fake_fetch(url, *, headers=None, cookies=None, **kwargs):
        parsed = urlparse(url)
        if parsed.path == "/html/1/1705/":
            return 200, overview_bytes, "text/html; charset=gbk", "gbk"
        if parsed.path.startswith("/html/1/1705/") and parsed.path.endswith(".html"):
            return 200, chapter_bytes, "text/html; charset=gbk", "gbk"
        if parsed.path.startswith("/files/article/image/") and parsed.path.endswith(".jpg"):
            return 200, png_1x1, "image/png", "utf-8"
        raise AssertionError(f"unexpected fetch URL: {url}")

    await init_db()
    async with open_conn() as conn:
        for t in ("chapters", "novels"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await conn.commit()
        result = await atomic_import_via_recipe(
            PiaotianRecipe(),
            "https://www.piaotia.com/bookinfo/1/1705.html",
            conn,
            cookies=None,
            fetch=fake_fetch,
        )

    assert isinstance(result, RecipeResult)
    assert result.title == "傲世九重天"
    assert result.added_chapters > 1000

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT genre, source_language FROM novels WHERE id = ?",
            (result.novel_id,),
        )
        novel = await cur.fetchone()
    assert novel["genre"] == "xianxia"
    assert novel["source_language"] == "zh"
