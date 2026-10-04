"""Tests for the profile and device API endpoints.

All tests use a real SQLite database in tmp_path and an httpx AsyncClient
pointed at the FastAPI app directly (no server process).
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_server.api.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """Return a fresh FastAPI app with an isolated DB and media store."""
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def client(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    """Yield an httpx AsyncClient wired to the test app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=admin_headers,
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def create_profile(client: AsyncClient, name: str = "Leo") -> dict:
    r = await client.post("/api/v1/profiles", json={"name": name})
    assert r.status_code == 201
    return r.json()


async def create_device(
    client: AsyncClient,
    profile_id: str,
    name: str = "Leo's GameBoy",
) -> dict:
    r = await client.post(
        "/api/v1/devices",
        json={"name": name, "profile_id": profile_id},
    )
    assert r.status_code == 201
    return r.json()


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


class TestProfiles:
    async def test_create_profile_returns_201(self, client: AsyncClient) -> None:
        r = await client.post("/api/v1/profiles", json={"name": "Leo"})
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "Leo"
        assert "id" in data
        assert "created_at" in data

    async def test_list_profiles_empty(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/profiles")
        assert r.status_code == 200
        assert r.json() == []

    async def test_list_profiles_returns_all(self, client: AsyncClient) -> None:
        await create_profile(client, "Leo")
        await create_profile(client, "Emma")
        r = await client.get("/api/v1/profiles")
        assert r.status_code == 200
        names = {p["name"] for p in r.json()}
        assert names == {"Leo", "Emma"}

    async def test_get_profile_existing(self, client: AsyncClient) -> None:
        profile = await create_profile(client, "Leo")
        r = await client.get(f"/api/v1/profiles/{profile['id']}")
        assert r.status_code == 200
        assert r.json()["name"] == "Leo"

    async def test_get_profile_not_found(self, client: AsyncClient) -> None:
        r = await client.get(f"/api/v1/profiles/{uuid.uuid4()}")
        assert r.status_code == 404
        body = r.json()
        assert "detail" in body
        assert body["error_code"] == "NOT_FOUND"

    async def test_delete_profile_returns_204(self, client: AsyncClient) -> None:
        profile = await create_profile(client, "Leo")
        r = await client.delete(f"/api/v1/profiles/{profile['id']}")
        assert r.status_code == 204

    async def test_delete_profile_not_found(self, client: AsyncClient) -> None:
        r = await client.delete(f"/api/v1/profiles/{uuid.uuid4()}")
        assert r.status_code == 404

    async def test_delete_profile_with_device_fails(self, client: AsyncClient) -> None:
        """Deleting a profile that still has a linked device returns 409."""
        profile = await create_profile(client, "Leo")
        await create_device(client, profile["id"])
        r = await client.delete(f"/api/v1/profiles/{profile['id']}")
        assert r.status_code == 409
        assert r.json()["error_code"] == "PROFILE_HAS_DEVICES"

    async def test_deleted_profile_not_in_list(self, client: AsyncClient) -> None:
        profile = await create_profile(client, "Leo")
        await client.delete(f"/api/v1/profiles/{profile['id']}")
        r = await client.get("/api/v1/profiles")
        ids = [p["id"] for p in r.json()]
        assert profile["id"] not in ids


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class TestDevices:
    async def test_create_device_returns_201(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        r = await client.post(
            "/api/v1/devices",
            json={"name": "Leo's GameBoy", "profile_id": profile["id"]},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "Leo's GameBoy"
        assert data["profile_id"] == profile["id"]
        assert "api_key" in data
        assert len(data["api_key"]) == 32  # uuid4().hex

    async def test_create_device_unknown_profile(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/devices",
            json={"name": "Leo's GameBoy", "profile_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404

    async def test_list_devices_empty(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/devices")
        assert r.status_code == 200
        assert r.json() == []

    async def test_list_devices_returns_all(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        await create_device(client, profile["id"], "Device A")
        await create_device(client, profile["id"], "Device B")
        r = await client.get("/api/v1/devices")
        assert r.status_code == 200
        assert len(r.json()) == 2

    async def test_get_device_existing(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.get(f"/api/v1/devices/{device['id']}")
        assert r.status_code == 200
        assert r.json()["id"] == device["id"]

    async def test_get_device_not_found(self, client: AsyncClient) -> None:
        r = await client.get(f"/api/v1/devices/{uuid.uuid4()}")
        assert r.status_code == 404
        assert r.json()["error_code"] == "NOT_FOUND"

    async def test_delete_device_returns_204(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.delete(f"/api/v1/devices/{device['id']}")
        assert r.status_code == 204

    async def test_delete_device_not_found(self, client: AsyncClient) -> None:
        r = await client.delete(f"/api/v1/devices/{uuid.uuid4()}")
        assert r.status_code == 404

    async def test_delete_device_removes_from_list(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        await client.delete(f"/api/v1/devices/{device['id']}")
        r = await client.get("/api/v1/devices")
        assert r.json() == []

    async def test_patch_device_name(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.patch(
            f"/api/v1/devices/{device['id']}",
            json={"name": "New Name"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "New Name"
        # Other fields unchanged
        assert r.json()["profile_id"] == profile["id"]

    async def test_patch_device_profile(self, client: AsyncClient) -> None:
        profile1 = await create_profile(client, "Leo")
        profile2 = await create_profile(client, "Emma")
        device = await create_device(client, profile1["id"])
        r = await client.patch(
            f"/api/v1/devices/{device['id']}",
            json={"profile_id": profile2["id"]},
        )
        assert r.status_code == 200
        assert r.json()["profile_id"] == profile2["id"]

    async def test_patch_device_display_dimensions(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.patch(
            f"/api/v1/devices/{device['id']}",
            json={"display_width": 800, "display_height": 600},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["display_width"] == 800
        assert data["display_height"] == 600

    async def test_patch_device_not_found(self, client: AsyncClient) -> None:
        r = await client.patch(
            f"/api/v1/devices/{uuid.uuid4()}",
            json={"name": "Ghost"},
        )
        assert r.status_code == 404

    async def test_patch_device_unknown_profile(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        r = await client.patch(
            f"/api/v1/devices/{device['id']}",
            json={"profile_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404

    async def test_default_display_dimensions(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        assert device["display_width"] == 640
        assert device["display_height"] == 480


# ---------------------------------------------------------------------------
# Assign-group
# ---------------------------------------------------------------------------


class TestAssignGroup:
    async def test_assign_group_empty_library(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        r = await client.post(
            f"/api/v1/profiles/{profile['id']}/assign-group",
            json={"playlist_title": "Nonexistent Album"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total_matched"] == 0
        assert data["assigned"] == 0

    async def test_assign_group_profile_not_found(self, client: AsyncClient) -> None:
        r = await client.post(
            f"/api/v1/profiles/{uuid.uuid4()}/assign-group",
            json={"playlist_title": "Album"},
        )
        assert r.status_code == 404
