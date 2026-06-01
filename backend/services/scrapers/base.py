"""BaseRecipe ABC + RecipeResult dataclass.

A recipe owns the import path for one site. As of 2026-05-26 the contract
is **two-phase** to support resumable imports:

1. `plan(url, fetch, cookies)` — discover phase. Fetch the catalog page,
   extract the title + cover URL + the ordered list of chapter URLs
   with their printed-number hints. No chapter prose fetched yet. The
   runner persists this as a novel skeleton: one row per planned
   chapter, all with `original_text=''` and `import_source_url=<URL>`.

2. `fetch_chapter(planned, fetch, cookies)` — fill phase. Fetch and
   parse a single chapter body. Called once per skeleton row; runs in
   a loop the import_runner owns. **Must be idempotent** — the runner
   may retry on transient errors, and on resume it picks up exactly
   the chapters whose `import_fetched_at` is still NULL.

The legacy `scrape()` method (atomic single-transaction create of
novel + all chapters) is kept for back-compat with smoke scripts and
the generic `/scrape` route's non-runner fallback. New code paths
should go through `import_runner.start_from_recipe` which drives
`plan + fetch_chapter` with per-chapter commits.

Every fetch goes through the injected `fetch` callable (from
`backend.services.scraper._fetch_with_manual_redirects`) so the SSRF
guard + manual redirect validation + size/timeout caps are
non-bypassable — a recipe never constructs its own httpx / requests
client.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import aiosqlite


@dataclass(frozen=True)
class RecipeResult:
    """Returned by a recipe's `scrape()` (the legacy atomic path) when it
    has already created the novel + chapters. The route layer
    (routes/translate.py /scrape) distinguishes this from the generic
    ScrapeResult via isinstance and skips its own create-novel flow when
    it sees a RecipeResult.

    Why the two shapes coexist instead of making everything go through
    the same parse pipeline: the parse pipeline takes one big text blob
    and runs heading regexes over it. A recipe already knows what's a
    chapter heading vs body via site-specific selectors — round-tripping
    back through the regex parser would lose that fidelity."""

    novel_id: int
    first_chapter_num: int
    added_chapters: int
    source_url: str
    title: str
    cover_extracted: bool = False


@dataclass(frozen=True)
class PlannedChapterRef:
    """One entry in a RecipePlan's chapter list. `chapter_num` is the
    PLACEHOLDER index (1..N) at planning time — the runner reconciles
    it against `printed_num` once the full list is known so skeleton
    rows land with their canonical chapter numbers."""
    chapter_num: int
    title_zh: str | None
    source_url: str
    printed_num: int | None = None


@dataclass(frozen=True)
class RecipePlan:
    """The discover-phase output. The runner uses this to create the
    novel skeleton + N pending chapter rows in one short transaction.

    `cover_url` is informational — the runner can fetch it after the
    fill loop completes (best-effort; never blocks an import). `None`
    means the recipe didn't find a cover; the runner skips the fetch.

    `recipe_state` is an opaque dict the recipe can stuff with per-novel
    fetch state (e.g. a Referer URL, a CSRF token, decoded cookies).
    The runner passes it back into every `fetch_chapter` call so
    per-chapter requests share the same context. `frozen=True` for
    the dataclass; the dict inside is intentionally mutable."""
    title: str
    source_url: str          # The canonical URL we land all chapter fetches against.
    catalog_url: str         # The URL we actually parsed for the chapter list.
    cover_url: str | None
    chapters: tuple["PlannedChapterRef", ...]
    recipe_state: dict       # opaque per-novel context the recipe wants on every chapter fetch


@dataclass(frozen=True)
class FetchedChapter:
    """The fill-phase output for a single chapter. Plumbed back into
    `_fill_skeleton_chapter` by the runner."""
    title_zh: str | None
    original_text: str


# Type alias for the fetcher the recipe receives. The async helper
# returns (status_code, body_bytes, content_type, encoding_hint).
# Recipes never construct their own httpx client — the fetcher carries
# the SSRF / redirect / timeout safeguards.
Fetcher = Callable[..., Awaitable[tuple[int, bytes, str, str]]]


# Optional progress callback recipes receive when running under the
# background scrape-job runner. `step` is a short tag like
# 'fetching_overview' / 'fetching_chapters' / 'writing'; `current` and
# `total` are chapter counters (both 0 before the chapter list is
# known). Recipes call `await progress(...)` if it isn't None and just
# skip when it is — the same recipe code path serves blocking direct
# calls (smoke scripts) and the background runner.
ProgressFn = Optional[Callable[[str, int, int], Awaitable[None]]]


class BaseRecipe(ABC):
    """Subclass + drop into backend/services/scrapers/<name>.py +
    call register(instance) at module scope."""

    #: Short identifier used in logs / error messages. e.g. "69shuba".
    name: str = ""

    #: Default genre key (from backend/genres.py::GENRES) for novels
    #: imported via this recipe. The recipe's `scrape()` is expected to
    #: pass this through to `atomic_create_novel`. The route layer's
    #: user-supplied `genre` (when present) takes precedence — the
    #: default is the fallback when the user doesn't pick. NULL leaves
    #: novels.genre NULL, deferring to the user's edit on the novel page.
    default_genre: str | None = None

    @abstractmethod
    def matches(self, hostname: str) -> bool:
        """Hostname is lower-cased + has any leading "www." stripped
        before this is called (see __init__.dispatch). Implementations
        typically do `return host == "example.com" or host.endswith(".example.com")`."""

    @abstractmethod
    async def plan(
        self,
        url: str,
        *,
        cookies: str | None,
        fetch: Fetcher,
        progress: ProgressFn = None,
    ) -> RecipePlan:
        """Discover phase. Fetch the catalog page; extract title, cover URL,
        and the ordered chapter URL list. No chapter prose fetched.

        Returns a `RecipePlan`. The runner persists this as a novel
        skeleton (one row per planned chapter) so a crash between
        planning and fetching loses nothing — on restart the runner
        sees the skeleton chapters and re-runs only `fetch_chapter`
        for the still-pending rows.

        On failure (catalog page 404, parser couldn't find chapter
        links, etc.), raise `ScrapeError` with an actionable message.
        """

    @abstractmethod
    async def fetch_chapter(
        self,
        planned: PlannedChapterRef,
        *,
        cookies: str | None,
        fetch: Fetcher,
        recipe_state: dict,
    ) -> FetchedChapter:
        """Fill phase. Fetch and parse a single chapter body.

        MUST be idempotent — the runner retries on transient errors and
        replays calls after a crash. Don't mutate `recipe_state` in a
        way that's required for the call to succeed (it's shared across
        all per-novel fetches; a retry must see the same state). On
        permanent failure raise `ScrapeError`; on transient failure
        raise `TransientScrapeError` so the runner backs off and
        retries rather than marking the chapter dead.
        """

    async def scrape(
        self,
        url: str,
        conn: aiosqlite.Connection,
        *,
        cookies: str | None,
        fetch: Fetcher,
        progress: ProgressFn = None,
    ) -> RecipeResult:
        """Legacy atomic-import path. Kept for back-compat with smoke
        scripts; the live route layer goes through `import_runner`
        instead so the per-chapter loop is resumable.

        Default implementation: drive plan + fetch_chapter sequentially
        and atomic-create at the end (matches pre-refactor behavior).
        Subclasses that need cross-chapter state (e.g. paginated
        chapter lists with Referer continuity) can override this, but
        none of the current four recipes do.
        """
        # Lazy imports — keep base.py free of cyclic recipe deps.
        from backend.services.covers import write_cover_for_novel
        from backend.services.lang_detect import detect_source_language
        from backend.services.parser import ParsedChapter
        from backend.services.uploads import atomic_create_novel

        plan = await self.plan(url, cookies=cookies, fetch=fetch, progress=progress)
        if progress:
            await progress("fetching_chapters", 0, len(plan.chapters))

        parsed: list[ParsedChapter] = []
        for i, p in enumerate(plan.chapters, start=1):
            fetched = await self.fetch_chapter(
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
            if progress:
                await progress("fetching_chapters", i, len(plan.chapters))

        # Note: chapter_num reconciliation happens in the runner's
        # `_reconcile_planned`, not here — the legacy `scrape()` callers
        # (smoke scripts) historically used the enumerate-based chapter_num
        # without reconcile, and breaking that here changed first_chapter_num
        # in syosetu's test fixture. The runner path remains reconciled.
        if progress:
            await progress("writing", len(plan.chapters), len(plan.chapters))

        detected_lang = detect_source_language(
            parsed[0].original_text if parsed else "",
        )
        novel_id = await atomic_create_novel(
            conn,
            title=plan.title,
            chapters=parsed,
            source_type="url",
            source_url=plan.catalog_url,
            genre=self.default_genre,
            source_language=detected_lang,
        )

        cover_extracted = False
        if plan.cover_url:
            try:
                cs, cbody, cct, _enc = await fetch(
                    plan.cover_url, cookies=cookies,
                )
                if cs < 400 and cct.startswith("image/"):
                    written = await write_cover_for_novel(
                        conn, novel_id, cbody, source="url",
                    )
                    if written is not None:
                        cover_extracted = True
                        await conn.commit()
            except Exception:
                import logging
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
