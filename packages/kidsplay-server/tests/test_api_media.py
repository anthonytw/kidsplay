"""Tests for the media CRUD, ingest, and assignment API endpoints.

All tests use real PNG images (via Pillow) so that the photo ingest pipeline
can run end-to-end without mocking.  Audio ingest is not tested here to keep
the test suite fast and dependency-light — it is covered by
test_processing_pipeline.py.
"""

import io
import uuid
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


def make_png(path: Path, width: int = 200, height: int = 200, *, seed: int = 0) -> Path:
    """Write a solid-colour PNG to *path* and return the path."""
    color = (100 + seed * 30 % 155, 150, max(10, 200 - seed * 20 % 190))
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


async def create_profile(client: AsyncClient, name: str = "Leo") -> dict:
    r = await client.post("/api/v1/profiles", json={"name": name})
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
# Ingest
# ---------------------------------------------------------------------------


class TestMediaIngest:
    async def test_ingest_single_file(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src)
        assert result["total_files"] == 1
        assert result["successful"] == 1
        assert result["failed"] == 0

    async def test_ingest_directory(self, client: AsyncClient, tmp_path: Path) -> None:
        d = tmp_path / "photos"
        d.mkdir()
        make_png(d / "a.png", seed=1)
        make_png(d / "b.png", seed=2)
        r = await client.post(
            "/api/v1/media/ingest",
            json={
                "source_path": str(d),
                "media_type": "photo",
                "playlist_title": "Batch",
                "profile_ids": [],
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total_files"] == 2
        assert data["successful"] == 2

    async def test_ingest_invalid_path(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/media/ingest",
            json={
                "source_path": "/nonexistent/path/photo.png",
                "media_type": "photo",
                "playlist_title": "Test",
            },
        )
        assert r.status_code == 404
        assert r.json()["error_code"] == "PATH_NOT_FOUND"

    async def test_ingest_duplicate_is_skipped(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png", seed=3)
        r1 = await ingest_photo(client, src)
        r2 = await ingest_photo(client, src)
        assert r1["results"][0]["skipped"] is False
        assert r2["results"][0]["skipped"] is True
        assert r2["skipped"] == 1

    async def test_ingest_assigns_to_profile(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        profile = await create_profile(client)
        src = make_png(tmp_path / "photo.png", seed=4)
        await ingest_photo(client, src, profile_ids=[profile["id"]])

        # Media should appear when filtered by profile.
        r = await client.get(f"/api/v1/media?profile_id={profile['id']}")
        assert r.status_code == 200
        assert len(r.json()) == 1

    async def test_ingest_result_contains_media_id(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png", seed=5)
        result = await ingest_photo(client, src)
        media_id = result["results"][0]["media_id"]
        assert media_id is not None
        # Should be fetchable.
        r = await client.get(f"/api/v1/media/{media_id}")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


class TestMediaList:
    async def test_list_empty(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/media")
        assert r.status_code == 200
        assert r.json() == []

    async def test_list_all(self, client: AsyncClient, tmp_path: Path) -> None:
        make_png(tmp_path / "a.png", seed=1)
        make_png(tmp_path / "b.png", seed=2)
        for name in ("a.png", "b.png"):
            await ingest_photo(client, tmp_path / name)
        r = await client.get("/api/v1/media")
        assert r.status_code == 200
        assert len(r.json()) == 2

    async def test_list_filter_by_type(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png", seed=1)
        await ingest_photo(client, src)
        r_photo = await client.get("/api/v1/media?media_type=photo")
        r_music = await client.get("/api/v1/media?media_type=music")
        assert len(r_photo.json()) == 1
        assert len(r_music.json()) == 0

    async def test_list_filter_by_profile(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        p1 = await create_profile(client, "Leo")
        p2 = await create_profile(client, "Emma")
        src1 = make_png(tmp_path / "a.png", seed=1)
        src2 = make_png(tmp_path / "b.png", seed=2)
        await ingest_photo(client, src1, profile_ids=[p1["id"]])
        await ingest_photo(client, src2, profile_ids=[p2["id"]])
        r1 = await client.get(f"/api/v1/media?profile_id={p1['id']}")
        r2 = await client.get(f"/api/v1/media?profile_id={p2['id']}")
        assert len(r1.json()) == 1
        assert len(r2.json()) == 1

    async def test_list_search_query(self, client: AsyncClient, tmp_path: Path) -> None:
        src = make_png(tmp_path / "sunset.png", seed=1)
        await ingest_photo(client, src, playlist="Vacaciones")
        r = await client.get("/api/v1/media?q=sunset")
        assert r.status_code == 200
        assert len(r.json()) == 1

    async def test_list_pagination(self, client: AsyncClient, tmp_path: Path) -> None:
        for i in range(5):
            src = make_png(tmp_path / f"p{i}.png", seed=i)
            await ingest_photo(client, src)
        r = await client.get("/api/v1/media?limit=3&offset=0")
        assert len(r.json()) == 3
        r2 = await client.get("/api/v1/media?limit=3&offset=3")
        assert len(r2.json()) == 2


class TestMediaGet:
    async def test_get_existing(self, client: AsyncClient, tmp_path: Path) -> None:
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src)
        media_id = result["results"][0]["media_id"]
        r = await client.get(f"/api/v1/media/{media_id}")
        assert r.status_code == 200
        assert r.json()["id"] == media_id

    async def test_get_not_found(self, client: AsyncClient) -> None:
        r = await client.get(f"/api/v1/media/{uuid.uuid4()}")
        assert r.status_code == 404
        assert r.json()["error_code"] == "NOT_FOUND"

    async def test_get_includes_all_fields(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "beach.png", seed=1)
        result = await ingest_photo(client, src, playlist="Summer 2024")
        media_id = result["results"][0]["media_id"]
        r = await client.get(f"/api/v1/media/{media_id}")
        data = r.json()
        assert data["media_type"] == "photo"
        assert data["playlist_title"] == "Summer 2024"
        assert data["processing_status"] == "ready"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestMediaDelete:
    async def test_delete_existing(self, client: AsyncClient, tmp_path: Path) -> None:
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src)
        media_id = result["results"][0]["media_id"]
        r = await client.delete(f"/api/v1/media/{media_id}")
        assert r.status_code == 204

    async def test_delete_removes_item(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src)
        media_id = result["results"][0]["media_id"]
        await client.delete(f"/api/v1/media/{media_id}")
        r = await client.get(f"/api/v1/media/{media_id}")
        assert r.status_code == 404

    async def test_delete_not_found(self, client: AsyncClient) -> None:
        r = await client.delete(f"/api/v1/media/{uuid.uuid4()}")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


class TestMediaAssign:
    async def test_assign_to_profile(self, client: AsyncClient, tmp_path: Path) -> None:
        profile = await create_profile(client)
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src)
        media_id = result["results"][0]["media_id"]

        r = await client.post(
            f"/api/v1/media/{media_id}/assign",
            json={"profile_ids": [profile["id"]]},
        )
        assert r.status_code == 200
        assignments = r.json()
        assert len(assignments) == 1
        assert assignments[0]["profile_id"] == profile["id"]
        assert assignments[0]["media_id"] == media_id

    async def test_assign_to_multiple_profiles(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        p1 = await create_profile(client, "Leo")
        p2 = await create_profile(client, "Emma")
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src)
        media_id = result["results"][0]["media_id"]

        r = await client.post(
            f"/api/v1/media/{media_id}/assign",
            json={"profile_ids": [p1["id"], p2["id"]]},
        )
        assert r.status_code == 200
        assert len(r.json()) == 2

    async def test_assign_media_not_found(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        r = await client.post(
            f"/api/v1/media/{uuid.uuid4()}/assign",
            json={"profile_ids": [profile["id"]]},
        )
        assert r.status_code == 404

    async def test_unassign_from_profile(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        profile = await create_profile(client)
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src, profile_ids=[profile["id"]])
        media_id = result["results"][0]["media_id"]

        r = await client.delete(f"/api/v1/media/{media_id}/assign/{profile['id']}")
        assert r.status_code == 204

        # Should no longer appear in profile's media list.
        r2 = await client.get(f"/api/v1/media?profile_id={profile['id']}")
        assert r2.json() == []

    async def test_unassign_idempotent(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Unassigning a non-existent assignment returns 204 (no-op)."""
        profile = await create_profile(client)
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src)
        media_id = result["results"][0]["media_id"]

        r = await client.delete(f"/api/v1/media/{media_id}/assign/{profile['id']}")
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class TestMediaFiles:
    async def test_list_files_for_photo(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png", 300, 300, seed=1)
        result = await ingest_photo(client, src)
        media_id = result["results"][0]["media_id"]

        r = await client.get(f"/api/v1/media/{media_id}/files")
        assert r.status_code == 200
        files = r.json()
        # Photo pipeline: photo_resized + 3 thumbnails = 4 processed files.
        assert len(files) >= 1
        file_types = {f["file_type"] for f in files}
        assert "photo_resized" in file_types

    async def test_list_files_not_found(self, client: AsyncClient) -> None:
        r = await client.get(f"/api/v1/media/{uuid.uuid4()}/files")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Assign-batch
# ---------------------------------------------------------------------------


class TestAssignBatch:
    async def test_assign_batch_basic(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        profile = await create_profile(client)
        src1 = make_png(tmp_path / "a.png", seed=1)
        src2 = make_png(tmp_path / "b.png", seed=2)
        r1 = await ingest_photo(client, src1)
        r2 = await ingest_photo(client, src2)
        id1 = r1["results"][0]["media_id"]
        id2 = r2["results"][0]["media_id"]

        r = await client.post(
            "/api/v1/media/assign-batch",
            json={"profile_id": profile["id"], "media_ids": [id1, id2]},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["assigned"] == 2
        assert data["already_assigned"] == 0

    async def test_assign_batch_already_assigned(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        profile = await create_profile(client)
        src = make_png(tmp_path / "photo.png", seed=1)
        result = await ingest_photo(client, src, profile_ids=[profile["id"]])
        media_id = result["results"][0]["media_id"]

        r = await client.post(
            "/api/v1/media/assign-batch",
            json={"profile_id": profile["id"], "media_ids": [media_id]},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["assigned"] == 0
        assert data["already_assigned"] == 1

    async def test_assign_batch_missing_media(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        bad_id = str(uuid.uuid4())

        r = await client.post(
            "/api/v1/media/assign-batch",
            json={"profile_id": profile["id"], "media_ids": [bad_id]},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["assigned"] == 0
        assert len(data["errors"]) == 1
