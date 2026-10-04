"""Sync with the local transport (all-in-one mode).

The device diffs the manifest exactly as over HTTP but takes files from the
server's media store: a hard link when possible, a copy otherwise. The server
here is a real FastAPI app; its media store and the device's ``media_root``
are separate ``tmp_path`` directories on one filesystem, like a real install.

The properties that matter:

- links share the server file's inode (no second copy on the SD card);
- the device never modifies the server's files, and deleting on the device
  only unlinks, so the server's file keeps its inode and content (its backups
  rely on store files never changing);
- a local sync must not anchor the bedtime clock, since the "server"'s clock
  is the device's own.
"""

import errno
import hashlib
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_device import sync as sync_module
from kidsplay_device.config import DeviceConfig
from kidsplay_device.controls import TimeSource
from kidsplay_device.database import (
    ClockHeartbeat,
    get_all_media_ids,
    get_last_server_time,
    init_db,
)
from kidsplay_device.sync import SyncClient
from kidsplay_server.api.app import create_app

from .test_sync import (
    create_device,
    create_profile,
    fetch_manifest,
    ingest_photo,
    make_png,
    seed_admin_token,
)


@pytest.fixture
def server_app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "server.db", tmp_path / "server_media")


@pytest.fixture
async def server_client(server_app: FastAPI) -> AsyncIterator[AsyncClient]:
    token = await seed_admin_token(server_app.state.db_path)
    async with AsyncClient(
        transport=ASGITransport(app=server_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c


@pytest.fixture
async def device_http(server_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=server_app), base_url="http://test"
    ) as c:
        yield c


class _DatedTransport(httpx.AsyncBaseTransport):
    """Adds the ``Date`` header a real server sends; ASGITransport sends none."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        response.headers["Date"] = SERVER_DATE
        return response


SERVER_DATE = "Tue, 29 Sep 2026 19:30:00 GMT"


@pytest.fixture
async def dated_http(server_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Device client whose responses carry a ``Date`` header, like uvicorn's."""
    async with AsyncClient(
        transport=_DatedTransport(ASGITransport(app=server_app)),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
def store(server_app: FastAPI) -> Path:
    """The server's media store directory."""
    return server_app.state.media_store.root


@pytest.fixture
def local_cfg(tmp_path: Path, store: Path) -> DeviceConfig:
    return DeviceConfig(
        server_url="http://test",
        device_id="",
        api_key="",
        media_root=tmp_path / "device_media",
        db_path=tmp_path / "device.db",
        sync_transport="local",
        server_media_store=store,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def seeded(
    server_client: AsyncClient, tmp_path: Path, cfg: DeviceConfig, seed: int = 1
) -> tuple[dict, dict, dict]:
    """Create a profile and device and ingest one photo for it."""
    profile = await create_profile(server_client)
    device = await create_device(server_client, profile["id"])
    ingested = await ingest_photo(
        server_client,
        make_png(tmp_path / f"photo{seed}.png", seed=seed),
        profile_ids=[profile["id"]],
    )
    cfg.device_id = device["id"]
    cfg.api_key = device["api_key"]
    return profile, device, ingested


class TestLocalLinks:
    async def test_files_are_hard_links_to_the_store(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        store: Path,
        tmp_path: Path,
    ) -> None:
        _, device, _ = await seeded(server_client, tmp_path, local_cfg)

        await SyncClient(local_cfg, http_client=device_http).sync()

        files = (await fetch_manifest(server_client, device))["files"]
        assert files
        for entry in files:
            server_file = store / entry["relative_path"]
            local = local_cfg.media_root / entry["relative_path"]
            assert local.stat().st_ino == server_file.stat().st_ino
            assert local.stat().st_dev == server_file.stat().st_dev
            assert local.stat().st_nlink == 2
            assert sha256(local) == entry["content_hash"]

    async def test_device_db_is_populated_as_over_http(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        _, _, ingested = await seeded(server_client, tmp_path, local_cfg)

        await SyncClient(local_cfg, http_client=device_http).sync()

        conn = init_db(local_cfg.db_path)
        try:
            assert ingested["results"][0]["media_id"] in get_all_media_ids(conn)
        finally:
            conn.close()

    async def test_falls_back_to_a_copy_when_linking_fails(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        store: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, device, _ = await seeded(server_client, tmp_path, local_cfg)

        def cross_device(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(os, "link", cross_device)
        await SyncClient(local_cfg, http_client=device_http).sync()

        files = (await fetch_manifest(server_client, device))["files"]
        assert files
        for entry in files:
            server_file = store / entry["relative_path"]
            local = local_cfg.media_root / entry["relative_path"]
            assert local.stat().st_ino != server_file.stat().st_ino
            assert local.stat().st_nlink == 1
            assert sha256(local) == entry["content_hash"]
            assert not local.with_name(local.name + ".part").exists()

    async def test_second_sync_is_a_noop(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        await seeded(server_client, tmp_path, local_cfg)
        client = SyncClient(local_cfg, http_client=device_http)
        await client.sync()
        before = sorted(p.stat().st_ino for p in local_cfg.media_root.rglob("*.webp"))

        await client.sync()

        after = sorted(p.stat().st_ino for p in local_cfg.media_root.rglob("*.webp"))
        assert before == after


class TestServerFilesAreNeverTouched:
    async def test_removing_media_unlinks_only_the_device_name(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        store: Path,
        tmp_path: Path,
    ) -> None:
        profile, device, ingested = await seeded(server_client, tmp_path, local_cfg)
        client = SyncClient(local_cfg, http_client=device_http)
        await client.sync()
        entries = (await fetch_manifest(server_client, device))["files"]
        server_before = {
            e["relative_path"]: (
                (store / e["relative_path"]).stat().st_ino,
                sha256(store / e["relative_path"]),
            )
            for e in entries
        }

        media_id = ingested["results"][0]["media_id"]
        r = await server_client.delete(
            f"/api/v1/media/{media_id}/assign/{profile['id']}"
        )
        assert r.status_code == 204
        await client.sync()

        for entry in entries:
            rel = entry["relative_path"]
            assert not (local_cfg.media_root / rel).exists()
            server_file = store / rel
            assert server_file.stat().st_ino == server_before[rel][0]
            assert sha256(server_file) == server_before[rel][1] == entry["content_hash"]
            assert server_file.stat().st_nlink == 1

    async def test_deleting_media_on_the_server_removes_the_device_link(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        store: Path,
        tmp_path: Path,
    ) -> None:
        _, device, ingested = await seeded(server_client, tmp_path, local_cfg)
        client = SyncClient(local_cfg, http_client=device_http)
        await client.sync()
        entries = (await fetch_manifest(server_client, device))["files"]

        r = await server_client.delete(
            f"/api/v1/media/{ingested['results'][0]['media_id']}"
        )
        assert r.status_code == 204
        await client.sync()

        for entry in entries:
            assert not (local_cfg.media_root / entry["relative_path"]).exists()
            # The server keeps its stored file; only the DB rows are gone.
            server_file = store / entry["relative_path"]
            assert sha256(server_file) == entry["content_hash"]

    async def test_download_file_never_writes_through_a_hard_link(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        store: Path,
        tmp_path: Path,
    ) -> None:
        """The HTTP download replaces its target instead of overwriting it, so
        it cannot modify a store file the target is linked to."""
        _, device, _ = await seeded(server_client, tmp_path, local_cfg)
        entries = (await fetch_manifest(server_client, device))["files"]
        wanted, victim = entries[0], entries[1]
        victim_file = store / victim["relative_path"]
        linked = tmp_path / "linked"
        os.link(victim_file, linked)

        await SyncClient(local_cfg, http_client=device_http).download_file(
            wanted["content_hash"], linked
        )

        assert sha256(victim_file) == victim["content_hash"]
        assert sha256(linked) == wanted["content_hash"]
        assert victim_file.stat().st_nlink == 1

    async def test_fetch_never_opens_store_files_for_writing(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        store: Path,
        tmp_path: Path,
    ) -> None:
        await seeded(server_client, tmp_path, local_cfg)
        opened_for_writing: list[str] = []
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC

        def audit(event: str, args: tuple[object, ...]) -> None:
            if event != "open" or not _watching:
                return
            path, _mode, flags = args
            if isinstance(flags, int) and flags & write_flags:
                opened_for_writing.append(os.fsdecode(str(path)))

        # Audit hooks cannot be removed; ``_watching`` switches this one off.
        _watching.append(True)
        sys.addaudithook(audit)
        try:
            control = tmp_path / "control"
            control.write_text("x")  # proves the hook sees writes
            await SyncClient(local_cfg, http_client=device_http).sync()
        finally:
            _watching.clear()

        store_root = str(store.resolve())
        assert str(control.resolve()) in opened_for_writing
        assert not [p for p in opened_for_writing if p.startswith(store_root)]
        # ...and none of them was a file the device linked from the store.
        linked = {
            str(p.resolve()) for p in local_cfg.media_root.rglob("*") if p.is_file()
        }
        assert not linked & set(opened_for_writing)


_watching: list[bool] = []


class TestClockIsNotAnchoredByALocalServer:
    async def test_local_sync_does_not_report_server_time(
        self,
        server_client: AsyncClient,
        dated_http: AsyncClient,
        local_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        await seeded(server_client, tmp_path, local_cfg)
        seen: list[datetime] = []

        await SyncClient(
            local_cfg, http_client=dated_http, on_server_time=seen.append
        ).sync()

        assert seen == []
        conn = init_db(local_cfg.db_path)
        try:
            assert get_last_server_time(conn) is None
        finally:
            conn.close()

    async def test_restored_clock_stays_untrusted_after_a_local_sync(
        self,
        server_client: AsyncClient,
        dated_http: AsyncClient,
        local_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        """A clock restored from the last shutdown must not become trusted
        just because it agrees with the (local) server's Date header."""
        await seeded(server_client, tmp_path, local_cfg)
        now = datetime.now(UTC)
        # Last heartbeat from another boot, a few minutes before this "boot":
        # the signature of a clock restored from the last shutdown.
        heartbeat = ClockHeartbeat(
            wall=now - timedelta(minutes=2), boot_id="previous-boot", trusted=True
        )
        source = TimeSource(heartbeat=heartbeat, boot_id="this-boot")
        assert source.now() is None

        await SyncClient(
            local_cfg, http_client=dated_http, on_server_time=source.note_server_time
        ).sync()

        assert not source.synced_since_boot
        assert source.now() is None
        assert source.heartbeat().trusted is False

    async def test_http_sync_still_anchors_the_clock(
        self,
        server_client: AsyncClient,
        dated_http: AsyncClient,
        local_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        await seeded(server_client, tmp_path, local_cfg)
        http_cfg = DeviceConfig(
            server_url=local_cfg.server_url,
            device_id=local_cfg.device_id,
            api_key=local_cfg.api_key,
            media_root=local_cfg.media_root,
            db_path=local_cfg.db_path,
        )
        seen: list[datetime] = []

        await SyncClient(
            http_cfg, http_client=dated_http, on_server_time=seen.append
        ).sync()

        assert seen == [datetime(2026, 9, 29, 19, 30, tzinfo=UTC)]


def _http_config(cfg: DeviceConfig) -> DeviceConfig:
    return DeviceConfig(
        server_url=cfg.server_url,
        device_id="d",
        api_key="k",
        media_root=cfg.media_root,
        db_path=cfg.db_path,
    )


class TestRetryAfterFailure:
    def test_local_transport_retries_quickly_after_a_failure(
        self, local_cfg: DeviceConfig
    ) -> None:
        client = SyncClient(local_cfg)
        assert client._delay_after(ok=False) == sync_module._LOCAL_RETRY_SECONDS

    def test_local_transport_waits_the_interval_after_success(
        self, local_cfg: DeviceConfig
    ) -> None:
        client = SyncClient(local_cfg)
        assert client._delay_after(ok=True) == local_cfg.sync_interval_seconds

    def test_retry_is_never_longer_than_the_interval(
        self, local_cfg: DeviceConfig
    ) -> None:
        local_cfg.sync_interval_seconds = 10
        assert SyncClient(local_cfg)._delay_after(ok=False) == 10

    def test_http_transport_waits_the_full_interval_after_a_failure(
        self, local_cfg: DeviceConfig
    ) -> None:
        cfg = _http_config(local_cfg)
        assert SyncClient(cfg)._delay_after(ok=False) == cfg.sync_interval_seconds

    async def test_sync_once_reports_failure_when_the_server_is_down(
        self, local_cfg: DeviceConfig
    ) -> None:
        local_cfg.server_url = "http://127.0.0.1:9"  # the discard port: refused
        assert await SyncClient(local_cfg)._sync_once() is False

    async def test_sync_once_reports_success(
        self,
        server_client: AsyncClient,
        device_http: AsyncClient,
        local_cfg: DeviceConfig,
        tmp_path: Path,
    ) -> None:
        await seeded(server_client, tmp_path, local_cfg)
        assert await SyncClient(local_cfg, http_client=device_http)._sync_once()
