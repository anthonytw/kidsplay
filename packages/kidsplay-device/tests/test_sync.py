"""Integration tests for the device sync client.

Uses a real FastAPI test server (same pattern as kidsplay-server tests)
populated with a profile, device, and ingested media.  Tests verify the
full sync lifecycle: first sync downloads everything, second sync returns
304 (no-op), adding media triggers a differential download, and removing
an assignment triggers local file deletion.

Both the server's media store and the device's media_root use ``tmp_path``
fixtures — no real files are created outside the test sandbox.
"""

import io
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from kidsplay_device.config import DeviceConfig
from kidsplay_device.database import (
    get_all_media_ids,
    get_photos,
    get_sync_state,
    init_db,
)
from kidsplay_device.sync import SyncClient
from kidsplay_server.api.app import create_app
from kidsplay_server.auth import (
    create_admin_token,
    init_auth_db,
    set_initial_admin_password,
)
from kidsplay_server.database import configure_conn
from kidsplay_server.database import init_db as init_server_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server_app(tmp_path: Path) -> FastAPI:
    """FastAPI app wired to isolated tmp_path db + media store."""
    return create_app(tmp_path / "server.db", tmp_path / "server_media")


async def seed_admin_token(db_path: Path) -> str:
    """Set up the server's admin account and return an admin API token."""
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_server_db(conn)
        await init_auth_db(conn)
        await set_initial_admin_password(conn, "device-tests-password")
        token = await create_admin_token(conn, "device-tests")
        await conn.commit()
    return token.token


@pytest.fixture
async def server_client(server_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """httpx.AsyncClient acting as the admin, to populate the test server."""
    token = await seed_admin_token(server_app.state.db_path)
    async with AsyncClient(
        transport=ASGITransport(app=server_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c


@pytest.fixture
async def device_http(server_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Credential-free httpx.AsyncClient for the device's SyncClient.

    Kept separate from ``server_client`` so sync is proven to work with the
    device's own API key alone, never the admin token.
    """
    async with AsyncClient(
        transport=ASGITransport(app=server_app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def device_cfg(tmp_path: Path) -> DeviceConfig:
    """DeviceConfig pointing at tmp_path paths (populated by tests)."""
    return DeviceConfig(
        server_url="http://test",
        device_id="",  # filled in by each test after device creation
        api_key="",
        media_root=tmp_path / "device_media",
        db_path=tmp_path / "device.db",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_png(path: Path, seed: int = 0) -> Path:
    """Write a small RGB PNG to *path* and return it."""
    color = (100 + seed * 30 % 155, 150, max(10, 200 - seed * 20 % 190))
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color=color).save(buf, format="PNG")
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


async def fetch_manifest(client: AsyncClient, device: dict) -> dict:
    """GET a device's manifest with that device's bearer token.

    The sync endpoints require auth, so a test asserting against the manifest
    has to present the same credentials the device does -- without them the
    call 401s and the assertion fails later and less legibly, as a KeyError on
    the missing "files" key.
    """
    r = await client.get(
        f"/api/v1/devices/{device['id']}/manifest",
        headers={"Authorization": f"Bearer {device['api_key']}"},
    )
    assert r.status_code == 200, f"manifest fetch failed: {r.status_code} {r.text}"
    return r.json()


def make_sync_client(
    cfg: DeviceConfig, device: dict, http_client: AsyncClient
) -> SyncClient:
    """Return a SyncClient wired to the test server."""
    cfg.device_id = device["id"]
    cfg.api_key = device["api_key"]
    return SyncClient(cfg, http_client=http_client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFirstSync:
    async def test_first_sync_downloads_all_files(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        device_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        """First sync should download every file listed in the manifest."""
        profile = await create_profile(server_client)
        device = await create_device(server_client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=1)
        await ingest_photo(server_client, src, profile_ids=[profile["id"]])

        client = make_sync_client(device_cfg, device, device_http)
        await client.sync()

        # Every file in the manifest should now exist locally.
        manifest = await fetch_manifest(server_client, device)
        for file_entry in manifest["files"]:
            local = device_cfg.media_root / file_entry["relative_path"]
            assert local.exists(), f"Missing: {file_entry['relative_path']}"

    async def test_first_sync_populates_device_db(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        device_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        """First sync should write media metadata into the device SQLite."""
        profile = await create_profile(server_client)
        device = await create_device(server_client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=2)
        result = await ingest_photo(
            server_client, src, playlist="Beach 2025", profile_ids=[profile["id"]]
        )
        media_id = result["results"][0]["media_id"]

        client = make_sync_client(device_cfg, device, device_http)
        await client.sync()

        conn = init_db(device_cfg.db_path)
        try:
            ids = get_all_media_ids(conn)
            assert media_id in ids

            photos = get_photos(conn)
            assert len(photos) == 1
            assert photos[0].playlist_title == "Beach 2025"
            assert photos[0].photo_path is not None
        finally:
            conn.close()

    async def test_first_sync_stores_manifest_hash(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        device_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        """After sync the device DB should store last_manifest_hash."""
        profile = await create_profile(server_client)
        device = await create_device(server_client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=3)
        await ingest_photo(server_client, src, profile_ids=[profile["id"]])

        client = make_sync_client(device_cfg, device, device_http)
        await client.sync()

        expected_hash = (await fetch_manifest(server_client, device))["manifest_hash"]

        conn = init_db(device_cfg.db_path)
        try:
            stored = get_sync_state(conn, "last_manifest_hash")
            assert stored == expected_hash
        finally:
            conn.close()


class TestSecondSyncNoChanges:
    async def test_second_sync_is_304_noop(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        device_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        """Second sync with no server changes should produce no extra downloads."""
        profile = await create_profile(server_client)
        device = await create_device(server_client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=4)
        await ingest_photo(server_client, src, profile_ids=[profile["id"]])

        client = make_sync_client(device_cfg, device, device_http)
        await client.sync()  # first sync

        # Collect mtimes after first sync.
        file_entries = (await fetch_manifest(server_client, device))["files"]
        mtimes_before = {
            e["relative_path"]: (device_cfg.media_root / e["relative_path"])
            .stat()
            .st_mtime
            for e in file_entries
        }

        await client.sync()  # second sync — should 304

        mtimes_after = {
            e["relative_path"]: (device_cfg.media_root / e["relative_path"])
            .stat()
            .st_mtime
            for e in file_entries
        }
        assert mtimes_before == mtimes_after, "Files were re-written on second sync"


class TestDifferentialSync:
    async def test_new_media_downloads_only_new_file(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        device_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        """Adding media after first sync should download only the new file."""
        profile = await create_profile(server_client)
        device = await create_device(server_client, profile["id"])
        src1 = make_png(tmp_path / "photo1.png", seed=5)
        await ingest_photo(server_client, src1, profile_ids=[profile["id"]])

        client = make_sync_client(device_cfg, device, device_http)
        await client.sync()  # first sync

        # Record files present after first sync.
        manifest1 = await fetch_manifest(server_client, device)
        paths_after_first = {e["relative_path"] for e in manifest1["files"]}

        # Ingest a second photo and assign it.
        src2 = make_png(tmp_path / "photo2.png", seed=6)
        result2 = await ingest_photo(server_client, src2, profile_ids=[profile["id"]])
        media_id2 = result2["results"][0]["media_id"]

        await client.sync()  # second sync — differential

        manifest2 = await fetch_manifest(server_client, device)
        all_paths = {e["relative_path"] for e in manifest2["files"]}
        new_paths = all_paths - paths_after_first

        # New files should now exist locally.
        for rel in new_paths:
            assert (device_cfg.media_root / rel).exists(), f"Missing new file: {rel}"

        # Old files should still exist (not re-downloaded / deleted).
        for rel in paths_after_first:
            assert (device_cfg.media_root / rel).exists(), f"Old file gone: {rel}"

        # Device DB should include the new media item.
        conn = init_db(device_cfg.db_path)
        try:
            assert media_id2 in get_all_media_ids(conn)
        finally:
            conn.close()


class TestRemovalSync:
    async def test_removed_assignment_deletes_local_file(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        device_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        """Removing a profile assignment should delete the local file on next sync."""
        profile = await create_profile(server_client)
        device = await create_device(server_client, profile["id"])
        src = make_png(tmp_path / "photo.png", seed=7)
        result = await ingest_photo(server_client, src, profile_ids=[profile["id"]])
        media_id = result["results"][0]["media_id"]

        client = make_sync_client(device_cfg, device, device_http)
        await client.sync()  # first sync — file present

        local_paths = [
            device_cfg.media_root / e["relative_path"]
            for e in (await fetch_manifest(server_client, device))["files"]
        ]
        assert all(p.exists() for p in local_paths), (
            "Files should exist after first sync"
        )

        # Remove the assignment.
        r_del = await server_client.delete(
            f"/api/v1/media/{media_id}/assign/{profile['id']}"
        )
        assert r_del.status_code == 204

        await client.sync()  # second sync — should delete local files

        for p in local_paths:
            assert not p.exists(), f"Stale file not deleted: {p}"

        # Device DB should also be clean.
        conn = init_db(device_cfg.db_path)
        try:
            assert media_id not in get_all_media_ids(conn)
        finally:
            conn.close()

    async def test_removed_assignment_keeps_other_files(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        device_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        """Removing one item's assignment must not delete the other item's files."""
        profile = await create_profile(server_client)
        device = await create_device(server_client, profile["id"])

        src1 = make_png(tmp_path / "photo1.png", seed=8)
        src2 = make_png(tmp_path / "photo2.png", seed=9)
        r1 = await ingest_photo(server_client, src1, profile_ids=[profile["id"]])
        r2 = await ingest_photo(server_client, src2, profile_ids=[profile["id"]])
        media_id1 = r1["results"][0]["media_id"]
        media_id2 = r2["results"][0]["media_id"]

        client = make_sync_client(device_cfg, device, device_http)
        await client.sync()

        # Remove only the first item's assignment.
        await server_client.delete(f"/api/v1/media/{media_id1}/assign/{profile['id']}")
        await client.sync()

        conn = init_db(device_cfg.db_path)
        try:
            ids = get_all_media_ids(conn)
            assert media_id1 not in ids
            assert media_id2 in ids
        finally:
            conn.close()


class TestSyncErrorHandling:
    async def test_sync_survives_server_unreachable(
        self,
        device_cfg: DeviceConfig,
    ) -> None:
        """sync() must not raise even when the server is unreachable."""
        device_cfg.device_id = "00000000-0000-0000-0000-000000000000"
        device_cfg.api_key = "bad"
        # No http_client injected → will try to connect to server_url which
        # won't resolve; we rely on the client-ctx error path.
        bad_cfg = DeviceConfig(
            server_url="http://does-not-exist.invalid",
            device_id="00000000-0000-0000-0000-000000000000",
            api_key="bad",
            media_root=device_cfg.media_root,
            db_path=device_cfg.db_path,
        )
        client = SyncClient(bad_cfg)
        # Should complete without raising.
        await client.sync()

    async def test_sync_does_not_crash_on_404_device(
        self,
        server_client: AsyncClient,
        device_cfg: DeviceConfig,
    ) -> None:
        """sync() should not raise when the device_id is unknown (404)."""
        device_cfg.device_id = "00000000-0000-0000-0000-000000000099"
        device_cfg.api_key = "whatever"
        client = SyncClient(device_cfg, http_client=server_client)
        await client.sync()  # should not raise
