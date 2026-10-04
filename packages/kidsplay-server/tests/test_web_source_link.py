"""The web UI links to its source code (AGPL-3.0 section 13).

Anyone running a modified server for others must offer those users its source;
the footer link, pointed at their repository with ``KIDSPLAY_SOURCE_URL``, does
that on every page.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_server.api.app import create_app
from kidsplay_server.config import DEFAULT_SOURCE_URL, source_url


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def client(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=admin_headers,
    ) as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/media", "/settings"])
async def test_every_page_links_to_the_source(client: AsyncClient, path: str) -> None:
    page = (await client.get(path)).text
    assert f'<a href="{DEFAULT_SOURCE_URL}" rel="noopener">Source code</a>' in page
    assert "AGPL-3.0 or later" in page


async def test_signed_out_pages_link_to_the_source(app: FastAPI) -> None:
    """The first-run setup page (where /login leads with no admin yet) too."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as c:
        r = await c.get("/login")
    assert r.url.path in ("/login", "/setup")
    assert f'href="{DEFAULT_SOURCE_URL}"' in r.text


async def test_operator_points_it_at_their_repository(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIDSPLAY_SOURCE_URL", "https://git.example.org/fork/kidsplay")
    page = (await client.get("/")).text
    assert 'href="https://git.example.org/fork/kidsplay"' in page


async def test_spanish_footer(client: AsyncClient) -> None:
    page = (await client.get("/?lang=es")).text
    assert "Código fuente" in page


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "git.example.org/fork", "  ", "ftp://example.org/x"],
)
def test_non_http_urls_fall_back_to_upstream(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIDSPLAY_SOURCE_URL", value)
    assert source_url() == DEFAULT_SOURCE_URL
