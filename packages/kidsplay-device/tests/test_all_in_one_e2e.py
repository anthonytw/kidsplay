"""All-in-one mode end to end on one machine, as ``docs/ALL_IN_ONE.md`` runs it.

``kidsplay-allinone`` sets up the server data and the player config; the server
then runs on that data (a real FastAPI app), media is imported through its
API as the admin, and the player syncs with the config the command wrote.
Nothing is mocked except the systemd calls and the network transport
(``ASGITransport`` in place of localhost:8000).
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_device.config import DeviceConfig
from kidsplay_device.database import get_photos, init_db
from kidsplay_device.sync import SyncClient
from kidsplay_server import allinone
from kidsplay_server.api.app import create_app

from .test_sync import ingest_photo, make_png, seed_admin_token


@dataclass
class Running:
    """The server on the installed data, and an admin client for it."""

    app: FastAPI
    admin: AsyncClient


@pytest.fixture
async def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run ``kidsplay-allinone``; return the player's config path."""
    monkeypatch.setattr(allinone, "_systemctl", lambda *a: None)
    monkeypatch.setattr(allinone, "_running_as_root", lambda: False)  # CI may be root
    config = tmp_path / "home" / "config.json"
    args = [
        "--data-dir", str(tmp_path / "data"),
        "--config-path", str(config),
        "--media-root", str(tmp_path / "home" / "media"),
        "--player-db", str(tmp_path / "home" / "db.sqlite"),
        "--no-systemd",
        "--admin-password", "all-in-one-tests",
        "--profile-name", "Leo",
    ]  # fmt: skip
    result = await asyncio.to_thread(CliRunner().invoke, allinone.main, args)
    assert result.exit_code == 0, result.output
    return config


@pytest.fixture
async def server(installed: Path, tmp_path: Path) -> AsyncIterator[Running]:
    app = create_app(tmp_path / "data" / "db.sqlite", tmp_path / "data" / "media")
    token = await seed_admin_token(app.state.db_path)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
        headers={"Authorization": f"Bearer {token}"},
    ) as admin:
        yield Running(app, admin)


async def test_imported_media_reaches_the_player_by_hard_link(
    server: Running, installed: Path, tmp_path: Path
) -> None:
    config = DeviceConfig.load(installed)
    assert config.is_local

    admin = server.admin
    profile_id = (await admin.get("/api/v1/profiles")).json()[0]["id"]
    ingested = await ingest_photo(
        admin, make_png(tmp_path / "p.png", seed=3), profile_ids=[profile_id]
    )
    media_id = ingested["results"][0]["media_id"]

    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url=config.server_url
    ) as device_http:
        await SyncClient(config, http_client=device_http).sync()

    store = tmp_path / "data" / "media"
    linked = [p for p in config.media_root.rglob("*") if p.is_file()]
    assert linked
    for path in linked:
        server_file = store / path.relative_to(config.media_root)
        assert path.stat().st_ino == server_file.stat().st_ino

    conn = init_db(config.db_path)
    try:
        assert [p.media_id for p in get_photos(conn)] == [media_id]
    finally:
        conn.close()
