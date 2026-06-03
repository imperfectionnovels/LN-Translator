"""uukanshu recipe tests.

Fixtures captured May 2026 from www.uukanshu.cc (the .net domain is
dead). overview.html is the catalog page for book 17474 (斗羅大陸V重生
唐三), chapter.html is the prologue.
"""

from __future__ import annotations

import pathlib
from urllib.parse import urlparse

import pytest

from backend.db import init_db, open_conn
from backend.services.scrapers.base import RecipeResult, extract_printed_num_cn
from backend.services.scrapers.uukanshu import (
    UukanshuRecipe,
    _extract_chapter_body,
    _extract_chapter_links,
    _extract_cover_url,
    _extract_title,
    _normalize_to_catalog,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "scrapers" / "uukanshu"


def _load(name: str):
    from bs4 import BeautifulSoup
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


# ---- Hostname matching ------------------------------------------------------

def test_matches_canonical_host():
    r = UukanshuRecipe()
    assert r.matches("uukanshu.cc")


def test_matches_subdomain():
    """tw./m./www. all share the same site structure on uukanshu — the
    recipe handles them via endswith."""
    r = UukanshuRecipe()
    assert r.matches("www.uukanshu.cc")
    assert r.matches("tw.uukanshu.cc")


def test_does_not_match_legacy_dead_domain():
    """uukanshu.net stopped resolving. We don't claim it any more —
    a user pasting a .net URL will fall through to the generic scraper,
    which surfaces a clear DNS error."""
    r = UukanshuRecipe()
    assert not r.matches("uukanshu.net")
    assert not r.matches("www.uukanshu.net")


# ---- URL normalization ------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    (
        "https://www.uukanshu.cc/book/17474/",
        "https://www.uukanshu.cc/book/17474/",
    ),
    (
        "https://www.uukanshu.cc/book/17474",  # no trailing slash
        "https://www.uukanshu.cc/book/17474/",
    ),
    (
        "https://www.uukanshu.cc/book/17474/10328712.html",
        "https://www.uukanshu.cc/book/17474/",
    ),
])
def test_normalize_to_catalog_accepts_known_shapes(url, expected):
    assert _normalize_to_catalog(url) == expected


def test_normalize_rejects_non_book_paths():
    assert _normalize_to_catalog("https://www.uukanshu.cc/") is None
    assert _normalize_to_catalog("https://www.uukanshu.cc/random/page") is None


def test_normalize_rejects_other_hosts():
    assert _normalize_to_catalog("https://example.com/book/17474/") is None


# ---- Selectors against captured fixtures -----------------------------------

def test_extract_title_against_fixture():
    soup = _load("overview.html")
    title = _extract_title(soup)
    # Captured book is 斗羅大陸V重生唐三.
    assert "斗羅大陸" in title


def test_extract_cover_url_against_fixture():
    soup = _load("overview.html")
    url = _extract_cover_url(soup, base_url="https://www.uukanshu.cc/book/17474/")
    assert url is not None
    # image.uukanshu.cc hosts the covers.
    assert "uukanshu.cc" in url
    assert url.endswith(".jpg")


def test_extract_chapter_links_against_fixture():
    soup = _load("overview.html")
    links = _extract_chapter_links(
        soup, base_url="https://www.uukanshu.cc/book/17474/",
    )
    # Captured book has 1184 numbered chapters + 1 prologue (引子) = 1185.
    # We tolerate a small floor in case the live capture caught the
    # site mid-update; the important assertion is that the count
    # massively exceeds anything a navigation widget would have.
    assert len(links) > 1000
    # First entry is the prologue (引子) — ascending order, no reversal.
    first_title, _first_url = links[0]
    assert first_title == "引子"
    # Last entry is the final chapter — 第1184章.
    last_title, _last_url = links[-1]
    assert last_title.startswith("第1184章")


def test_extract_chapter_links_filters_anchors_without_href():
    """If the captured layout ever shifts to anchors without `href`
    (e.g. JS-only links), we drop them rather than producing
    (title, None) pairs."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<dl class="book chapterlist">'
        '<dd><a href="/book/1/1.html">第一章</a></dd>'
        '<dd><a>第二章 hrefless</a></dd>'
        '<dd><a href="/book/1/3.html">第三章</a></dd>'
        '</dl>',
        "html.parser",
    )
    links = _extract_chapter_links(
        soup, base_url="https://www.uukanshu.cc/book/1/",
    )
    assert len(links) == 2
    assert [t for t, _ in links] == ["第一章", "第三章"]


def test_extract_chapter_body_against_fixture():
    soup = _load("chapter.html")
    text = _extract_chapter_body(soup)
    assert len(text) > 300  # real prose, not an empty selector miss
    # The captured chapter is the prologue (引子) of 斗羅大陸V — opens with
    # "廣袤無垠的太空之中" (in the vastness of space).
    assert "太空" in text or "宇宙" in text


# ---- printed_num + recipe contract -----------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("第1章 出發", 1),
    ("第123章 中段", 123),
    ("第一章 開始", 1),
    ("第十章 平凡", 10),
    ("引子", None),
    ("番外", None),
])
def test_extract_printed_num(title, expected):
    assert extract_printed_num_cn(title) == expected


def test_recipe_declares_xianxia_default_genre():
    assert UukanshuRecipe.default_genre == "xianxia"


def test_recipe_is_registered_with_dispatcher():
    from backend.services.scrapers import dispatch_for_url
    r = dispatch_for_url("https://www.uukanshu.cc/book/17474/")
    assert isinstance(r, UukanshuRecipe)


# ---- End-to-end recipe run via mocked fetcher ------------------------------

@pytest.mark.asyncio
async def test_recipe_imports_with_mocked_fetcher(monkeypatch):
    """Mock the fetcher with the captured fixtures and run the recipe
    via the atomic_import_via_recipe helper. Assert the novel + N chapters
    land in the DB with the expected title, genre, source_language.

    Patches _CHAPTER_FETCH_INTERVAL to 0 — the 0.2s polite throttle is
    a live-site nicety; with 1000+ mocked chapters it would balloon CI
    by ~4 minutes for no test signal."""
    from backend.services.scrapers import uukanshu as uu_mod
    from backend.services.scrapers.uukanshu import UukanshuRecipe
    from backend.tests._recipe_atomic_helper import atomic_import_via_recipe
    monkeypatch.setattr(uu_mod, "_CHAPTER_FETCH_INTERVAL", 0)

    overview_bytes = (FIXTURES / "overview.html").read_bytes()
    chapter_bytes = (FIXTURES / "chapter.html").read_bytes()

    # A 1-pixel PNG so the cover write succeeds (the fixture cover URL
    # is image.uukanshu.cc/.../...jpg; we redirect to our PNG).
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
        if parsed.path == "/book/17474/":
            return 200, overview_bytes, "text/html; charset=utf-8", "utf-8"
        if parsed.path.startswith("/book/17474/") and parsed.path.endswith(".html"):
            return 200, chapter_bytes, "text/html; charset=utf-8", "utf-8"
        # Cover image (under image.uukanshu.cc).
        if parsed.hostname and "uukanshu" in parsed.hostname and parsed.path.endswith(".jpg"):
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
            UukanshuRecipe(),
            "https://www.uukanshu.cc/book/17474/",
            conn,
            cookies=None,
            fetch=fake_fetch,
        )

    assert isinstance(result, RecipeResult)
    assert "斗羅大陸" in result.title
    assert result.added_chapters > 1000

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT genre, source_language FROM novels WHERE id = ?",
            (result.novel_id,),
        )
        novel = await cur.fetchone()
    assert novel["genre"] == "xianxia"
    assert novel["source_language"] == "zh"
