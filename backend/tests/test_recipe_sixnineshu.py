"""69shuba recipe end-to-end test.

Mocks the fetcher to return canned 69shuba-shaped HTML (overview page +
chapter-list page + N chapter bodies), runs the recipe through
`scrape_url`, and asserts the resulting novel has the expected chapters
in order with the expected title and cover.

GBK encoding: the canned bytes are actually encoded as GBK so the recipe's
decode path is exercised end-to-end.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from backend.db import init_db, open_conn
from backend.services.parser import ParsedChapter  # noqa: F401  (kept for type clarity)
from backend.services.scrapers.base import RecipeResult
from backend.services.scrapers.sixnineshu import (
    _extract_chapter_body,
    _extract_chapter_links,
    _extract_cover_url,
    _extract_printed_num,
    _extract_title,
    _han_digits_to_int,
    _normalize_to_chapter_list,
    _to_overview_url,
)

# ---- Helpers: build canned 69shuba HTML in GBK ------------------------------

def _overview_html(title: str, cover_relpath: str) -> bytes:
    """Mimic the 69shuba /book/N.htm overview page. Carries cover +
    title only — the real catalog lives at /<N>/. Encoded as GBK so the
    recipe's _decode_gbk path runs."""
    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="gbk">
<title>{title}_69书吧</title>
</head><body>
<div class="bookimg2"><img src="{cover_relpath}"></div>
<div class="booknav2"><h1>{title}</h1></div>
<div class="qustime"><ul>
<li><a href="/A12345/recent.html">最新章节 占位</a></li>
</ul></div>
</body></html>"""
    return html.encode("gbk")


def _catalog_html(book_id: str, chapter_links: list[tuple[int, str]]) -> bytes:
    """Mimic the 69shuba /<id>/ catalog page. Lists chapters
    **latest-first** (the recipe reverses)."""
    rows_desc = "\n".join(
        f'<li><a href="/A12345/{cid}.html">第{cid}章 {cname}</a></li>'
        for cid, cname in reversed(chapter_links)
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="gbk"><title>目录 - {book_id}</title></head>
<body>
<div id="catalog"><ul>
{rows_desc}
</ul></div>
</body></html>"""
    return html.encode("gbk")


def _chapter_html(printed_num: int, title: str, body_paragraphs: list[str]) -> bytes:
    """Mimic a 69shuba chapter page. Body wrapped in div.txtnav with
    the title h1, the txtinfo div, and the txtright nav that the
    recipe strips."""
    paragraphs_html = "<br><br>".join(body_paragraphs)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="gbk"><title>第{printed_num}章 {title}</title></head>
<body>
<div class="txtnav">
<h1>第{printed_num}章 {title}</h1>
<div class="txtinfo">作者：佚名 · 字数：1200</div>
{paragraphs_html}
<div id="txtright">上一章 · 下一章</div>
</div>
</body></html>"""
    return html.encode("gbk")


# Tiny valid PNG so the cover write actually succeeds.
_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xfc\xcf\xc0P\x0f\x00\x04\x85\x01\x80"
    b"\x84\x90\x97\xab"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---- Pure-function tests ---------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    # /book/<numeric>.htm is a metadata-only overview; the real catalog
    # lives at /<numeric>/ (verified empirically May 2026 — see
    # _normalize_to_chapter_list docstring).
    (
        "https://www.69shuba.com/book/88724.htm",
        "https://www.69shuba.com/88724/",
    ),
    (
        "https://www.69shuba.com/txt/A43616.htm",
        "https://www.69shuba.com/A43616/",
    ),
    (
        "https://www.69shuba.com/txt/88724.htm",
        "https://www.69shuba.com/88724/",
    ),
    (
        "https://www.69shuba.com/A43616/",
        "https://www.69shuba.com/A43616/",
    ),
    (
        "https://www.69shuba.com/A43616/12345.html",
        "https://www.69shuba.com/A43616/",
    ),
    # /txt/<book>/<chapter_id> (no extension) — current 69shuba chapter
    # URL shape; normalize back to the catalog.
    (
        "https://www.69shuba.com/txt/88724/41028833",
        "https://www.69shuba.com/88724/",
    ),
])
def test_normalize_to_chapter_list_matches_lncrawl_patterns(url, expected):
    assert _normalize_to_chapter_list(url) == expected


def test_normalize_returns_none_for_unrecognized_path():
    """A weird path that doesn't match any known shape → None →
    recipe raises ScrapeError."""
    assert _normalize_to_chapter_list("https://www.69shuba.com/random/page") is None


def test_to_overview_url_only_matches_book_form():
    assert _to_overview_url("https://www.69shuba.com/book/88724.htm") == \
        "https://www.69shuba.com/book/88724.htm"
    assert _to_overview_url("https://www.69shuba.com/A43616/") is None


@pytest.mark.parametrize("title, expected", [
    ("第123章 出发", 123),
    ("第  4567  章 标题", 4567),
    ("第一百二十三章 你好", 123),
    ("第十章 简单", 10),
    ("无章节号", None),
])
def test_extract_printed_num_recognizes_chinese_chapter_numbers(title, expected):
    assert _extract_printed_num(title) == expected


@pytest.mark.parametrize("han, expected", [
    ("一", 1),
    ("十", 10),
    ("二十", 20),
    ("一百", 100),
    ("一百二十三", 123),
    ("一千零一", 1001),
    ("一万", 10000),
])
def test_han_digits_to_int_basic_cases(han, expected):
    assert _han_digits_to_int(han) == expected


# ---- End-to-end recipe run via mocked fetcher ------------------------------

@pytest.mark.asyncio
async def test_recipe_imports_a_full_novel_with_cover(monkeypatch):
    """Mock a 5-chapter 69shuba novel + cover. Run the recipe through
    scrape_url. Assert: novel exists, has 5 chapters in order with
    correct printed_num, cover bytes were written, cover_source='url'.

    Patches _CHAPTER_FETCH_INTERVAL to 0 — the live recipe sleeps 1s
    between chapters to stay under Cloudflare's rate limit, but in
    tests the mocked fetcher returns instantly and the sleep is pure
    overhead."""
    from backend.services.scraper import scrape_url
    from backend.services.scrapers import sixnineshu as six_mod
    monkeypatch.setattr(six_mod, "_CHAPTER_FETCH_INTERVAL", 0)

    chapter_links = [(i, f"章节{i}") for i in range(1, 6)]
    chapter_bodies = {
        i: _chapter_html(
            i,
            f"章节{i}",
            [
                f"第{i}章的开头。这是一个测试段落。",
                f"第{i}章的中间。又一个段落。",
                f"第{i}章的结束。完。",
            ],
        )
        for i in range(1, 6)
    }
    overview_bytes = _overview_html(
        title="测试小说",
        cover_relpath="/imgs/A12345.jpg",
    )
    catalog_bytes = _catalog_html("99999", chapter_links=chapter_links)

    async def fake_fetch(url, *, headers=None, cookies=None, **kwargs):
        path = urlparse(url).path
        # Overview (metadata + cover).
        if path == "/book/99999.htm":
            return 200, overview_bytes, "text/html; charset=gbk", "gbk"
        # Catalog (chapter list) — the recipe transforms /book/99999.htm
        # to /99999/ via _normalize_to_chapter_list, so this is a
        # separate fetch.
        if path == "/99999/":
            return 200, catalog_bytes, "text/html; charset=gbk", "gbk"
        # Individual chapter pages.
        if path.startswith("/A12345/") and path.endswith(".html"):
            stem = path.rsplit("/", 1)[-1].split(".")[0]
            if not stem.isdigit():
                # The qustime placeholder href in the overview shouldn't
                # ever be fetched, but fail loudly if it is.
                raise AssertionError(f"unexpected non-numeric chapter id: {stem}")
            ch_id = int(stem)
            return 200, chapter_bodies[ch_id], "text/html; charset=gbk", "gbk"
        # Cover image.
        if path == "/imgs/A12345.jpg":
            return 200, _PNG_1x1, "image/png", "utf-8"
        raise AssertionError(f"unexpected fetch URL in test: {url}")

    # Monkeypatch scrape_url's fetch_one with our canned fetcher.
    from backend.services import scraper as scraper_mod
    real_fetch = scraper_mod.fetch_one
    scraper_mod.fetch_one = fake_fetch
    # Also bypass DNS for the SSRF guard at the start of scrape_url
    # (the recipe is dispatched before the trafilatura path's fetch, but
    # scrape_url still SSRF-validates the initial URL).
    real_resolve = scraper_mod._resolve_and_validate

    async def fake_resolve(host):
        return None

    scraper_mod._resolve_and_validate = fake_resolve

    try:
        await init_db()
        async with open_conn() as conn:
            # Clean slate so we know which novel id to inspect.
            for t in ("chapters", "novels"):
                try:
                    await conn.execute(f"DELETE FROM {t}")
                except Exception:
                    pass
            await conn.commit()

            result = await scrape_url(
                "https://www.69shuba.com/book/99999.htm",
                cookies=None,
                conn=conn,
            )
    finally:
        scraper_mod.fetch_one = real_fetch
        scraper_mod._resolve_and_validate = real_resolve

    assert isinstance(result, RecipeResult)
    assert result.title == "测试小说"
    assert result.added_chapters == 5
    assert result.first_chapter_num == 1
    assert result.cover_extracted is True

    # Inspect the DB to confirm chapters landed in order with the right
    # printed numbers and decoded titles.
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT chapter_num, title_zh FROM chapters WHERE novel_id = ? "
            "ORDER BY chapter_num",
            (result.novel_id,),
        )
        rows = await cur.fetchall()
        # And the cover_source column.
        cur = await conn.execute(
            "SELECT cover_source, cover_image_path, title FROM novels WHERE id = ?",
            (result.novel_id,),
        )
        novel_row = await cur.fetchone()

    assert len(rows) == 5
    for i, row in enumerate(rows, start=1):
        assert row["chapter_num"] == i
        assert f"第{i}章" in row["title_zh"]

    assert novel_row["title"] == "测试小说"
    assert novel_row["cover_source"] == "url"
    assert novel_row["cover_image_path"] is not None

    # 2026-05-25: recipe stamps default_genre + auto-detected
    # source_language so the novel doesn't land with NULL on either.
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT genre, source_language FROM novels WHERE id = ?",
            (result.novel_id,),
        )
        meta = await cur.fetchone()
    assert meta["genre"] == "xianxia"           # recipe's default_genre
    assert meta["source_language"] == "zh"      # detected from CJK ideographs


@pytest.mark.asyncio
async def test_recipe_retries_chapter_fetch_on_cf_403(monkeypatch):
    """69shuba's Cloudflare rate-limits sustained chapter fetches with
    transient 403s; the recipe's backoff helper retries with exponential
    sleeps before giving up.

    Mock a chapter that returns 403 twice then 200. Patch the backoff
    intervals to zero so the test doesn't actually sleep. Assert the
    recipe still completes the import.

    Regression for the May 2026 CF rate-limit bug (chapter 33/1424 of
    book 88724 hit a transient 403 and the recipe aborted the entire
    1424-chapter crawl over one transient failure)."""
    from backend.services.scrapers import sixnineshu as six_mod
    monkeypatch.setattr(six_mod, "_CHAPTER_FETCH_INTERVAL", 0)
    monkeypatch.setattr(six_mod, "_CHAPTER_RETRY_BACKOFFS", (0, 0, 0))

    # 5-chapter novel. Chapter 3 returns 403 twice before succeeding.
    chapter_links = [(i, f"章节{i}") for i in range(1, 6)]
    chapter_bodies = {
        i: _chapter_html(i, f"章节{i}", [f"第{i}章。", "段落。"])
        for i in range(1, 6)
    }
    overview_bytes = _overview_html(title="重试小说", cover_relpath="/imgs/x.jpg")
    catalog_bytes = _catalog_html("99998", chapter_links=chapter_links)

    chapter3_403_remaining = [2]  # mutable counter for the retry tracker

    async def fake_fetch(url, *, headers=None, cookies=None, **kwargs):
        from urllib.parse import urlparse
        path = urlparse(url).path
        if path == "/book/99998.htm":
            return 200, overview_bytes, "text/html; charset=gbk", "gbk"
        if path == "/99998/":
            return 200, catalog_bytes, "text/html; charset=gbk", "gbk"
        if path.startswith("/A12345/") and path.endswith(".html"):
            ch_id = int(path.rsplit("/", 1)[-1].split(".")[0])
            # Chapter 3 simulates the CF rate-limit: 403 twice, then 200.
            if ch_id == 3 and chapter3_403_remaining[0] > 0:
                chapter3_403_remaining[0] -= 1
                return 403, b"<html>blocked by cf</html>", "text/html", "utf-8"
            return 200, chapter_bodies[ch_id], "text/html; charset=gbk", "gbk"
        if path == "/imgs/x.jpg":
            return 200, _PNG_1x1, "image/png", "utf-8"
        raise AssertionError(f"unexpected fetch URL: {url}")

    from backend.services import scraper as scraper_mod
    real_fetch = scraper_mod.fetch_one
    real_resolve = scraper_mod._resolve_and_validate
    scraper_mod.fetch_one = fake_fetch

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
            from backend.services.scraper import scrape_url
            result = await scrape_url(
                "https://www.69shuba.com/book/99998.htm",
                cookies=None,
                conn=conn,
            )
    finally:
        scraper_mod.fetch_one = real_fetch
        scraper_mod._resolve_and_validate = real_resolve

    # All 5 chapters land — the retry recovered chapter 3.
    assert result.added_chapters == 5
    # Confirm the mock actually exercised the retry path (counter
    # reached 0 means it returned 403 the configured number of times).
    assert chapter3_403_remaining[0] == 0


def test_recipe_declares_default_genre() -> None:
    """The recipe surface contract: default_genre must be set on the
    recipe class. The route layer passes it through to atomic_create_novel
    when the user doesn't override."""
    from backend.services.scrapers.sixnineshu import SixNineShuRecipe
    assert SixNineShuRecipe.default_genre == "xianxia"


@pytest.mark.asyncio
async def test_recipe_chapter_body_extraction_strips_navigation():
    """The recipe's _extract_chapter_body decomposes h1 / txtinfo /
    txtright before returning text. Verify the noise is gone and the
    prose remains."""
    from bs4 import BeautifulSoup

    html = """<html><body>
    <div class="txtnav">
        <h1>第1章 测试</h1>
        <div class="txtinfo">字数信息</div>
        段落一。<br><br>
        段落二。
        <div id="txtright">下一章</div>
    </div>
    </body></html>"""
    soup = BeautifulSoup(html, "html.parser")
    body = _extract_chapter_body(soup)
    assert "测试" not in body  # h1 stripped
    assert "字数信息" not in body  # txtinfo stripped
    assert "下一章" not in body  # txtright stripped
    assert "段落一" in body
    assert "段落二" in body


def test_extract_title_falls_back_to_title_tag():
    from bs4 import BeautifulSoup

    # No div.booknav2 → fall through to <title> with site suffix stripped.
    soup = BeautifulSoup(
        "<html><head><title>小说名_69shuba</title></head><body></body></html>",
        "html.parser",
    )
    assert _extract_title(soup) == "小说名"


def test_extract_cover_url_resolves_relative():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<html><body><div class="bookimg2"><img src="/cover.jpg"></div></body></html>',
        "html.parser",
    )
    assert _extract_cover_url(soup, base_url="https://www.69shuba.com/book/1.htm") == \
        "https://www.69shuba.com/cover.jpg"


def test_extract_chapter_links_reverses_catalog_into_ascending_order():
    """69shuba's catalog page lists chapters latest-first; the extractor
    reverses so the first imported chapter is chapter 1."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<div id="catalog"><ul>'
        '<li><a href="/A1/3.html">第3章 三</a></li>'
        '<li><a href="/A1/2.html">第2章 二</a></li>'
        '<li><a href="/A1/1.html">第1章 一</a></li>'
        '</ul></div>',
        "html.parser",
    )
    links = _extract_chapter_links(soup, base_url="https://www.69shuba.com/A1/")
    assert len(links) == 3
    assert links[0] == ("第1章 一", "https://www.69shuba.com/A1/1.html")
    assert links[1] == ("第2章 二", "https://www.69shuba.com/A1/2.html")
    assert links[2] == ("第3章 三", "https://www.69shuba.com/A1/3.html")


def test_extract_chapter_links_filters_author_announcements():
    """Author-note entries (中奖名单, 公告, 求月票, …) get dropped —
    they'd otherwise occupy chapter numbers and shift every chapter after
    them. has_author_note_markers from parser.py is the filter."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<div id="catalog"><ul>'
        '<li><a href="/A1/4.html">第3章 三</a></li>'
        '<li><a href="/A1/3.html">二月中奖名单！</a></li>'
        '<li><a href="/A1/2.html">第2章 二</a></li>'
        '<li><a href="/A1/1.html">第1章 一</a></li>'
        '</ul></div>',
        "html.parser",
    )
    links = _extract_chapter_links(soup, base_url="https://www.69shuba.com/A1/")
    assert len(links) == 3
    titles = [t for t, _ in links]
    assert "二月中奖名单！" not in titles
    assert titles == ["第1章 一", "第2章 二", "第3章 三"]


def test_extract_chapter_links_returns_empty_when_only_qustime_widget_present():
    """The historical fallback to `div.qustime` (the 'recent updates'
    widget on /book/N.htm) caused silent 5-chapter imports. With the
    fallback removed, a page that carries only `div.qustime` should
    extract zero chapter links so the recipe raises rather than imports
    a partial novel.

    Regression for the May 2026 5-chapter-truncation bug."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<div class="qustime"><ul>'
        '<li><a href="/A1/recent5.html">最新章节 5</a></li>'
        '<li><a href="/A1/recent4.html">最新章节 4</a></li>'
        '<li><a href="/A1/recent3.html">最新章节 3</a></li>'
        '<li><a href="/A1/recent2.html">最新章节 2</a></li>'
        '<li><a href="/A1/recent1.html">最新章节 1</a></li>'
        '</ul></div>',
        "html.parser",
    )
    links = _extract_chapter_links(soup, base_url="https://www.69shuba.com/A1/")
    assert links == []
