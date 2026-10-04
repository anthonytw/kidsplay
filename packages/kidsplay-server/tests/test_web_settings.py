"""Tests for the settings pages of the web UI."""

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_server.api.app import create_app


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


async def create_profile(client: AsyncClient, name: str = "Leo") -> str:
    r = await client.post("/api/v1/profiles", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


class TestProfileSettingsPage:
    async def test_renders_defaults(self, client: AsyncClient) -> None:
        pid = await create_profile(client)
        r = await client.get(f"/profiles/{pid}/settings")
        assert r.status_code == 200
        html = r.text
        assert "Leo — settings" in html
        assert 'id="max-volume-value">100<' in html
        assert 'id="enabled-mon"' in html
        assert f"/api/v1/profiles/{pid}/settings" in html

    async def test_renders_saved_values(self, client: AsyncClient) -> None:
        pid = await create_profile(client)
        await client.put(
            f"/api/v1/profiles/{pid}/settings",
            json={
                "max_volume": 45,
                "volume_buttons": True,
                "bedtime_mode": "audiobooks_only",
                "bedtime_schedule": {"fri": {"bedtime": "21:15", "wake": "08:00"}},
            },
        )
        html = (await client.get(f"/profiles/{pid}/settings")).text
        assert 'value="45"' in html
        assert '<option value="on" selected>' in html
        assert '<option value="audiobooks_only" selected>' in html
        assert 'id="bedtime-fri" value="21:15"' in html
        assert 'class="day-enabled" checked' in html

    async def test_volume_buttons_choice_is_shown(self, client: AsyncClient) -> None:
        def chosen(html: str) -> str:
            select = re.search(
                r'<select id="volume-buttons">(.*?)</select>', html, re.S
            )
            assert select is not None
            (value,) = re.findall(r'<option value="(\w*)" selected>', select[1])
            return value

        pid = await create_profile(client)
        url = f"/api/v1/profiles/{pid}/settings"
        assert chosen((await client.get(f"/profiles/{pid}/settings")).text) == ""
        await client.put(url, json={"volume_buttons": False})
        assert chosen((await client.get(f"/profiles/{pid}/settings")).text) == "off"
        await client.put(url, json={"volume_buttons": True})
        assert chosen((await client.get(f"/profiles/{pid}/settings")).text) == "on"

    async def test_button_sounds_choice_is_shown(self, client: AsyncClient) -> None:
        def chosen(html: str) -> str:
            select = re.search(r'<select id="ui-sounds">(.*?)</select>', html, re.S)
            assert select is not None
            (value,) = re.findall(r'<option value="(\w*)" selected>', select[1])
            return value

        pid = await create_profile(client)
        url = f"/api/v1/profiles/{pid}/settings"
        page = (await client.get(f"/profiles/{pid}/settings")).text
        assert "Button sounds" in page
        assert chosen(page) == "on"  # the default
        await client.put(url, json={"ui_sounds": False})
        assert chosen((await client.get(f"/profiles/{pid}/settings")).text) == "off"
        await client.put(url, json={"ui_sounds": True})
        assert chosen((await client.get(f"/profiles/{pid}/settings")).text) == "on"

    async def test_button_sounds_label_is_spanish(self, client: AsyncClient) -> None:
        pid = await create_profile(client)
        html = (await client.get(f"/profiles/{pid}/settings?lang=es")).text
        assert "Sonidos de botones" in html
        assert "Button sounds" not in html

    async def test_unknown_profile_404(self, client: AsyncClient) -> None:
        r = await client.get("/profiles/00000000-0000-0000-0000-000000000000/settings")
        assert r.status_code == 404

    async def test_profiles_page_links_settings(self, client: AsyncClient) -> None:
        pid = await create_profile(client)
        html = (await client.get("/profiles")).text
        assert f'href="/profiles/{pid}/settings"' in html

    async def test_requires_login(
        self, client: AsyncClient, anon_client: AsyncClient
    ) -> None:
        pid = await create_profile(client)
        r = await anon_client.get(f"/profiles/{pid}/settings")
        assert r.status_code in (302, 303, 307)
        assert "/login" in r.headers["location"]


class TestServerSettingsPage:
    async def test_renders_defaults(self, client: AsyncClient) -> None:
        r = await client.get("/settings")
        assert r.status_code == 200
        assert 'id="setting-sync_interval_seconds"' in r.text
        assert 'value="900"' in r.text
        assert 'value="85"' in r.text
        assert "set by KIDSPLAY_" not in r.text

    async def test_saved_value_offers_reset(self, client: AsyncClient) -> None:
        await client.put("/api/v1/server-settings", json={"webp_quality": 60})
        html = (await client.get("/settings")).text
        assert 'value="60"' in html
        assert "resetSetting('webp_quality')" in html

    async def test_env_locked_shown_disabled(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_SYNC_INTERVAL_SECONDS", "120")
        html = (await client.get("/settings")).text
        assert "set by KIDSPLAY_SYNC_INTERVAL_SECONDS" in html
        assert 'value="120"' in html
        locked = html.split('id="setting-sync_interval_seconds"')[1].split(">")[0]
        assert "disabled" in locked
        free = html.split('id="setting-webp_quality"')[1].split(">")[0]
        assert "disabled" not in free

    async def test_loudness_targets_are_listed(self, client: AsyncClient) -> None:
        html = (await client.get("/settings")).text
        assert 'id="setting-loudness_target_lufs"' in html
        assert 'id="setting-loudness_target_lufs_music"' in html
        assert 'id="setting-loudness_target_lufs_audiobook"' in html
        assert 'value="-16"' in html
        # Per-type targets are empty until set: "same as overall".
        music = html.split('id="setting-loudness_target_lufs_music"')[1].split(">")[0]
        assert 'value=""' in music
        assert 'id="normalize-offer"' in html

    async def test_loudness_env_locked(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIOBOOK", "-13.5")
        html = (await client.get("/settings")).text
        assert "set by KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIOBOOK" in html
        assert 'value="-13.5"' in html
        locked = html.split('id="setting-loudness_target_lufs_audiobook"')[1]
        assert "disabled" in locked.split(">")[0]

    async def test_nav_link(self, client: AsyncClient) -> None:
        html = (await client.get("/")).text
        assert 'href="/settings"' in html

    async def test_requires_login(self, anon_client: AsyncClient) -> None:
        r = await anon_client.get("/settings")
        assert r.status_code in (302, 303, 307)
