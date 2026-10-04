"""Acceptance: a parent changes a profile's theme; the device follows after sync.

A real server (in process), the real ``SyncClient`` and the real
``MusicPlayerApp`` running headless. The parent's actions are the API calls the
web UI's settings page makes.
"""

import io
import wave
from pathlib import Path

import aiosqlite
import pygame
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from kidsplay_device import views
from kidsplay_device.sync import SyncClient
from kidsplay_server.api.app import create_app
from kidsplay_server.auth import (
    create_admin_token,
    init_auth_db,
    set_initial_admin_password,
)
from kidsplay_server.database import configure_conn, init_db

from . import scenes

pytestmark = pytest.mark.usefixtures("headless")

COLORS = {
    "bg": "#101820",
    "surface": "#1c2a36",
    "surface_sel": "#2c4560",
    "primary": "#f2a900",
    "text": "#e8eef2",
    "text_dim": "#8a9aa6",
    "text_bright": "#ffffff",
    "accent": "#5ec8ff",
    "progress_bg": "#2a3a48",
}


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1600, 900), (20, 90, 160)).save(buf, "PNG")
    return buf.getvalue()


def wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


async def admin_token(db_path: Path) -> str:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await init_auth_db(conn)
        await set_initial_admin_password(conn, "device-tests-password")
        token = await create_admin_token(conn, "device-tests")
        await conn.commit()
    return token.token


async def test_theme_change_reaches_the_device_after_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = create_app(tmp_path / "server.db", tmp_path / "server_media")
    token = await admin_token(server.state.db_path)
    transport = ASGITransport(app=server)
    async with (
        AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as admin,
        AsyncClient(transport=transport, base_url="http://test") as device_http,
    ):
        profile = (await admin.post("/api/v1/profiles", json={"name": "Leo"})).json()
        device = (
            await admin.post(
                "/api/v1/devices", json={"name": "GB", "profile_id": profile["id"]}
            )
        ).json()
        settings_url = f"/api/v1/profiles/{profile['id']}/settings"

        # A custom theme with a background and a UI sound.
        await admin.put(
            "/api/v1/themes/ocean", json={"name": "Ocean", "colors": COLORS}
        )
        for role, data in (("background", png()), ("sound_move", wav())):
            r = await admin.put(
                f"/api/v1/themes/ocean/assets/{role}", files={"file": ("f", data)}
            )
            assert r.status_code == 200, r.text

        app = scenes.make_app(
            tmp_path / "player",
            monkeypatch,
            library=False,
            volume_buttons=None,
            credentials=(device["id"], device["api_key"]),
        )
        media = app._config.media_root

        async def sync() -> None:
            await SyncClient(
                app._config,
                http_client=device_http,
                on_settings=app._on_synced_settings,
                on_theme=app._on_synced_theme,
            ).sync()
            app._tick_controls()

        # Before anything is chosen: the original look.
        await sync()
        assert app._theme.theme_id == "default"
        assert not app.theme_locked

        # The parent picks a built-in theme on the profile's settings page.
        await admin.put(settings_url, json={"theme": "high-contrast"})
        await sync()
        assert app._theme.theme_id == "high-contrast"
        assert app._theme.BG == (0, 0, 0)
        assert app.theme_locked

        # ...then a custom one: its files are synced like media.
        await admin.put(settings_url, json={"theme": "ocean"})
        await sync()
        assert app._theme.theme_id == "ocean"
        assert app._theme.PRIMARY == (0xF2, 0xA9, 0x00)
        assert views._BACKGROUND is not None
        assert views._BACKGROUND.get_size() == (640, 480)
        synced = {p.suffix for p in (media / "themes").rglob("*") if p.is_file()}
        assert synced == {".webp", ".wav"}

        # And the device changes on screen, not only in memory.
        app._render()
        surface = pygame.display.get_surface()
        assert surface is not None
        assert surface.get_at((5, app.layout.view_h - 5))[:3] != app._theme.BG

    # Rebooted with the server gone, the theme and its files are still there.
    pygame.quit()
    rebooted = scenes.make_app(
        tmp_path / "player",
        monkeypatch,
        library=False,
        volume_buttons=None,
        credentials=(device["id"], device["api_key"]),
    )
    assert rebooted._theme.theme_id == "ocean"
    assert views._BACKGROUND is not None
