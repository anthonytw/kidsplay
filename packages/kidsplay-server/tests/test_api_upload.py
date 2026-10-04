"""Tests for POST /api/v1/media/upload and the updated ingest endpoint.

Uses real PNG images so the photo pipeline can run end-to-end.
Audio ingest is covered by test_processing_pipeline.py.
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


def _png_bytes(width: int = 200, height: int = 200, *, seed: int = 0) -> bytes:
    color = (100 + seed * 30 % 155, 150, max(10, 200 - seed * 20 % 190))
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


async def _create_profile(client: AsyncClient, name: str = "Leo") -> dict:
    r = await client.post("/api/v1/profiles", json={"name": name})
    assert r.status_code == 201
    return r.json()


# ---------------------------------------------------------------------------
# POST /api/v1/media/upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_photo_success(client: AsyncClient) -> None:
    """Uploading a PNG produces a READY result."""
    resp = await client.post(
        "/api/v1/media/upload",
        files={"file": ("holiday.png", _png_bytes(), "image/png")},
        data={"media_type": "photo", "playlist_title": "Holidays"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_files"] == 1
    assert body["successful"] == 1
    assert body["failed"] == 0
    assert body["results"][0]["processing_status"] == "ready"
    assert body["results"][0]["title"] == "holiday"


@pytest.mark.asyncio
async def test_upload_photo_with_profile(client: AsyncClient, tmp_path: Path) -> None:
    """Upload assigns the item to the given profile."""
    profile = await _create_profile(client, "Mia")
    profile_id = profile["id"]

    resp = await client.post(
        "/api/v1/media/upload",
        files={"file": ("beach.png", _png_bytes(seed=1), "image/png")},
        data={
            "media_type": "photo",
            "playlist_title": "Summer",
            "profile_ids": profile_id,
        },
    )
    assert resp.status_code == 200
    media_id = resp.json()["results"][0]["media_id"]

    # Verify the item appears when filtered by profile.
    r = await client.get(
        "/api/v1/media",
        params={"profile_id": profile_id, "media_type": "photo"},
    )
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()]
    assert media_id in ids


@pytest.mark.asyncio
async def test_upload_deduplication(client: AsyncClient) -> None:
    """Uploading the same bytes twice returns skipped=True on the second."""
    img = _png_bytes(seed=99)
    form = {"media_type": "photo", "playlist_title": "Dup"}

    r1 = await client.post(
        "/api/v1/media/upload",
        files={"file": ("dup.png", img, "image/png")},
        data=form,
    )
    assert r1.status_code == 200
    assert r1.json()["successful"] == 1

    r2 = await client.post(
        "/api/v1/media/upload",
        files={"file": ("dup.png", img, "image/png")},
        data=form,
    )
    assert r2.status_code == 200
    assert r2.json()["skipped"] == 1


# ---------------------------------------------------------------------------
# POST /api/v1/media/ingest — source_url validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_rejects_both_source_fields(client: AsyncClient) -> None:
    """Providing both source_path and source_url is a validation error."""
    resp = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": "/tmp/file.mp3",
            "source_url": "https://example.com/file.mp3",
            "media_type": "music",
            "playlist_title": "Test",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_rejects_neither_source_field(
    client: AsyncClient,
) -> None:
    """Providing neither source_path nor source_url is a validation error."""
    resp = await client.post(
        "/api/v1/media/ingest",
        json={
            "media_type": "music",
            "playlist_title": "Test",
        },
    )
    assert resp.status_code == 422
