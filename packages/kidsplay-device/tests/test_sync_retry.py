"""A sync that could not fetch every file must be retried, not remembered.

Storing the manifest hash after a partial sync made the next cycle a 304, so
files that failed to arrive (a mistyped or unmounted ``server_media_store``, a
download that errored) were never fetched again until the manifest changed.
These tests run a real server app and break either the media store (local
transport) or the file download endpoint (HTTP transport), then repair it.
"""

import logging
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_device.config import DeviceConfig
from kidsplay_device.database import get_all_media_ids, get_sync_state, init_db
from kidsplay_device.sync import SyncClient
from kidsplay_server.api.app import create_app

from .test_sync import (
    create_device,
    create_profile,
    ingest_photo,
    make_png,
    seed_admin_token,
)


class _Spy(httpx.AsyncBaseTransport):
    """Wraps the ASGI app; records manifest statuses, can fail one file download."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.manifest_statuses: list[int] = []
        self.fail_one_download = False
        self._victim: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/sync/file/") and self.fail_one_download:
            # Always the same file: the first one asked for.
            self._victim = self._victim or path
            if path == self._victim:
                return httpx.Response(500, request=request)
        response = await self._inner.handle_async_request(request)
        if path.endswith("/manifest"):
            self.manifest_statuses.append(response.status_code)
        return response


@pytest.fixture
def server_app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "server.db", tmp_path / "server_media")


@pytest.fixture
def spy(server_app: FastAPI) -> _Spy:
    return _Spy(ASGITransport(app=server_app))


@pytest.fixture
async def device_http(spy: _Spy) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=spy, base_url="http://test") as c:
        yield c


@pytest.fixture
async def credentials(server_app: FastAPI, tmp_path: Path) -> tuple[str, str]:
    """(device id, api key) of a device on a server holding one photo."""
    token = await seed_admin_token(server_app.state.db_path)
    async with AsyncClient(
        transport=ASGITransport(app=server_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as admin:
        profile = await create_profile(admin)
        device = await create_device(admin, profile["id"])
        await ingest_photo(
            admin,
            make_png(tmp_path / "photo.png", seed=3),
            profile_ids=[profile["id"]],
        )
    return device["id"], device["api_key"]


def make_cfg(
    tmp_path: Path,
    server_app: FastAPI,
    credentials: tuple[str, str],
    *,
    local: bool,
) -> DeviceConfig:
    return DeviceConfig(
        server_url="http://test",
        device_id=credentials[0],
        api_key=credentials[1],
        media_root=tmp_path / "device_media",
        db_path=tmp_path / "device.db",
        sync_transport="local" if local else "http",
        server_media_store=server_app.state.media_store.root if local else None,
    )


def _hash(cfg: DeviceConfig) -> str | None:
    conn = init_db(cfg.db_path)
    try:
        return get_sync_state(conn, "last_manifest_hash")
    finally:
        conn.close()


def _media_ids(cfg: DeviceConfig) -> set[str]:
    conn = init_db(cfg.db_path)
    try:
        return get_all_media_ids(conn)
    finally:
        conn.close()


def _files(cfg: DeviceConfig) -> list[Path]:
    if not cfg.media_root.exists():
        return []
    return [p for p in cfg.media_root.rglob("*") if p.is_file()]


class TestFailedFetchesAreRetried:
    async def test_missing_store_then_restored(
        self,
        server_app: FastAPI,
        credentials: tuple[str, str],
        device_http: AsyncClient,
        tmp_path: Path,
    ) -> None:
        cfg = make_cfg(tmp_path, server_app, credentials, local=True)
        store: Path = server_app.state.media_store.root
        away = tmp_path / "store_away"
        shutil.move(store, away)
        client = SyncClient(cfg, http_client=device_http)

        assert await client._sync_once() is False
        assert _files(cfg) == []
        assert _hash(cfg) is None
        assert _media_ids(cfg) == set()

        shutil.move(away, store)
        assert await client._sync_once() is True
        assert len(_files(cfg)) > 0
        assert _hash(cfg) is not None
        assert len(_media_ids(cfg)) == 1

    async def test_failed_download_is_retried(
        self,
        server_app: FastAPI,
        credentials: tuple[str, str],
        spy: _Spy,
        device_http: AsyncClient,
        tmp_path: Path,
    ) -> None:
        cfg = make_cfg(tmp_path, server_app, credentials, local=False)
        spy.fail_one_download = True
        client = SyncClient(cfg, http_client=device_http)

        assert await client._sync_once() is False
        assert _hash(cfg) is None
        assert _media_ids(cfg) == set()  # no rows pointing at missing files

        spy.fail_one_download = False
        assert await client._sync_once() is True
        assert _hash(cfg) is not None
        assert len(_files(cfg)) > 0
        assert len(_media_ids(cfg)) == 1
        assert 304 not in spy.manifest_statuses

    @pytest.mark.parametrize("local", [True, False], ids=["local", "http"])
    async def test_success_stores_hash_and_next_sync_is_304(
        self,
        local: bool,
        server_app: FastAPI,
        credentials: tuple[str, str],
        spy: _Spy,
        device_http: AsyncClient,
        tmp_path: Path,
    ) -> None:
        cfg = make_cfg(tmp_path, server_app, credentials, local=local)
        client = SyncClient(cfg, http_client=device_http)

        assert await client._sync_once() is True
        assert _hash(cfg) is not None
        assert await client._sync_once() is True

        assert spy.manifest_statuses == [200, 304]


class TestUnusableStore:
    @pytest.mark.parametrize("kind", ["missing", "file"])
    async def test_one_clear_error_not_one_per_file(
        self,
        kind: str,
        server_app: FastAPI,
        credentials: tuple[str, str],
        device_http: AsyncClient,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = make_cfg(tmp_path, server_app, credentials, local=True)
        bad = tmp_path / "nowhere"
        if kind == "file":
            bad.write_text("not a directory")
        cfg.server_media_store = bad
        caplog.set_level(logging.DEBUG, logger="kidsplay_device")

        assert await SyncClient(cfg, http_client=device_http)._sync_once() is False

        problems = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(problems) == 1
        assert "server_media_store" in problems[0].getMessage()
        assert str(bad) in problems[0].getMessage()
        assert all(r.exc_info is None for r in caplog.records)
        assert _hash(cfg) is None
