"""Tests for profile settings and server settings endpoints.

Covers ``GET/PUT /profiles/{id}/settings``, ``GET/PUT /server-settings`` and
how both reach devices through the sync manifest.
"""

import io
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image, PngImagePlugin

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


async def create_profile(client: AsyncClient, name: str = "Leo") -> dict:
    r = await client.post("/api/v1/profiles", json={"name": name})
    assert r.status_code == 201
    return r.json()


async def create_device(client: AsyncClient, profile_id: str) -> dict:
    r = await client.post(
        "/api/v1/devices", json={"name": "Leo's GameBoy", "profile_id": profile_id}
    )
    assert r.status_code == 201
    return r.json()


async def get_manifest(
    client: AsyncClient, device: dict, etag: str | None = None
) -> tuple[int, dict | None, str | None]:
    headers = {"Authorization": f"Bearer {device['api_key']}"}
    if etag:
        headers["If-None-Match"] = etag
    r = await client.get(f"/api/v1/devices/{device['id']}/manifest", headers=headers)
    body = r.json() if r.status_code == 200 else None
    return r.status_code, body, r.headers.get("etag")


SLEEPY = {
    "max_volume": 60,
    "volume_buttons": True,
    "bedtime_mode": "sleep_screen",
    "bedtime_schedule": {"mon": {"bedtime": "20:00", "wake": "07:00"}},
}


# ---------------------------------------------------------------------------
# Profile settings
# ---------------------------------------------------------------------------


class TestProfileSettingsApi:
    async def test_defaults_when_never_saved(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        r = await client.get(f"/api/v1/profiles/{profile['id']}/settings")
        assert r.status_code == 200
        body = r.json()
        assert body["max_volume"] == 100
        assert "volume_buttons" not in body  # undecided: the device decides
        assert body["bedtime_mode"] == "off"
        assert body["bedtime_schedule"] == {}

    async def test_volume_buttons_choice_round_trips(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        for value in (False, True):
            await client.put(url, json={"volume_buttons": value})
            assert (await client.get(url)).json()["volume_buttons"] is value
        await client.put(url, json={"volume_buttons": None})
        assert "volume_buttons" not in (await client.get(url)).json()

    async def test_ui_sounds_default_on_and_round_trips(
        self, client: AsyncClient
    ) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        assert (await client.get(url)).json()["ui_sounds"] is True
        for value in (False, True):
            r = await client.put(url, json={"ui_sounds": value})
            assert r.json()["ui_sounds"] is value
            assert (await client.get(url)).json()["ui_sounds"] is value
        # A PUT that omits it takes the default, like the other fields.
        await client.put(url, json={"ui_sounds": False})
        await client.put(url, json={"max_volume": 50})
        assert (await client.get(url)).json()["ui_sounds"] is True

    async def test_non_boolean_ui_sounds_is_rejected(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        r = await client.put(url, json={"ui_sounds": "banana"})
        assert r.status_code == 422

    async def test_put_then_get(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        r = await client.put(url, json=SLEEPY)
        assert r.status_code == 200
        assert r.json()["max_volume"] == 60
        got = (await client.get(url)).json()
        assert got["max_volume"] == 60
        assert got["volume_buttons"] is True
        assert got["bedtime_mode"] == "sleep_screen"
        assert got["bedtime_schedule"]["mon"] == {
            "bedtime": "20:00:00",
            "wake": "07:00:00",
        }

    async def test_settings_are_per_profile(self, client: AsyncClient) -> None:
        leo = await create_profile(client, "Leo")
        mia = await create_profile(client, "Mia")
        await client.put(f"/api/v1/profiles/{leo['id']}/settings", json=SLEEPY)
        r = await client.get(f"/api/v1/profiles/{mia['id']}/settings")
        assert r.json()["max_volume"] == 100

    async def test_put_ignores_unknown_fields(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        r = await client.put(url, json={"max_volume": 50, "sparkles": "fr"})
        assert r.status_code == 200
        assert "sparkles" not in r.json()

    async def test_put_without_language_keeps_the_stored_one(
        self, client: AsyncClient
    ) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        assert (await client.put(url, json={"language": "en"})).status_code == 200
        r = await client.put(url, json={"max_volume": 40})
        assert r.status_code == 200
        assert r.json()["max_volume"] == 40
        assert r.json()["language"] == "en"
        assert (await client.get(url)).json()["language"] == "en"

    async def test_put_with_null_language_clears_it(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        await client.put(url, json={"language": "en"})
        r = await client.put(url, json={"language": None})
        assert r.status_code == 200
        assert r.json()["language"] is None
        assert (await client.get(url)).json()["language"] is None

    async def test_put_with_language_sets_it(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        await client.put(url, json={"language": "en"})
        r = await client.put(url, json={"language": "es", "max_volume": 40})
        assert r.json()["language"] == "es"
        assert (await client.get(url)).json()["language"] == "es"

    @pytest.mark.parametrize(
        "body",
        [
            {"max_volume": 101},
            {"max_volume": -5},
            {"bedtime_mode": "party"},
            {"bedtime_schedule": {"mon": {"bedtime": "20:00", "wake": "20:00"}}},
            {"bedtime_schedule": {"xyz": {"bedtime": "20:00", "wake": "07:00"}}},
            {"bedtime_schedule": {"mon": {"bedtime": "20:00+05:00", "wake": "07:00"}}},
            {"bedtime_schedule": {"mon": {"bedtime": "20:00", "wake": "07:00Z"}}},
        ],
    )
    async def test_invalid_body_rejected(self, client: AsyncClient, body: dict) -> None:
        profile = await create_profile(client)
        r = await client.put(f"/api/v1/profiles/{profile['id']}/settings", json=body)
        assert r.status_code == 422

    async def test_unknown_profile_404(self, client: AsyncClient) -> None:
        missing = "00000000-0000-0000-0000-000000000000"
        r = await client.get(f"/api/v1/profiles/{missing}/settings")
        assert r.status_code == 404
        r = await client.put(f"/api/v1/profiles/{missing}/settings", json={})
        assert r.status_code == 404

    async def test_deleting_profile_deletes_its_settings(
        self, client: AsyncClient
    ) -> None:
        profile = await create_profile(client)
        await client.put(f"/api/v1/profiles/{profile['id']}/settings", json=SLEEPY)
        r = await client.delete(f"/api/v1/profiles/{profile['id']}")
        assert r.status_code == 204

    async def test_requires_admin(
        self, client: AsyncClient, anon_client: AsyncClient
    ) -> None:
        profile = await create_profile(client)
        url = f"/api/v1/profiles/{profile['id']}/settings"
        assert (await anon_client.get(url)).status_code == 401
        assert (await anon_client.put(url, json={})).status_code == 401


class TestProfileSettingsInManifest:
    async def test_manifest_carries_defaults(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        status, body, _ = await get_manifest(client, device)
        assert status == 200
        assert body is not None
        assert body["profile_settings"]["max_volume"] == 100
        # Nobody chose an interval: the device keeps its own.
        assert body["sync_interval_seconds"] is None

    async def test_manifest_carries_saved_settings(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        await client.put(f"/api/v1/profiles/{profile['id']}/settings", json=SLEEPY)
        _, body, _ = await get_manifest(client, device)
        assert body is not None
        assert body["profile_settings"]["max_volume"] == 60
        assert body["profile_settings"]["bedtime_mode"] == "sleep_screen"

    async def test_manifest_carries_ui_sounds(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        _, body, _ = await get_manifest(client, device)
        assert body is not None
        assert body["profile_settings"]["ui_sounds"] is True
        await client.put(
            f"/api/v1/profiles/{profile['id']}/settings", json={"ui_sounds": False}
        )
        _, body, _ = await get_manifest(client, device)
        assert body is not None
        assert body["profile_settings"]["ui_sounds"] is False

    async def test_settings_change_invalidates_etag(self, client: AsyncClient) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        _, _, etag = await get_manifest(client, device)
        status, _, _ = await get_manifest(client, device, etag)
        assert status == 304

        url = f"/api/v1/profiles/{profile['id']}/settings"
        await client.put(url, json={"max_volume": 40})
        status, body, new_etag = await get_manifest(client, device, etag)
        assert status == 200
        assert body is not None
        assert body["profile_settings"]["max_volume"] == 40
        assert new_etag != etag

    async def test_interval_reset_goes_back_to_the_device_own(
        self, client: AsyncClient
    ) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        await client.put("/api/v1/server-settings", json={"sync_interval_seconds": 300})
        _, body, etag = await get_manifest(client, device)
        assert body is not None
        assert body["sync_interval_seconds"] == 300
        await client.put(
            "/api/v1/server-settings", json={"reset": ["sync_interval_seconds"]}
        )
        status, body, _ = await get_manifest(client, device, etag)
        assert status == 200
        assert body is not None
        assert body["sync_interval_seconds"] is None

    async def test_interval_pinned_by_the_environment_is_sent(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        monkeypatch.setenv("KIDSPLAY_SYNC_INTERVAL_SECONDS", "120")
        _, body, _ = await get_manifest(client, device)
        assert body is not None
        assert body["sync_interval_seconds"] == 120

    async def test_saving_another_setting_does_not_send_the_interval(
        self, client: AsyncClient
    ) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        await client.put("/api/v1/server-settings", json={"webp_quality": 70})
        _, body, _ = await get_manifest(client, device)
        assert body is not None
        assert body["sync_interval_seconds"] is None

    async def test_sync_interval_change_invalidates_etag(
        self, client: AsyncClient
    ) -> None:
        profile = await create_profile(client)
        device = await create_device(client, profile["id"])
        _, _, etag = await get_manifest(client, device)
        await client.put("/api/v1/server-settings", json={"sync_interval_seconds": 300})
        status, body, _ = await get_manifest(client, device, etag)
        assert status == 200
        assert body is not None
        assert body["sync_interval_seconds"] == 300


# ---------------------------------------------------------------------------
# Server settings
# ---------------------------------------------------------------------------


class TestServerSettingsApi:
    async def test_defaults(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/server-settings")
        assert r.status_code == 200
        body = r.json()
        assert body["values"] == {
            "sync_interval_seconds": 900,
            "webp_quality": 85,
            "pairing_enabled": True,
            "loudness_target_lufs": -16.0,
            "loudness_target_lufs_music": None,
            "loudness_target_lufs_audiobook": None,
        }
        assert body["env_locked"] == []
        assert body["env_vars"]["webp_quality"] == "KIDSPLAY_WEBP_QUALITY"

    async def test_put_saves_values(self, client: AsyncClient) -> None:
        r = await client.put("/api/v1/server-settings", json={"webp_quality": 70})
        assert r.status_code == 200
        assert r.json()["values"]["webp_quality"] == 70
        assert r.json()["values"]["sync_interval_seconds"] == 900
        got = (await client.get("/api/v1/server-settings")).json()
        assert got["values"]["webp_quality"] == 70

    async def test_reset_restores_default(self, client: AsyncClient) -> None:
        await client.put("/api/v1/server-settings", json={"webp_quality": 70})
        r = await client.put(
            "/api/v1/server-settings", json={"reset": ["webp_quality"]}
        )
        assert r.json()["values"]["webp_quality"] == 85

    @pytest.mark.parametrize(
        "body",
        [
            {"webp_quality": 0},
            {"webp_quality": 101},
            {"sync_interval_seconds": 5},
            {"reset": ["no_such_setting"]},
            {"webp_qualty": 50},  # a typo is an error, not a silent no-op
            {"webp_quality": 70, "bogus": 1},
            {"loudness_target_lufs": -4},
            {"loudness_target_lufs_music": -71},
        ],
    )
    async def test_invalid_rejected(self, client: AsyncClient, body: dict) -> None:
        r = await client.put("/api/v1/server-settings", json=body)
        assert r.status_code == 422

    async def test_unknown_key_saves_nothing(self, client: AsyncClient) -> None:
        r = await client.put(
            "/api/v1/server-settings", json={"webp_quality": 70, "bogus": 1}
        )
        assert r.status_code == 422
        got = (await client.get("/api/v1/server-settings")).json()
        assert got["values"]["webp_quality"] == 85

    async def test_loudness_targets_save_and_reset(self, client: AsyncClient) -> None:
        r = await client.put(
            "/api/v1/server-settings",
            json={"loudness_target_lufs": -18, "loudness_target_lufs_audiobook": -14},
        )
        assert r.status_code == 200
        values = r.json()["values"]
        assert values["loudness_target_lufs"] == -18
        assert values["loudness_target_lufs_audiobook"] == -14
        assert values["loudness_target_lufs_music"] is None
        r = await client.put(
            "/api/v1/server-settings",
            json={"reset": ["loudness_target_lufs_audiobook"]},
        )
        assert r.json()["values"]["loudness_target_lufs_audiobook"] is None
        assert r.json()["values"]["loudness_target_lufs"] == -18

    async def test_env_wins_and_locks(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await client.put("/api/v1/server-settings", json={"webp_quality": 70})
        monkeypatch.setenv("KIDSPLAY_WEBP_QUALITY", "95")
        body = (await client.get("/api/v1/server-settings")).json()
        assert body["values"]["webp_quality"] == 95
        assert body["env_locked"] == ["webp_quality"]

        r = await client.put("/api/v1/server-settings", json={"webp_quality": 60})
        assert r.status_code == 409
        assert r.json()["error_code"] == "ENV_LOCKED"
        r = await client.put(
            "/api/v1/server-settings", json={"reset": ["webp_quality"]}
        )
        assert r.status_code == 409

    async def test_env_lock_does_not_block_other_keys(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_WEBP_QUALITY", "95")
        r = await client.put(
            "/api/v1/server-settings", json={"sync_interval_seconds": 600}
        )
        assert r.status_code == 200
        assert r.json()["values"]["sync_interval_seconds"] == 600

    async def test_requires_admin(self, anon_client: AsyncClient) -> None:
        assert (await anon_client.get("/api/v1/server-settings")).status_code == 401
        r = await anon_client.put("/api/v1/server-settings", json={})
        assert r.status_code == 401


class TestWebpQualitySetting:
    async def test_photo_ingest_uses_quality_setting(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """A lower quality setting yields a smaller processed photo."""
        pixels = Image.effect_noise((600, 400), 64).convert("RGB")

        async def ingest(name: str) -> int:
            # Same pixels, different PNG text chunk: a distinct source hash.
            info = PngImagePlugin.PngInfo()
            info.add_text("name", name)
            buf = io.BytesIO()
            pixels.save(buf, format="PNG", pnginfo=info)
            src = tmp_path / f"{name}.png"
            src.write_bytes(buf.getvalue())
            r = await client.post(
                "/api/v1/media/ingest",
                json={
                    "source_path": str(src),
                    "media_type": "photo",
                    "playlist_title": name,
                },
            )
            assert r.status_code == 200
            media_id = r.json()["results"][0]["media_id"]
            with sqlite3.connect(tmp_path / "test.db") as conn:
                row = conn.execute(
                    "SELECT size_bytes FROM processed_files"
                    " WHERE media_id = ? AND file_type = 'photo_resized'",
                    (media_id,),
                ).fetchone()
            return int(row[0])

        high = await ingest("high")
        await client.put("/api/v1/server-settings", json={"webp_quality": 10})
        low = await ingest("low")
        assert low < high
