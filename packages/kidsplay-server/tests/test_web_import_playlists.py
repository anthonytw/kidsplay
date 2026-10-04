"""The import page suggests existing playlist titles in every playlist field.

Only the photo tab had suggestions; a video, playlist, folder or archive import
had to retype a title exactly, or the media landed in a second playlist.
"""

import html
import io
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from kidsplay_server.api.app import create_app

# Every playlist-title field on the import page.
FIELDS = [
    "yt-playlist-title",  # video / single track
    "yt-pl-title",  # video playlist
    "photo-playlist-title",
    "bulk-playlist-title",
    "arc-playlist-title",
]
TITLES = ["Bedtime Songs", 'Dad\'s "Road Trip" <mix>']


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


async def _ingest_photo(client: AsyncClient, tmp_path: Path, playlist: str) -> None:
    photo = tmp_path / f"{len(playlist)}.png"
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (len(playlist) * 7 % 255, 90, 160)).save(buf, "PNG")
    photo.write_bytes(buf.getvalue())
    r = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": str(photo),
            "media_type": "photo",
            "playlist_title": playlist,
        },
    )
    assert r.status_code in (200, 201), r.text


async def test_every_playlist_field_suggests_existing_titles(
    client: AsyncClient, tmp_path: Path
) -> None:
    for title in TITLES:
        await _ingest_photo(client, tmp_path, title)

    page = (await client.get("/media/import")).text

    for field in FIELDS:
        tag = re.search(rf'<input[^>]*id="{field}"[^>]*>', page)
        assert tag, field
        assert 'list="playlist-titles"' in tag.group(0), tag.group(0)

    datalist = re.search(r'<datalist id="playlist-titles">(.*?)</datalist>', page, re.S)
    assert datalist
    options = [
        html.unescape(v)
        for v in re.findall(r'<option value="([^"]*)"', datalist.group(1))
    ]
    assert sorted(options) == sorted(TITLES)
    # Escaped in the attribute, not injected as markup.
    assert "<mix>" not in datalist.group(1)
