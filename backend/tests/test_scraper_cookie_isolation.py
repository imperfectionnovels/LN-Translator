"""H2: a pasted Cookie / Authorization header is sent only to the origin host;
once a redirect crosses to a different host the credential headers are stripped,
so a session token pasted for site A never leaks to site B.
"""

from __future__ import annotations

import httpx
import pytest

from backend.services import scraper as scraper_mod


def _public(*a, **k):
    return [(2, 1, 6, "", ("8.8.8.8", 0))]


@pytest.mark.asyncio
async def test_cookie_stripped_on_cross_origin_redirect(monkeypatch):
    monkeypatch.setattr(scraper_mod.socket, "getaddrinfo", _public)
    seen: list[tuple[str, str | None]] = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("cookie")))
        if request.url.host == "a.example":
            return httpx.Response(302, headers={"location": "https://b.example/next"})
        return httpx.Response(
            200,
            content=b"<html><body><article><p>chapter body text here.</p></article></body></html>",
            headers={"content-type": "text/html"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        headers={"Cookie": "sess=secret"},
        follow_redirects=False,
    ) as client:
        resp = await scraper_mod._fetch_with_manual_redirects(client, "https://a.example/start")
        await resp.aread()
        await resp.aclose()

    # Origin host keeps the cookie; the cross-origin hop must not receive it.
    assert seen[0] == ("a.example", "sess=secret")
    assert seen[1][0] == "b.example"
    assert seen[1][1] is None
