"""Tests for themes: the API, asset validation, the profile choice and the manifest."""

import io
import threading
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response
from PIL import Image, ImageFile

from kidsplay_models import (
    BUILTIN_THEMES,
    SyncManifest,
    ThemeAsset,
    ThemeAssetRole,
)
from kidsplay_server import theme_assets
from kidsplay_server.api import themes as api_themes
from kidsplay_server.api.app import create_app
from kidsplay_server.storage import MediaStore

FONT = (
    Path(__file__).parents[2]
    / "kidsplay-device/src/kidsplay_device/assets/fa-solid-900.ttf"
)
COLORS = {
    "bg": "#101010",
    "surface": "#202020",
    "surface_sel": "#303030",
    "primary": "#ff8800",
    "text": "#eeeeee",
    "text_dim": "#999999",
    "text_bright": "#ffffff",
    "accent": "#00aaff",
    "progress_bg": "#444444",
}


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


def png(size: tuple[int, int] = (2000, 1500)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, (10, 120, 200, 255)).save(buf, "PNG")
    return buf.getvalue()


def wav(seconds: float = 0.2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buf.getvalue()


OGG_FIXTURE = Path(__file__).parent / "fixtures" / "beep.ogg"


def ogg() -> bytes:
    """A 0.2 s Ogg Vorbis beep.

    A checked-in file rather than one encoded by ffmpeg here: not every ffmpeg
    build has the libvorbis encoder (macOS Homebrew's does not). It was made
    with ``ffmpeg -f lavfi -i sine=d=0.2:r=8000 -ac 1 -c:a libvorbis -q:a 0
    -map_metadata -1 -fflags +bitexact -flags:a +bitexact beep.ogg``.
    """
    return OGG_FIXTURE.read_bytes()


async def make_theme(client: AsyncClient, theme_id: str = "sunset") -> dict:
    r = await client.put(
        f"/api/v1/themes/{theme_id}", json={"name": "Sunset", "colors": COLORS}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def upload(
    client: AsyncClient, theme_id: str, role: str, data: bytes, name: str = "f.bin"
) -> Response:
    return await client.put(
        f"/api/v1/themes/{theme_id}/assets/{role}", files={"file": (name, data)}
    )


async def device_for(client: AsyncClient, settings: dict) -> tuple[dict, dict]:
    profile = (await client.post("/api/v1/profiles", json={"name": "Leo"})).json()
    r = await client.put(f"/api/v1/profiles/{profile['id']}/settings", json=settings)
    assert r.status_code == 200, r.text
    device = (
        await client.post(
            "/api/v1/devices", json={"name": "GB", "profile_id": profile["id"]}
        )
    ).json()
    return profile, device


def bearer(device: dict) -> dict:
    return {"Authorization": f"Bearer {device['api_key']}"}


class TestThemeApi:
    async def test_lists_builtins_then_custom(self, client: AsyncClient) -> None:
        await make_theme(client)
        themes = (await client.get("/api/v1/themes")).json()
        ids = [t["id"] for t in themes]
        assert ids[: len(BUILTIN_THEMES)] == [t.id for t in BUILTIN_THEMES]
        assert ids[-1] == "sunset"
        assert themes[0]["builtin"] and not themes[-1]["builtin"]

    async def test_create_update_keeps_assets(self, client: AsyncClient) -> None:
        await make_theme(client)
        assert (await upload(client, "sunset", "sound_move", wav())).status_code == 200
        r = await client.put(
            "/api/v1/themes/sunset",
            json={"name": "Sunset 2", "colors": {**COLORS, "bg": "#000000"}},
        )
        body = r.json()
        assert body["name"] == "Sunset 2"
        assert body["colors"]["bg"] == "#000000"
        assert [a["role"] for a in body["assets"]] == ["sound_move"]

    async def test_builtin_cannot_be_changed_or_deleted(
        self, client: AsyncClient
    ) -> None:
        r = await client.put(
            "/api/v1/themes/night", json={"name": "x", "colors": COLORS}
        )
        assert r.status_code == 409
        assert (await client.delete("/api/v1/themes/night")).status_code == 409
        assert (await upload(client, "night", "font", b"x")).status_code == 409

    @pytest.mark.parametrize("bad", ["Has Space", "UPPER", "-x", "a" * 41])
    async def test_invalid_id_rejected(self, client: AsyncClient, bad: str) -> None:
        r = await client.put(
            f"/api/v1/themes/{bad}", json={"name": "x", "colors": COLORS}
        )
        assert r.status_code == 422

    async def test_invalid_color_rejected(self, client: AsyncClient) -> None:
        r = await client.put(
            "/api/v1/themes/bad",
            json={"name": "x", "colors": {**COLORS, "bg": "black"}},
        )
        assert r.status_code == 422

    async def test_get_and_delete(self, client: AsyncClient) -> None:
        await make_theme(client)
        assert (await client.get("/api/v1/themes/sunset")).json()["name"] == "Sunset"
        assert (await client.get("/api/v1/themes/night")).json()["builtin"]
        assert (await client.delete("/api/v1/themes/sunset")).status_code == 204
        assert (await client.get("/api/v1/themes/sunset")).status_code == 404
        assert (await client.delete("/api/v1/themes/sunset")).status_code == 404

    async def test_requires_admin(
        self, client: AsyncClient, anon_client: AsyncClient
    ) -> None:
        await make_theme(client)
        assert (await anon_client.get("/api/v1/themes")).status_code == 401
        r = await anon_client.put(
            "/api/v1/themes/x", json={"name": "x", "colors": COLORS}
        )
        assert r.status_code == 401
        assert (await upload(anon_client, "sunset", "font", b"x")).status_code == 401


class TestAssets:
    async def test_background_is_reencoded_small_webp(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        await make_theme(client)
        r = await upload(client, "sunset", "background", png())
        assert r.status_code == 200, r.text
        asset = r.json()["assets"][0]
        assert asset["relative_path"].startswith("themes/")
        assert asset["relative_path"].endswith(".webp")
        stored = tmp_path / "media" / asset["relative_path"]
        with Image.open(stored) as img:
            assert img.format == "WEBP" and img.mode == "RGB"
            assert img.size == (960, 720)  # 2000x1500 fitted into 1280x720
        assert asset["size_bytes"] == stored.stat().st_size

    async def test_small_background_is_not_upscaled(self, client: AsyncClient) -> None:
        await make_theme(client)
        r = await upload(client, "sunset", "home_background", png((100, 50)))
        assert r.status_code == 200

    async def test_font_sound_roles(self, client: AsyncClient) -> None:
        await make_theme(client)
        assert (
            await upload(client, "sunset", "font", FONT.read_bytes())
        ).status_code == 200
        assert (await upload(client, "sunset", "sound_open", wav())).status_code == 200
        assert (await upload(client, "sunset", "sound_back", ogg())).status_code == 200
        roles = {
            a["role"]
            for a in (await client.get("/api/v1/themes/sunset")).json()["assets"]
        }
        assert roles == {"font", "sound_open", "sound_back"}

    @pytest.mark.parametrize(
        ("role", "data"),
        [
            ("background", b"not an image"),
            ("font", b"not a font at all"),
            ("font", b"\x00\x01\x00\x00" + b"\x00" * 40),  # right magic, no font
            ("sound_move", b"not a sound"),
            ("sound_move", b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8),  # truncated
            ("sound_move", b""),
        ],
    )
    async def test_unusable_files_are_rejected(
        self, client: AsyncClient, role: str, data: bytes
    ) -> None:
        await make_theme(client)
        r = await upload(client, "sunset", role, data)
        assert r.status_code == 422
        assert r.json()["error_code"] == "INVALID_THEME_ASSET"
        assert (await client.get("/api/v1/themes/sunset")).json()["assets"] == []

    async def test_decompression_bomb_is_rejected_before_decoding(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tiny file that would decode to hundreds of MB is refused by size."""
        await make_theme(client)
        # 1-bit, 72 megapixels: about 9 MB raw, a few KB compressed.
        buf = io.BytesIO()
        Image.new("1", (9000, 8000)).save(buf, "PNG", optimize=True)
        bomb = buf.getvalue()
        assert len(bomb) < 100_000

        def no_decode(self: Image.Image) -> None:
            raise AssertionError("pixels were decoded")

        monkeypatch.setattr(ImageFile.ImageFile, "load", no_decode)
        r = await upload(client, "sunset", "background", bomb)
        assert r.status_code == 422
        assert r.json()["error_code"] == "INVALID_THEME_ASSET"
        assert "too large" in r.json()["detail"]
        assert (await client.get("/api/v1/themes/sunset")).json()["assets"] == []

    async def test_pillow_bomb_error_is_a_clean_rejection(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pillow's own limit (if lowered/hit mid-decode) is a 422, not a 500."""
        await make_theme(client)
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
        r = await upload(client, "sunset", "background", png((100, 100)))
        assert r.status_code == 422
        assert "too large" in r.json()["detail"]

    @pytest.mark.parametrize("size", [(4097, 10), (10, 4097)])
    async def test_image_over_the_side_limit_is_rejected(
        self, client: AsyncClient, size: tuple[int, int]
    ) -> None:
        """A thin strip is under the pixel cap but over the per-side cap."""
        await make_theme(client)
        r = await upload(client, "sunset", "background", png(size))
        assert r.status_code == 422
        assert r.json()["error_code"] == "INVALID_THEME_ASSET"
        assert "too large" in r.json()["detail"]

    async def test_image_at_the_side_limit_is_accepted(
        self, client: AsyncClient
    ) -> None:
        await make_theme(client)
        r = await upload(client, "sunset", "background", png((4096, 10)))
        assert r.status_code == 200

    async def test_image_at_the_pixel_limit_is_accepted(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await make_theme(client)
        monkeypatch.setattr(theme_assets, "MAX_IMAGE_PIXELS", 200 * 100)
        assert (
            await upload(client, "sunset", "background", png((200, 100)))
        ).status_code == 200
        assert (
            await upload(client, "sunset", "background", png((201, 100)))
        ).status_code == 422

    async def test_validation_runs_off_the_event_loop(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: Pillow/mutagen work used to run on the event loop."""
        await make_theme(client)
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = api_themes.store_theme_asset

        def spy(
            role: ThemeAssetRole, data: bytes, store: MediaStore, quality: int
        ) -> tuple[ThemeAsset, str]:
            seen.append(threading.get_ident())
            return real(role, data, store, quality)

        monkeypatch.setattr(api_themes, "store_theme_asset", spy)
        r = await upload(client, "sunset", "background", png())
        assert r.status_code == 200
        assert len(seen) == 1 and seen[0] != loop_thread

    async def test_long_sound_rejected(self, client: AsyncClient) -> None:
        await make_theme(client)
        r = await upload(client, "sunset", "sound_move", wav(11.0))
        assert r.status_code == 422
        assert "longer" in r.json()["detail"]

    async def test_oversized_upload_rejected(self, client: AsyncClient) -> None:
        await make_theme(client)
        r = await upload(client, "sunset", "sound_move", b"\0" * (2 * 1024 * 1024 + 1))
        assert r.status_code == 422
        assert "larger" in r.json()["detail"]

    async def test_unknown_role_and_theme(self, client: AsyncClient) -> None:
        await make_theme(client)
        assert (await upload(client, "sunset", "wallpaper", b"x")).status_code == 422
        assert (await upload(client, "nope", "font", b"x")).status_code == 404

    async def test_replace_and_delete_asset_keep_store_files(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        await make_theme(client)
        first = (await upload(client, "sunset", "sound_move", wav(0.2))).json()
        first_path = tmp_path / "media" / first["assets"][0]["relative_path"]
        second = (await upload(client, "sunset", "sound_move", wav(0.3))).json()
        assert len(second["assets"]) == 1
        assert second["assets"][0]["content_hash"] != first["assets"][0]["content_hash"]
        assert first_path.exists()  # never deleted in place
        r = await client.delete("/api/v1/themes/sunset/assets/sound_move")
        assert r.json()["assets"] == []
        assert first_path.exists()
        r = await client.delete("/api/v1/themes/sunset/assets/sound_move")
        assert r.status_code == 404


class TestProfileChoice:
    async def test_theme_round_trips_and_is_validated(
        self, client: AsyncClient
    ) -> None:
        profile, _ = await device_for(client, {"theme": "night"})
        url = f"/api/v1/profiles/{profile['id']}/settings"
        assert (await client.get(url)).json()["theme"] == "night"
        r = await client.put(url, json={"theme": "no-such-theme"})
        assert r.status_code == 422
        assert r.json()["error_code"] == "UNKNOWN_THEME"
        assert (await client.get(url)).json()["theme"] == "night"

    async def test_custom_theme_can_be_chosen(self, client: AsyncClient) -> None:
        await make_theme(client)
        profile, _ = await device_for(client, {"theme": "sunset"})
        url = f"/api/v1/profiles/{profile['id']}/settings"
        assert (await client.get(url)).json()["theme"] == "sunset"

    async def test_omitting_theme_keeps_it_and_null_clears_it(
        self, client: AsyncClient
    ) -> None:
        profile, _ = await device_for(client, {"theme": "night"})
        url = f"/api/v1/profiles/{profile['id']}/settings"
        # A client that predates themes (the old CLI) sends no theme key.
        await client.put(url, json={"max_volume": 40})
        stored = (await client.get(url)).json()
        assert stored["theme"] == "night" and stored["max_volume"] == 40
        await client.put(url, json={"theme": None})
        assert (await client.get(url)).json()["theme"] is None

    async def test_web_page_lists_themes_and_marks_the_choice(
        self, client: AsyncClient
    ) -> None:
        await make_theme(client)
        profile, _ = await device_for(client, {"theme": "sunset"})
        html = (await client.get(f"/profiles/{profile['id']}/settings")).text
        assert 'id="device-theme"' in html
        assert '<option value="night" >Night</option>' in html
        assert '<option value="sunset" selected>Sunset</option>' in html
        assert "theme: document.getElementById('device-theme')" in html

    async def test_web_page_is_translated_and_custom_names_are_not(
        self, client: AsyncClient
    ) -> None:
        await make_theme(client)
        profile, _ = await device_for(client, {})
        html = (await client.get(f"/profiles/{profile['id']}/settings?lang=es")).text
        assert ">Alto contraste</option>" in html
        assert ">Noche</option>" in html
        assert ">Se elige en el dispositivo</option>" in html
        assert ">Sunset</option>" in html  # a custom theme's name is the owner's


class TestManifest:
    async def test_no_theme_leaves_the_manifest_as_before(
        self, client: AsyncClient
    ) -> None:
        _, device = await device_for(client, {})
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=bearer(device)
        )
        body = r.json()
        assert body["theme"] is None
        assert body["profile_settings"]["theme"] is None

    async def test_builtin_theme_is_in_the_manifest_without_files(
        self, client: AsyncClient
    ) -> None:
        _, device = await device_for(client, {"theme": "high-contrast"})
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=bearer(device)
        )
        manifest = SyncManifest.model_validate(r.json())
        assert manifest.theme is not None
        assert manifest.theme.id == "high-contrast"
        assert manifest.files == []

    async def test_custom_theme_assets_are_synced_like_media(
        self, client: AsyncClient
    ) -> None:
        await make_theme(client)
        await upload(client, "sunset", "background", png())
        await upload(client, "sunset", "sound_select", wav())
        _, device = await device_for(client, {"theme": "sunset"})
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=bearer(device)
        )
        manifest = SyncManifest.model_validate(r.json())
        assert manifest.theme is not None
        assert {f.content_hash for f in manifest.files} == {
            a.content_hash for a in manifest.theme.assets
        }
        assert all(f.file_type == "theme" for f in manifest.files)
        assert manifest.total_size_bytes == sum(f.size_bytes for f in manifest.files)
        for entry in manifest.files:
            dl = await client.get(
                f"/api/v1/sync/file/{entry.content_hash}", headers=bearer(device)
            )
            assert dl.status_code == 200
            assert len(dl.content) == entry.size_bytes

    async def test_theme_change_changes_the_etag(self, client: AsyncClient) -> None:
        profile, device = await device_for(client, {"theme": "night"})
        url = f"/api/v1/devices/{device['id']}/manifest"
        etag = (await client.get(url, headers=bearer(device))).headers["etag"]
        same = await client.get(url, headers={**bearer(device), "If-None-Match": etag})
        assert same.status_code == 304
        await client.put(
            f"/api/v1/profiles/{profile['id']}/settings", json={"theme": "green"}
        )
        changed = await client.get(
            url, headers={**bearer(device), "If-None-Match": etag}
        )
        assert changed.status_code == 200
        assert changed.json()["theme"]["id"] == "green"

    async def test_asset_change_changes_the_etag(self, client: AsyncClient) -> None:
        await make_theme(client)
        _, device = await device_for(client, {"theme": "sunset"})
        url = f"/api/v1/devices/{device['id']}/manifest"
        etag = (await client.get(url, headers=bearer(device))).headers["etag"]
        await upload(client, "sunset", "sound_move", wav())
        r = await client.get(url, headers={**bearer(device), "If-None-Match": etag})
        assert r.status_code == 200

    async def test_deleted_theme_sends_none(self, client: AsyncClient) -> None:
        await make_theme(client)
        await upload(client, "sunset", "sound_move", wav())
        profile, device = await device_for(client, {"theme": "sunset"})
        await client.delete("/api/v1/themes/sunset")
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=bearer(device)
        )
        body = r.json()
        assert body["theme"] is None and body["files"] == []
        assert body["profile_settings"]["theme"] == "sunset"

    async def test_theme_assets_need_a_device_token_to_download(
        self, client: AsyncClient, anon_client: AsyncClient
    ) -> None:
        await make_theme(client)
        asset = (await upload(client, "sunset", "sound_move", wav())).json()["assets"][
            0
        ]
        r = await anon_client.get(f"/api/v1/sync/file/{asset['content_hash']}")
        assert r.status_code == 401

    async def test_web_media_route_does_not_serve_theme_assets(
        self, client: AsyncClient
    ) -> None:
        await make_theme(client)
        asset = (await upload(client, "sunset", "sound_move", wav())).json()["assets"][
            0
        ]
        from kidsplay_server.web import routes

        paths = [
            r.path
            for r in routes.router.routes
            if isinstance(r, APIRoute) and "hash" in r.path
        ]
        assert paths, "the web media-file route exists"
        r = await client.get(paths[0].replace("{content_hash}", asset["content_hash"]))
        assert r.status_code == 404
