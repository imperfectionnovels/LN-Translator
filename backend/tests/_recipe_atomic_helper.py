"""Test-only atomic recipe import helper.

The production recipe path is two-phase and resumable: a route checks the
recipe dispatcher, then `import_runner.start_from_recipe` drives the
recipe's `plan()` + `fetch_chapter()` over a persisted skeleton with
per-chapter commits. There is no single-call "scrape the whole novel
atomically" method on a recipe anymore (the legacy `BaseRecipe.scrape()`
default was removed once the route stopped reaching it).

The recipe end-to-end tests still want one cheap call that drives a recipe
through plan + fetch_chapter + create and hands back a `RecipeResult` so
they can assert on chapter count / first-chapter-num / cover in one place,
without spinning up a `scrape_jobs` row and the import_runner's background
machinery. This helper is exactly that: the old atomic flow, parked in the
test tree where it belongs. It is NOT part of any production code path.
"""

from __future__ import annotations

import logging

import aiosqlite

from backend.services.parser import ParsedChapter
from backend.services.scrapers.base import BaseRecipe, RecipeResult


async def atomic_import_via_recipe(
    recipe: BaseRecipe,
    url: str,
    conn: aiosqlite.Connection,
    *,
    cookies: str | None = None,
    fetch=None,
) -> RecipeResult:
    """Drive `recipe` through plan + fetch_chapter sequentially and
    atomic-create the novel + chapters at the end. Returns a RecipeResult.

    Mirrors what the removed `BaseRecipe.scrape()` default did. Note:
    chapter_num here is the recipe's enumerate-based placeholder (no
    `reconcile_chapter_numbers`), matching the legacy atomic behavior the
    recipe fixtures were written against. The production import_runner path
    DOES reconcile; that divergence is intentional and historical.
    """
    from backend.services.covers import write_cover_for_novel
    from backend.services.lang_detect import detect_source_language
    from backend.services.uploads import atomic_create_novel

    if fetch is None:
        from backend.services.scraper import fetch_one as fetch

    plan = await recipe.plan(url, cookies=cookies, fetch=fetch)

    parsed: list[ParsedChapter] = []
    for p in plan.chapters:
        fetched = await recipe.fetch_chapter(
            p, cookies=cookies, fetch=fetch, recipe_state=plan.recipe_state,
        )
        parsed.append(
            ParsedChapter(
                chapter_num=p.chapter_num,
                title_zh=fetched.title_zh,
                original_text=fetched.original_text,
                printed_num=p.printed_num,
            )
        )

    detected_lang = detect_source_language(
        parsed[0].original_text if parsed else "",
    )
    novel_id = await atomic_create_novel(
        conn,
        title=plan.title,
        chapters=parsed,
        source_type="url",
        source_url=plan.catalog_url,
        genre=recipe.default_genre,
        source_language=detected_lang,
    )

    cover_extracted = False
    if plan.cover_url:
        try:
            cs, cbody, cct, _enc = await fetch(plan.cover_url, cookies=cookies)
            if cs < 400 and cct.startswith("image/"):
                written = await write_cover_for_novel(
                    conn, novel_id, cbody, source="url",
                )
                if written is not None:
                    cover_extracted = True
                    await conn.commit()
        except Exception:
            logging.getLogger(__name__).exception(
                "cover fetch failed for novel %d (continuing)", novel_id,
            )

    return RecipeResult(
        novel_id=novel_id,
        first_chapter_num=parsed[0].chapter_num if parsed else 1,
        added_chapters=len(parsed),
        source_url=plan.catalog_url,
        title=plan.title,
        cover_extracted=cover_extracted,
    )
