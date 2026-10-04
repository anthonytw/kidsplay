"""Tests for the sync API endpoints.

Tests the manifest generation (including ETag/304 support) and file download
endpoint using photos so the full ingest pipeline can run without mocking.

The sync endpoints require ``Authorization: Bearer {api_key}`` — the helpers
below thread the device's key through every request.
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


def auth(device: dict) -> dict:
    """Bearer-auth headers for a device dict (as returned by create_device)."""
    return {"Authorization": f"Bearer {device['api_key']}"}


def make_png(path: Path, width: int = 200, height: int = 200, *, seed: int = 0) -> Path:
    color = (100 + seed * 30 % 155, 150, max(10, 200 - seed * 20 % 190))
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


async def create_profile(client: AsyncClient, name: str = "Leo") -> dict:
    r = await client.post("/api/v1/profiles", json={"name": name})
    assert r.status_code == 201
    return r.json()


async def create_device(client: AsyncClient, profile_id: str) -> dict:
    r = await client.post(
        "/api/v1/devices",
        json={"name": "Leo's GameBoy", "profile_id": profile_id},
    )
    assert r.status_code == 201
    return r.json()


async def ingest_photo(
    client: AsyncClient,
    src: Path,
    playlist: str = "My Photos",
    profile_ids: list[str] | None = None,
) -> dict:
    r = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": str(src),
            "media_type": "photo",
            "playlist_title": playlist,
            "profile_ids": profile_ids or [],
        },
    )
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestSyncAuth:
    async def test_manifest_requires_auth(
        self, client: AsyncClient, anon_client: AsyncClient
    ) -> None:
        """No Authorization header → 401."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await anon_client.get(f"/api/v1/devices/{device['id']}/manifest")
        assert r.status_code == 401
        assert r.json()["error_code"] == "UNAUTHORIZED"

    async def test_manifest_rejects_invalid_token(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest",
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        assert r.status_code == 401

    async def test_manifest_rejects_other_device_token(
        self, client: AsyncClient
    ) -> None:
        """A valid token may not read another device's manifest → 403."""
        profile = await create_profile(client)
        device_a = await create_device(client, profile["id"])
        device_b = await create_device(client, profile["id"])
        r = await client.get(
            f"/api/v1/devices/{device_b['id']}/manifest",
            headers=auth(device_a),
        )
        assert r.status_code == 403
        assert r.json()["error_code"] == "FORBIDDEN"

    async def test_download_requires_auth(self, anon_client: AsyncClient) -> None:
        r = await anon_client.get(f"/api/v1/sync/file/{'a' * 64}")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /devices/{device_id}/manifest
# ---------------------------------------------------------------------------


class TestSyncManifest:
    async def test_empty_manifest_for_new_device(self, client: AsyncClient) -> None:
        """A new device with no assigned media returns an empty manifest."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["device_id"] == device["id"]
        assert data["profile_id"] == profile["id"]
        assert data["files"] == []
        assert data["media"] == []
        assert data["total_size_bytes"] == 0

    async def test_manifest_has_etag_header(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        assert r.status_code == 200
        assert "etag" in r.headers

    async def test_manifest_etag_is_sha256_hex(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        etag = r.headers["etag"]
        assert len(etag) == 64
        assert all(c in "0123456789abcdef" for c in etag)

    async def test_manifest_etag_matches_manifest_hash(
        self, client: AsyncClient
    ) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        assert r.headers["etag"] == r.json()["manifest_hash"]

    async def test_manifest_304_when_etag_matches(self, client: AsyncClient) -> None:
        """If-None-Match with current hash returns 304 Not Modified."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r1 = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        etag = r1.headers["etag"]

        r2 = await client.get(
            f"/api/v1/devices/{device['id']}/manifest",
            headers={**auth(device), "if-none-match": etag},
        )
        assert r2.status_code == 304

    async def test_manifest_200_when_etag_stale(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """If-None-Match with a different hash returns 200 with a new body."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        stale_etag = "a" * 64  # clearly wrong hash

        r2 = await client.get(
            f"/api/v1/devices/{device['id']}/manifest",
            headers={**auth(device), "if-none-match": stale_etag},
        )
        assert r2.status_code == 200
        assert r2.headers["etag"] != stale_etag

    async def test_manifest_includes_assigned_media(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Full flow: create profile/device, ingest photo, assign, check manifest."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=1)

        result = await ingest_photo(client, src, profile_ids=[profile["id"]])
        media_id = result["results"][0]["media_id"]

        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["media"]) == 1
        assert data["media"][0]["media_id"] == media_id
        assert len(data["files"]) >= 1
        assert data["total_size_bytes"] > 0

    async def test_manifest_media_entry_fields(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=1)
        await ingest_photo(
            client, src, playlist="Vacation 2024", profile_ids=[profile["id"]]
        )

        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        entry = r.json()["media"][0]
        assert entry["media_type"] == "photo"
        assert entry["playlist_title"] == "Vacation 2024"
        assert entry["photo_path"] is not None
        assert isinstance(entry["thumbnail_paths"], dict)

    async def test_manifest_file_entries_have_correct_type(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Photo ingest → file entries should use 'photo' and 'thumbnail' types."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=1)
        await ingest_photo(client, src, profile_ids=[profile["id"]])

        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        file_types = {f["file_type"] for f in r.json()["files"]}
        assert "photo" in file_types
        assert "thumbnail" in file_types

    async def test_manifest_excludes_unassigned_media(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Media not assigned to the device's profile must not appear."""
        profile = await create_profile(client)
        other_profile = await create_profile(client, "Emma")
        device = await create_device(client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=1)
        # Ingest and assign only to other_profile.
        await ingest_photo(client, src, profile_ids=[other_profile["id"]])

        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        assert r.json()["media"] == []

    async def test_manifest_hash_stable_for_same_content(
        self, client: AsyncClient
    ) -> None:
        """Calling manifest twice with no changes returns the same hash."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r1 = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        r2 = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        assert r1.json()["manifest_hash"] == r2.json()["manifest_hash"]

    async def test_manifest_hash_changes_after_assignment(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Adding media to the profile must change the manifest hash."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])

        r_before = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        hash_before = r_before.json()["manifest_hash"]

        src = make_png(tmp_path / "photo.png", seed=1)
        await ingest_photo(client, src, profile_ids=[profile["id"]])

        r_after = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        hash_after = r_after.json()["manifest_hash"]

        assert hash_before != hash_after


# ---------------------------------------------------------------------------
# GET /sync/file/{content_hash}
# ---------------------------------------------------------------------------


class TestSyncFileDownload:
    async def _ingest_and_get_hash(
        self, client: AsyncClient, src: Path, profile_id: str
    ) -> str:
        """Ingest a photo and return the content_hash of a processed file."""
        result = await ingest_photo(client, src, profile_ids=[profile_id])
        media_id = result["results"][0]["media_id"]
        r = await client.get(f"/api/v1/media/{media_id}/files")
        files = r.json()
        return files[0]["content_hash"]

    async def test_download_existing_file(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=1)
        content_hash = await self._ingest_and_get_hash(client, src, profile["id"])

        r = await client.get(f"/api/v1/sync/file/{content_hash}", headers=auth(device))
        assert r.status_code == 200
        assert len(r.content) > 0

    async def test_download_has_correct_content_type(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=1)

        result = await ingest_photo(client, src, profile_ids=[profile["id"]])
        media_id = result["results"][0]["media_id"]
        r_files = await client.get(f"/api/v1/media/{media_id}/files")
        # Find the photo_resized file (WebP).
        photo_file = next(
            f for f in r_files.json() if f["file_type"] == "photo_resized"
        )
        r = await client.get(
            f"/api/v1/sync/file/{photo_file['content_hash']}", headers=auth(device)
        )
        assert r.status_code == 200
        assert "image/webp" in r.headers["content-type"]

    async def test_download_not_found(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        fake_hash = "a" * 64
        r = await client.get(f"/api/v1/sync/file/{fake_hash}", headers=auth(device))
        assert r.status_code == 404
        assert r.json()["error_code"] == "NOT_FOUND"

    async def test_full_sync_flow(self, client: AsyncClient, tmp_path: Path) -> None:
        """End-to-end: create profile + device, ingest, assign, fetch manifest,
        download every file listed in the manifest."""
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=42)
        await ingest_photo(client, src, profile_ids=[profile["id"]])

        r_manifest = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=auth(device)
        )
        assert r_manifest.status_code == 200
        manifest = r_manifest.json()

        for file_entry in manifest["files"]:
            r = await client.get(
                f"/api/v1/sync/file/{file_entry['content_hash']}",
                headers=auth(device),
            )
            assert r.status_code == 200, (
                f"Failed to download {file_entry['content_hash']}"
            )
            assert len(r.content) == file_entry["size_bytes"]
