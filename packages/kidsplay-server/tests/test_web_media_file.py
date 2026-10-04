"""Tests for the web UI's media-file route.

The admin UI renders thumbnails and plays audio from ``/web/media-file/{hash}``.
That route exists because the device-facing ``/api/v1/sync/file/{hash}`` endpoint
requires a Bearer token, which a browser ``<img>``/``<audio>`` tag cannot send.
"""

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from kidsplay_server.api.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_png(path: Path, width: int = 200, height: int = 200) -> Path:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(120, 150, 180)).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


async def ingest_photo_hashes(client: AsyncClient, src: Path) -> list[dict]:
    """Ingest a photo and return its processed-file rows."""
    r = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": str(src),
            "media_type": "photo",
            "playlist_title": "My Photos",
            "profile_ids": [],
        },
    )
    assert r.status_code == 200
    media_id = r.json()["results"][0]["media_id"]
    r_files = await client.get(f"/api/v1/media/{media_id}/files")
    assert r_files.status_code == 200
    return r_files.json()


# ---------------------------------------------------------------------------
# GET /web/media-file/{content_hash}
# ---------------------------------------------------------------------------


class TestWebMediaFile:
    async def test_serves_file_without_auth(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """The browser sends no Authorization header and still gets the bytes."""
        files = await ingest_photo_hashes(client, make_png(tmp_path / "photo.png"))
        content_hash = files[0]["content_hash"]

        r = await client.get(f"/web/media-file/{content_hash}")
        assert r.status_code == 200
        assert len(r.content) > 0

    async def test_sets_content_type_from_stored_mime(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        files = await ingest_photo_hashes(client, make_png(tmp_path / "photo.png"))
        photo = next(f for f in files if f["file_type"] == "photo_resized")

        r = await client.get(f"/web/media-file/{photo['content_hash']}")
        assert r.status_code == 200
        assert r.headers["content-type"] == photo["mime_type"]

    async def test_unknown_hash_returns_404(self, client: AsyncClient) -> None:
        r = await client.get(f"/web/media-file/{'a' * 64}")
        assert r.status_code == 404
        assert r.json()["error_code"] == "NOT_FOUND"

    async def test_device_endpoint_still_requires_auth(
        self, client: AsyncClient, anon_client: AsyncClient, tmp_path: Path
    ) -> None:
        """Adding the web route must not loosen the device-facing endpoint."""
        files = await ingest_photo_hashes(client, make_png(tmp_path / "photo.png"))
        content_hash = files[0]["content_hash"]

        r = await anon_client.get(f"/api/v1/sync/file/{content_hash}")
        assert r.status_code == 401
        assert r.json()["error_code"] == "UNAUTHORIZED"
