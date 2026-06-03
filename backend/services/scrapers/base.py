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

All recipe imports go through `import_runner.start_from_recipe`, which
drives `plan + fetch_chapter` with per-chapter commits (resumable across
restarts). There is no longer a single-call atomic `scrape()` on the
recipe surface; the recipe end-to-end tests drive plan + fetch_chapter
through a small test-only helper instead.

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


@dataclass(frozen=True)
class RecipeResult:
    """Summary of a completed recipe import (novel + chapters already
    created). The production import_runner reports progress + completion
    through `scrape_jobs` rather than returning this; the recipe
    end-to-end tests use it as a compact assertion target via the
    test-only atomic import helper.

    Kept as a typed shape (rather than a bare dict) so the test helper and
    any future single-call caller share one contract: novel_id, the first
    chapter number, the count added, the source URL, the title, and whether
    a cover was extracted."""

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
    #: imported via this recipe. The import_runner reads it when creating
    #: the novel skeleton. The route layer's user-supplied `genre` (when
    #: present) takes precedence — the default is the fallback when the
    #: user doesn't pick. NULL leaves novels.genre NULL, deferring to the
    #: user's edit on the novel page.
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

        MUST be idempotent: the runner replays calls after a crash and on
        a user-initiated resume. Don't mutate `recipe_state` in a way that's
        required for the call to succeed (it's shared across all per-novel
        fetches; a replay must see the same state).

        On any failure (permanent or transient) raise `ScrapeError`. The
        import_runner's fill loop catches it, flips the novel to
        `import_status='paused'`, and stops at that chapter. The user can
        hit Resume to re-run the fill loop, which retries the still-pending
        chapter. There is no separate transient-error path: a recipe that
        wants to retry transient network blips internally should do so
        before raising, since the runner treats every raise as "pause here."
        """


# ---------------------------------------------------------------------------
# Shared parsing helpers
# ---------------------------------------------------------------------------
# Primitives that more than one site recipe needs. They live here (not in any
# one recipe) so recipes depend on the shared base, never on a sibling recipe.

_HAN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9}
_HAN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def han_digits_to_int(s: str) -> int:
    """Small Han-numeral parser sufficient for chapter numbers (up to
    ~99999). Doesn't handle every edge case in classical Chinese
    numerals; that's fine for chapter labels. Raises ValueError on an
    unrecognized character so callers can fall back to a placeholder."""
    total = 0
    section = 0
    last_digit = 0
    for ch in s:
        if ch in _HAN_DIGITS:
            last_digit = _HAN_DIGITS[ch]
        elif ch in _HAN_UNITS:
            unit = _HAN_UNITS[ch]
            if last_digit == 0:
                last_digit = 1
            if unit >= 10000:
                section += last_digit * unit
                total += section
                section = 0
            else:
                section += last_digit * unit
            last_digit = 0
        else:
            raise ValueError(f"unknown han digit {ch!r}")
    return total + section + last_digit
