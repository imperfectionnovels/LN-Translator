"""Per-site scraper recipes (lncrawl-inspired).

The generic `scrape_url` in `backend/services/scraper.py` uses trafilatura
to extract main-article text from arbitrary HTML — that's enough for a
single fan-translation blog post but loses badly on sites with quirks:
GBK encoding, index pages that link to chapter lists, JS-rendered tags,
or aggressive bot-detection. A *recipe* is a per-site Python module
that knows those quirks and owns the full import flow for the site.

How a recipe gets registered:
- Import `backend.services.scrapers.<site_module>`.
- The module instantiates its recipe and calls `register(instance)` at
  import time.
- `dispatch(hostname)` returns the recipe whose `matches(hostname)`
  comes back True, or None when no recipe is registered for the host.

Adding a new site is one file drop + one import line below.

Why a separate package and not a class hierarchy under
`backend/services/translators/`? Translators are about LLM choice;
recipes are about HTTP / parsing. They share no superclass and zero
runtime state. Co-locating them would confuse both.
"""

from __future__ import annotations

from urllib.parse import urlparse

from backend.services.scrapers.base import BaseRecipe

_REGISTERED: list[BaseRecipe] = []


def register(recipe: BaseRecipe) -> None:
    """Add a recipe to the dispatcher. Idempotent — re-importing a
    recipe module (e.g. during pytest collection) won't double-register
    because the per-module `register()` call lives at import scope and
    Python caches the module."""
    if recipe not in _REGISTERED:
        _REGISTERED.append(recipe)


def dispatch(hostname: str | None) -> BaseRecipe | None:
    """Return the recipe whose `matches(hostname)` is True, or None.
    Hostname comparison is the recipe's responsibility — most recipes
    do a case-insensitive `endswith` so subdomain variants of a site
    (m.example.com, www2.example.com) match the same recipe."""
    if not hostname:
        return None
    host = hostname.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    for r in _REGISTERED:
        if r.matches(host):
            return r
    return None


def dispatch_for_url(url: str) -> BaseRecipe | None:
    """Convenience: parse the URL and dispatch on its hostname."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    return dispatch(parsed.hostname)


# ---- Register every site recipe here. Order doesn't matter (matches() is
# host-scoped, no overlap by design). One import per recipe.

from backend.services.scrapers import sixnineshu  # noqa: E402, F401
from backend.services.scrapers import syosetu  # noqa: E402, F401
from backend.services.scrapers import uukanshu  # noqa: E402, F401
from backend.services.scrapers import piaotian  # noqa: E402, F401

__all__ = ["BaseRecipe", "dispatch", "dispatch_for_url", "register"]
