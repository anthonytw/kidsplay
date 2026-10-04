"""Tests for the theme part of device sync.

The theme is parsed apart from the media manifest, so whatever it holds, media
still syncs; its asset files ride in the manifest's ``files``, so they are
downloaded, verified, kept (not pruned) and work offline.
"""

import hashlib
import json
import uuid
from pathlib import Path

import httpx
import pytest

from kidsplay_device.config import DeviceConfig
from kidsplay_device.database import (
    get_all_media_ids,
    get_sync_state,
    get_theme_definition,
    init_db,
    set_theme_definition,
)
from kidsplay_device.sync import SyncClient
from kidsplay_models import (
    BUILTIN_THEMES_BY_ID,
    ThemeAsset,
    ThemeAssetRole,
    ThemeDefinition,
)

SOUND = b"OggS-not-decoded-here"


@pytest.fixture
def cfg(tmp_path: Path) -> DeviceConfig:
    return DeviceConfig(
        server_url="http://test",
        device_id=str(uuid.uuid4()),
        api_key="k",
        media_root=tmp_path / "media",
        db_path=tmp_path / "device.db",
    )


def theme_with_sound() -> ThemeDefinition:
    digest = hashlib.sha256(SOUND).hexdigest()
    return BUILTIN_THEMES_BY_ID["night"].model_copy(
        update={
            "id": "mine",
            "builtin": False,
            "assets": [
                ThemeAsset(
                    role=ThemeAssetRole.SOUND_MOVE,
                    content_hash=digest,
                    relative_path=f"themes/{digest[:2]}/{digest}.ogg",
                    size_bytes=len(SOUND),
                )
            ],
        }
    )


def manifest(cfg: DeviceConfig, theme: object, files: list | None = None) -> dict:
    body: dict = {
        "device_id": cfg.device_id,
        "profile_id": str(uuid.uuid4()),
        "manifest_hash": "hash-1",
        "files": files or [],
        "media": [],
    }
    if theme is not ...:
        body["theme"] = theme
    return body


def file_entry(theme: ThemeDefinition) -> dict:
    asset = theme.assets[0]
    return {
        "content_hash": asset.content_hash,
        "relative_path": asset.relative_path,
        "size_bytes": asset.size_bytes,
        "file_type": "theme",
    }


def client_for(body: dict, files: dict[str, bytes] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/manifest"):
            return httpx.Response(200, content=json.dumps(body))
        digest = path.rsplit("/", 1)[-1]
        for content in (files or {}).values():
            if hashlib.sha256(content).hexdigest() == digest:
                return httpx.Response(200, content=content)
        return httpx.Response(404)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )


class TestThemeSync:
    async def test_theme_is_stored_and_reported(self, cfg: DeviceConfig) -> None:
        theme = theme_with_sound()
        received: list[ThemeDefinition | None] = []
        body = manifest(cfg, theme.model_dump(mode="json"), [file_entry(theme)])
        async with client_for(body, {"s": SOUND}) as http:
            await SyncClient(cfg, http_client=http, on_theme=received.append).sync()
        assert received == [theme]
        conn = init_db(cfg.db_path)
        assert get_theme_definition(conn) == theme
        assert get_sync_state(conn, "last_manifest_hash") == "hash-1"
        conn.close()

    async def test_asset_files_are_downloaded_verified_and_kept(
        self, cfg: DeviceConfig
    ) -> None:
        theme = theme_with_sound()
        body = manifest(cfg, theme.model_dump(mode="json"), [file_entry(theme)])
        async with client_for(body, {"s": SOUND}) as http:
            await SyncClient(cfg, http_client=http).sync()
            # A second sync (files present) must not prune them.
            await SyncClient(cfg, http_client=http).sync()
        assert (cfg.media_root / theme.assets[0].relative_path).read_bytes() == SOUND

    async def test_a_corrupt_asset_is_not_kept_and_is_retried(
        self, cfg: DeviceConfig
    ) -> None:
        theme = theme_with_sound()
        body = manifest(cfg, theme.model_dump(mode="json"), [file_entry(theme)])
        async with client_for(body, {"s": b"tampered"}) as http:  # wrong hash: 404
            await SyncClient(cfg, http_client=http).sync()
        assert not (cfg.media_root / theme.assets[0].relative_path).exists()
        conn = init_db(cfg.db_path)
        # The manifest is not recorded as synced, so the next cycle retries.
        assert get_sync_state(conn, "last_manifest_hash") is None
        conn.close()

    @pytest.mark.parametrize(
        "broken",
        [
            {"id": "Bad Id", "name": "x", "colors": {}},
            {"id": "x", "name": "x", "colors": {"bg": "red"}},
            "not even an object",
            123,
            [],
        ],
    )
    async def test_a_broken_theme_never_stops_media_sync(
        self, cfg: DeviceConfig, broken: object
    ) -> None:
        media_id = str(uuid.uuid4())
        body = manifest(cfg, broken)
        body["media"] = [
            {
                "media_id": media_id,
                "media_type": "music",
                "playlist_title": "P",
                "title": "T",
            }
        ]
        received: list[ThemeDefinition | None] = []
        async with client_for(body) as http:
            await SyncClient(cfg, http_client=http, on_theme=received.append).sync()
        conn = init_db(cfg.db_path)
        assert media_id in get_all_media_ids(conn)
        assert get_theme_definition(conn) is None
        assert get_sync_state(conn, "last_manifest_hash") == "hash-1"
        conn.close()
        assert received == [None]

    async def test_no_theme_clears_a_stored_one(self, cfg: DeviceConfig) -> None:
        conn = init_db(cfg.db_path)
        set_theme_definition(conn, theme_with_sound())
        conn.commit()
        conn.close()
        received: list[ThemeDefinition | None] = []
        async with client_for(manifest(cfg, ...)) as http:
            await SyncClient(cfg, http_client=http, on_theme=received.append).sync()
        conn = init_db(cfg.db_path)
        assert get_theme_definition(conn) is None
        conn.close()
        assert received == [None]

    async def test_manifest_from_an_older_server_has_no_theme(
        self, cfg: DeviceConfig
    ) -> None:
        async with client_for(manifest(cfg, ...)) as http:
            await SyncClient(cfg, http_client=http).sync()
        conn = init_db(cfg.db_path)
        assert get_theme_definition(conn) is None
        conn.close()

    async def test_304_keeps_the_stored_theme(self, cfg: DeviceConfig) -> None:
        theme = theme_with_sound()
        conn = init_db(cfg.db_path)
        set_theme_definition(conn, theme)
        conn.execute(
            "INSERT OR REPLACE INTO sync_state VALUES ('last_manifest_hash', 'h')"
        )
        conn.commit()
        conn.close()
        received: list[ThemeDefinition | None] = []
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(304)),
            base_url="http://test",
        )
        async with http:
            await SyncClient(cfg, http_client=http, on_theme=received.append).sync()
        assert received == []
        conn = init_db(cfg.db_path)
        assert get_theme_definition(conn) == theme
        conn.close()


class TestThemeStorage:
    def test_unreadable_stored_theme_reads_as_none(self, cfg: DeviceConfig) -> None:
        conn = init_db(cfg.db_path)
        conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('theme', '{oops')")
        conn.commit()
        assert get_theme_definition(conn) is None
        conn.close()

    def test_round_trip_and_clear(self, cfg: DeviceConfig) -> None:
        conn = init_db(cfg.db_path)
        theme = theme_with_sound()
        set_theme_definition(conn, theme)
        assert get_theme_definition(conn) == theme
        set_theme_definition(conn, None)
        assert get_theme_definition(conn) is None
        conn.close()
