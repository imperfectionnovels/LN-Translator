"""Recipe registry + dispatch tests.

Verifies hostname matching, www-stripping, and fallback when no recipe
is registered for a host. Doesn't touch the network or DB.
"""

from __future__ import annotations

import pytest

from backend.services.scrapers import dispatch, dispatch_for_url, register
from backend.services.scrapers.base import BaseRecipe


class _StubRecipe(BaseRecipe):
    """Test recipe that matches one host. Used to verify dispatch
    without touching the real 69shuba recipe."""

    name = "stub"

    def __init__(self, host: str):
        self._host = host

    def matches(self, hostname: str) -> bool:
        return hostname == self._host

    async def plan(self, url, *, cookies, fetch, progress=None):  # pragma: no cover
        raise RuntimeError("stub recipe should not actually run in dispatch tests")

    async def fetch_chapter(self, planned, *, cookies, fetch, recipe_state):  # pragma: no cover
        raise RuntimeError("stub recipe should not actually run in dispatch tests")


def test_dispatch_returns_known_recipe():
    """The 69shuba recipe is auto-registered at import time."""
    r = dispatch("69shuba.com")
    assert r is not None
    assert r.name == "69shuba"


def test_dispatch_strips_www_prefix():
    """www.69shuba.com and 69shuba.com hit the same recipe."""
    r = dispatch("www.69shuba.com")
    assert r is not None
    assert r.name == "69shuba"


def test_dispatch_returns_none_for_unknown_host():
    """No recipe registered for example.com → dispatch returns None,
    caller falls back to the trafilatura path."""
    r = dispatch("totally-unknown-novel-site.example")
    assert r is None


def test_dispatch_returns_none_for_empty_host():
    assert dispatch(None) is None
    assert dispatch("") is None


def test_dispatch_for_url_parses_hostname():
    r = dispatch_for_url("https://www.69shuba.com/book/88724.htm")
    assert r is not None and r.name == "69shuba"


def test_dispatch_for_url_handles_garbage_url():
    """Malformed URL must not crash dispatch — returns None."""
    r = dispatch_for_url("not-a-url-at-all")
    assert r is None


def test_register_idempotent():
    """Registering the same recipe twice doesn't double-register —
    important because importing the recipes package twice (e.g. via
    pytest collection) must not corrupt the registry."""
    from backend.services.scrapers import _REGISTERED

    stub = _StubRecipe("stubhost.example")
    before_count = len(_REGISTERED)
    register(stub)
    after_first = len(_REGISTERED)
    register(stub)  # second registration of the SAME instance
    after_second = len(_REGISTERED)

    assert after_first == before_count + 1
    assert after_second == after_first  # idempotent
    # Cleanup so we don't pollute other tests.
    _REGISTERED.remove(stub)


@pytest.mark.parametrize("host", [
    "69shuba.com",
    "www.69shuba.com",
    "m.69shuba.com",
    "69shu.com",
    "69shu.pro",
    "69shuba.pro",
    "69xinshu.com",
])
def test_69shuba_recipe_matches_all_known_mirrors(host):
    """Every domain listed in lncrawl's base_url should dispatch to the
    69shuba recipe."""
    r = dispatch(host.replace("www.", "") if host.startswith("www.") else host)
    assert r is not None
    assert r.name == "69shuba"
