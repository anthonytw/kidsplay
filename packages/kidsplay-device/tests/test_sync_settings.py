"""Tests for the settings part of device sync.

Covers: profile settings and the sync interval persisted from the manifest,
the server's ``Date`` header recorded for the bedtime clock, and older-device
compatibility (unknown manifest fields are ignored).
"""

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from kidsplay_device.config import DeviceConfig
from kidsplay_device.database import (
    ClockHeartbeat,
    get_all_media_ids,
    get_clock_heartbeat,
    get_last_server_time,
    get_profile_settings,
    get_sync_interval,
    get_sync_state,
    init_db,
    set_clock_heartbeat,
    set_last_server_time,
    set_profile_settings,
    set_sync_interval,
    set_sync_state,
)
from kidsplay_device.sync import SyncClient
from kidsplay_models import BedtimeMode, ProfileSettings

DATE_HEADER = "Tue, 29 Sep 2026 19:30:00 GMT"
SERVER_TIME = datetime(2026, 9, 29, 19, 30, tzinfo=UTC)


@pytest.fixture
def cfg(tmp_path: Path) -> DeviceConfig:
    return DeviceConfig(
        server_url="http://test",
        device_id=str(uuid.uuid4()),
        api_key="k",
        media_root=tmp_path / "media",
        db_path=tmp_path / "device.db",
        sync_interval_seconds=900,
    )


def manifest_json(cfg: DeviceConfig, **extra: object) -> dict[str, object]:
    return {
        "device_id": cfg.device_id,
        "profile_id": str(uuid.uuid4()),
        "manifest_hash": "hash-1",
        "files": [],
        "media": [],
        **extra,
    }


def mock_client(
    body: dict[str, object] | None, status: int = 200, date: str | None = DATE_HEADER
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Date": date} if date else {}
        if body is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, headers=headers, content=json.dumps(body))

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )


class TestSettingsFromManifest:
    async def test_settings_persisted_and_reported(self, cfg: DeviceConfig) -> None:
        received: list[ProfileSettings] = []
        body = manifest_json(
            cfg,
            profile_settings={"max_volume": 35, "bedtime_mode": "audiobooks_only"},
            sync_interval_seconds=300,
        )
        async with mock_client(body) as http:
            client = SyncClient(cfg, http_client=http, on_settings=received.append)
            await client.sync()

        assert [s.max_volume for s in received] == [35]
        conn = init_db(cfg.db_path)
        stored = get_profile_settings(conn)
        assert stored.max_volume == 35
        assert stored.bedtime_mode is BedtimeMode.AUDIOBOOKS_ONLY
        assert get_sync_interval(conn) == 300
        conn.close()
        assert client.sync_interval_seconds == 300

    async def test_unknown_settings_fields_ignored(self, cfg: DeviceConfig) -> None:
        """Older-device compatibility: newer fields must not break sync."""
        body = manifest_json(
            cfg,
            profile_settings={"max_volume": 20, "language": "fr", "theme": "sea"},
            some_new_section={"anything": [1, 2]},
        )
        async with mock_client(body) as http:
            await SyncClient(cfg, http_client=http).sync()
        conn = init_db(cfg.db_path)
        assert get_profile_settings(conn).max_volume == 20
        assert get_sync_state(conn, "last_manifest_hash") == "hash-1"
        conn.close()

    async def test_manifest_from_older_server_gives_defaults(
        self, cfg: DeviceConfig
    ) -> None:
        async with mock_client(manifest_json(cfg)) as http:
            client = SyncClient(cfg, http_client=http)
            await client.sync()
        conn = init_db(cfg.db_path)
        assert get_profile_settings(conn) == ProfileSettings()
        assert get_sync_interval(conn) is None
        conn.close()
        assert client.sync_interval_seconds == 900

    async def test_304_keeps_settings(self, cfg: DeviceConfig) -> None:
        conn = init_db(cfg.db_path)
        set_profile_settings(conn, ProfileSettings(max_volume=45))
        set_sync_state(conn, "last_manifest_hash", "hash-1")
        conn.commit()
        conn.close()
        received: list[ProfileSettings] = []
        async with mock_client(None, status=304) as http:
            await SyncClient(cfg, http_client=http, on_settings=received.append).sync()
        assert received == []
        conn = init_db(cfg.db_path)
        assert get_profile_settings(conn).max_volume == 45
        conn.close()


AUDIO = b"pretend mp3 bytes"
AUDIO_HASH = hashlib.sha256(AUDIO).hexdigest()
MEDIA_ID = str(uuid.uuid4())


def manifest_with_media(cfg: DeviceConfig, **extra: object) -> dict[str, object]:
    """A manifest carrying one real audio file, so media syncing is provable."""
    return manifest_json(
        cfg,
        files=[
            {
                "content_hash": AUDIO_HASH,
                "relative_path": "audio/a.mp3",
                "size_bytes": len(AUDIO),
                "file_type": "audio",
            }
        ],
        media=[
            {
                "media_id": MEDIA_ID,
                "media_type": "music",
                "playlist_title": "Album",
                "title": "Song",
                "audio_path": "audio/a.mp3",
            }
        ],
        **extra,
    )


def serving_client(body: dict[str, object]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/sync/file/"):
            return httpx.Response(200, content=AUDIO)
        return httpx.Response(
            200, headers={"Date": DATE_HEADER}, content=json.dumps(body)
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )


class TestSettingsApplyBeforeDownloads:
    """A big media batch must not delay a volume cap or bedtime change."""

    async def test_settings_are_stored_and_applied_before_the_first_download(
        self, cfg: DeviceConfig
    ) -> None:
        body = manifest_with_media(
            cfg, profile_settings={"max_volume": 35}, sync_interval_seconds=300
        )
        events: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/api/v1/sync/file/"):
                conn = init_db(cfg.db_path)
                try:
                    stored = get_profile_settings(conn).max_volume
                    interval = get_sync_interval(conn)
                finally:
                    conn.close()
                events.append(f"download(stored cap={stored}, interval={interval})")
                return httpx.Response(200, content=AUDIO)
            return httpx.Response(
                200, headers={"Date": DATE_HEADER}, content=json.dumps(body)
            )

        def on_settings(settings: ProfileSettings) -> None:
            events.append(f"applied cap={settings.max_volume}")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://test"
        ) as http:
            client = SyncClient(cfg, http_client=http, on_settings=on_settings)
            await client.sync()

        assert events == [
            "applied cap=35",
            "download(stored cap=35, interval=300)",
        ]
        assert client.sync_interval_seconds == 300

    async def test_settings_apply_even_when_every_download_fails(
        self, cfg: DeviceConfig
    ) -> None:
        body = manifest_with_media(cfg, profile_settings={"max_volume": 20})
        applied: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/api/v1/sync/file/"):
                return httpx.Response(500)
            return httpx.Response(
                200, headers={"Date": DATE_HEADER}, content=json.dumps(body)
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://test"
        ) as http:
            client = SyncClient(
                cfg,
                http_client=http,
                on_settings=lambda s: applied.append(s.max_volume),
            )
            await client.sync()

        assert applied == [20]
        conn = init_db(cfg.db_path)
        assert get_profile_settings(conn).max_volume == 20
        # The manifest is not marked done, so the files are retried.
        assert get_sync_state(conn, "last_manifest_hash") is None
        conn.close()


class TestUnreadableSettingsDoNotBreakSync:
    """A newer server's settings must not stop media syncing on this device."""

    @pytest.mark.parametrize(
        "bad",
        [
            {"bedtime_mode": "lullaby"},  # new enum value
            {"max_volume": 250},  # out of range
            {"bedtime_schedule": {"holiday": {"bedtime": "20:00", "wake": "07:00"}}},
            {"version": 2, "max_volume": 10},  # newer than supported
        ],
        ids=["new-enum", "out-of-range", "new-weekday-key", "version-too-new"],
    )
    async def test_media_syncs_and_previous_settings_kept(
        self,
        cfg: DeviceConfig,
        bad: dict[str, object],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        conn = init_db(cfg.db_path)
        set_profile_settings(conn, ProfileSettings(max_volume=45))
        conn.commit()
        conn.close()
        received: list[ProfileSettings] = []
        body = manifest_with_media(cfg, profile_settings=bad, sync_interval_seconds=300)
        with caplog.at_level(logging.WARNING, logger="kidsplay_device.sync"):
            async with serving_client(body) as http:
                client = SyncClient(cfg, http_client=http, on_settings=received.append)
                await client.sync()

        # Media synced normally.
        assert (cfg.media_root / "audio/a.mp3").read_bytes() == AUDIO
        conn = init_db(cfg.db_path)
        assert get_all_media_ids(conn) == {MEDIA_ID}
        # Previous settings retained, and not re-announced to the player.
        assert get_profile_settings(conn).max_volume == 45
        assert received == []
        # Other manifest values still apply.
        assert get_sync_interval(conn) == 300
        # The manifest is not marked as done, so the settings are retried.
        assert get_sync_state(conn, "last_manifest_hash") is None
        conn.close()
        assert any(
            "profile settings" in r.message.lower() and r.levelno == logging.WARNING
            for r in caplog.records
        )

    @pytest.mark.parametrize("junk", ["banana", 7, None, [], {"a": 1}])
    async def test_invalid_ui_sounds_means_on_and_keeps_the_rest(
        self, cfg: DeviceConfig, junk: object
    ) -> None:
        received: list[ProfileSettings] = []
        body = manifest_with_media(
            cfg, profile_settings={"max_volume": 35, "ui_sounds": junk}
        )
        async with serving_client(body) as http:
            await SyncClient(cfg, http_client=http, on_settings=received.append).sync()
        assert [(s.max_volume, s.ui_sounds) for s in received] == [(35, True)]

    async def test_missing_ui_sounds_means_on_and_false_is_kept(
        self, cfg: DeviceConfig
    ) -> None:
        received: list[ProfileSettings] = []
        for raw in ({"max_volume": 35}, {"ui_sounds": False}):
            body = manifest_with_media(cfg, profile_settings=raw)
            async with serving_client(body) as http:
                client = SyncClient(cfg, http_client=http, on_settings=received.append)
                await client.sync()
        assert [s.ui_sounds for s in received] == [True, False]
        conn = init_db(cfg.db_path)
        assert get_profile_settings(conn).ui_sounds is False
        conn.close()

    async def test_settings_recover_once_readable(self, cfg: DeviceConfig) -> None:
        bad = manifest_with_media(cfg, profile_settings={"version": 2})
        async with serving_client(bad) as http:
            await SyncClient(cfg, http_client=http).sync()
        good = manifest_with_media(cfg, profile_settings={"max_volume": 30})
        async with serving_client(good) as http:
            await SyncClient(cfg, http_client=http).sync()
        conn = init_db(cfg.db_path)
        assert get_profile_settings(conn).max_volume == 30
        assert get_sync_state(conn, "last_manifest_hash") == "hash-1"
        conn.close()

    async def test_unknown_extra_field_still_ignored(self, cfg: DeviceConfig) -> None:
        body = manifest_with_media(
            cfg, profile_settings={"max_volume": 20, "language": "fr"}
        )
        received: list[ProfileSettings] = []
        async with serving_client(body) as http:
            await SyncClient(cfg, http_client=http, on_settings=received.append).sync()
        assert [s.max_volume for s in received] == [20]
        assert (cfg.media_root / "audio/a.mp3").exists()


class TestServerTime:
    @pytest.mark.parametrize("status", [200, 304])
    async def test_date_header_recorded(self, cfg: DeviceConfig, status: int) -> None:
        seen: list[datetime] = []
        body = manifest_json(cfg) if status == 200 else None
        async with mock_client(body, status=status) as http:
            await SyncClient(cfg, http_client=http, on_server_time=seen.append).sync()
        assert seen == [SERVER_TIME]
        conn = init_db(cfg.db_path)
        assert get_last_server_time(conn) == SERVER_TIME
        conn.close()

    async def test_missing_date_header_ignored(self, cfg: DeviceConfig) -> None:
        seen: list[datetime] = []
        async with mock_client(manifest_json(cfg), date=None) as http:
            await SyncClient(cfg, http_client=http, on_server_time=seen.append).sync()
        assert seen == []

    async def test_garbage_date_header_ignored(self, cfg: DeviceConfig) -> None:
        seen: list[datetime] = []
        async with mock_client(manifest_json(cfg), date="yesterday-ish") as http:
            await SyncClient(cfg, http_client=http, on_server_time=seen.append).sync()
        assert seen == []

    async def test_error_response_not_recorded(self, cfg: DeviceConfig) -> None:
        seen: list[datetime] = []
        async with mock_client({"detail": "no"}, status=500) as http:
            await SyncClient(cfg, http_client=http, on_server_time=seen.append).sync()
        assert seen == []


class TestSyncInterval:
    def test_loads_persisted_interval(self, cfg: DeviceConfig) -> None:
        conn = init_db(cfg.db_path)
        set_sync_interval(conn, 120)
        conn.commit()
        conn.close()
        client = SyncClient(cfg)
        client._load_sync_interval()
        assert client.sync_interval_seconds == 120

    def test_defaults_to_config(self, cfg: DeviceConfig) -> None:
        client = SyncClient(cfg)
        client._load_sync_interval()
        assert client.sync_interval_seconds == 900


class TestDeviceSettingsStorage:
    def test_profile_settings_default(self, tmp_path: Path) -> None:
        conn = init_db(tmp_path / "d.db")
        assert get_profile_settings(conn) == ProfileSettings()

    def test_profile_settings_round_trip(self, tmp_path: Path) -> None:
        conn = init_db(tmp_path / "d.db")
        set_profile_settings(conn, ProfileSettings(max_volume=10))
        assert get_profile_settings(conn).max_volume == 10

    def test_profile_settings_unreadable_gives_defaults(self, tmp_path: Path) -> None:
        conn = init_db(tmp_path / "d.db")
        set_sync_state(conn, "profile_settings", "{not json")
        assert get_profile_settings(conn) == ProfileSettings()

    def test_sync_interval_set_and_clear(self, tmp_path: Path) -> None:
        conn = init_db(tmp_path / "d.db")
        assert get_sync_interval(conn) is None
        set_sync_interval(conn, 60)
        assert get_sync_interval(conn) == 60
        set_sync_interval(conn, None)
        assert get_sync_interval(conn) is None

    def test_sync_interval_garbage_ignored(self, tmp_path: Path) -> None:
        conn = init_db(tmp_path / "d.db")
        set_sync_state(conn, "sync_interval_seconds", "soon")
        assert get_sync_interval(conn) is None

    def test_last_server_time_round_trip(self, tmp_path: Path) -> None:
        conn = init_db(tmp_path / "d.db")
        assert get_last_server_time(conn) is None
        set_last_server_time(conn, SERVER_TIME)
        assert get_last_server_time(conn) == SERVER_TIME

    def test_last_server_time_naive_or_garbage_ignored(self, tmp_path: Path) -> None:
        conn = init_db(tmp_path / "d.db")
        set_sync_state(conn, "last_server_time", "2026-01-01T00:00:00")
        assert get_last_server_time(conn) is None
        set_sync_state(conn, "last_server_time", "never")
        assert get_last_server_time(conn) is None


class TestClockHeartbeatStorage:
    def test_round_trip(self, tmp_path: Path) -> None:
        conn = init_db(tmp_path / "d.db")
        assert get_clock_heartbeat(conn) is None
        beat = ClockHeartbeat(SERVER_TIME, "boot-1", False)
        set_clock_heartbeat(conn, beat)
        conn.commit()
        assert get_clock_heartbeat(conn) == beat

    @pytest.mark.parametrize(
        "raw",
        [
            "{not json",
            "[]",
            '{"wall": "nope", "trusted": true}',
            '{"wall": "2026-09-29T19:30:00", "trusted": true}',
            '{"wall": "2026-09-29T19:30:00+00:00"}',
        ],
    )
    def test_unreadable_is_none(self, tmp_path: Path, raw: str) -> None:
        conn = init_db(tmp_path / "d.db")
        set_sync_state(conn, "clock_heartbeat", raw)
        assert get_clock_heartbeat(conn) is None
