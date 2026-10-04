"""Tests for kidsplay_server.server_settings and the settings tables."""

import uuid
from datetime import datetime

import aiosqlite
import pytest
from pydantic import ValidationError

from kidsplay_models import BedtimeMode, Profile, ProfileSettings
from kidsplay_server.database import (
    create_profile,
    delete_profile,
    delete_server_setting_value,
    get_profile_settings,
    get_server_setting_values,
    set_profile_settings,
    set_server_setting_value,
)
from kidsplay_server.processing.audio import LoudnessConfig
from kidsplay_server.server_settings import (
    EnvLockedError,
    ServerSettings,
    ServerSettingsUpdate,
    check_environment,
    load_loudness_config,
    load_server_settings,
    loudness_config,
    update_server_settings,
)

NOW = datetime(2026, 1, 1, 12, 0)


class TestProfileSettingsStorage:
    async def test_defaults_without_row(self, db: aiosqlite.Connection) -> None:
        assert await get_profile_settings(db, uuid.uuid4()) == ProfileSettings()

    async def test_round_trip(self, db: aiosqlite.Connection) -> None:
        profile = Profile(name="Leo")
        await create_profile(db, profile)
        settings = ProfileSettings(max_volume=30, bedtime_mode=BedtimeMode.OFF)
        await set_profile_settings(db, profile.id, settings, NOW)
        await db.commit()
        assert await get_profile_settings(db, profile.id) == settings

    async def test_replace(self, db: aiosqlite.Connection) -> None:
        profile = Profile(name="Leo")
        await create_profile(db, profile)
        await set_profile_settings(db, profile.id, ProfileSettings(max_volume=30), NOW)
        await set_profile_settings(db, profile.id, ProfileSettings(max_volume=80), NOW)
        assert (await get_profile_settings(db, profile.id)).max_volume == 80

    async def test_unknown_profile_rejected(self, db: aiosqlite.Connection) -> None:
        with pytest.raises(aiosqlite.IntegrityError):
            await set_profile_settings(db, uuid.uuid4(), ProfileSettings(), NOW)

    async def test_stored_unknown_field_dropped(self, db: aiosqlite.Connection) -> None:
        profile = Profile(name="Leo")
        await create_profile(db, profile)
        await db.execute(
            "INSERT INTO profile_settings VALUES (?, ?, ?)",
            (str(profile.id), '{"max_volume": 20, "theme": "x"}', NOW.isoformat()),
        )
        assert (await get_profile_settings(db, profile.id)).max_volume == 20

    async def test_cascade_on_profile_delete(self, db: aiosqlite.Connection) -> None:
        profile = Profile(name="Leo")
        await create_profile(db, profile)
        await set_profile_settings(db, profile.id, ProfileSettings(max_volume=5), NOW)
        await delete_profile(db, profile.id)
        async with db.execute("SELECT COUNT(*) FROM profile_settings") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0


class TestServerSettingStorage:
    async def test_set_get_delete(self, db: aiosqlite.Connection) -> None:
        await set_server_setting_value(db, "webp_quality", "70", NOW)
        assert await get_server_setting_values(db) == {"webp_quality": "70"}
        await delete_server_setting_value(db, "webp_quality")
        assert await get_server_setting_values(db) == {}


class TestLoadServerSettings:
    async def test_defaults(self, db: aiosqlite.Connection) -> None:
        resolved = await load_server_settings(db, environ={})
        assert resolved.values == ServerSettings()
        assert resolved.env_locked == frozenset()
        assert resolved.saved == frozenset()

    async def test_saved_value_used(self, db: aiosqlite.Connection) -> None:
        await set_server_setting_value(db, "sync_interval_seconds", "120", NOW)
        resolved = await load_server_settings(db, environ={})
        assert resolved.values.sync_interval_seconds == 120
        assert resolved.saved == frozenset({"sync_interval_seconds"})

    async def test_env_beats_saved(self, db: aiosqlite.Connection) -> None:
        await set_server_setting_value(db, "webp_quality", "70", NOW)
        resolved = await load_server_settings(
            db, environ={"KIDSPLAY_WEBP_QUALITY": "90"}
        )
        assert resolved.values.webp_quality == 90
        assert resolved.env_locked == frozenset({"webp_quality"})

    async def test_blank_env_ignored(self, db: aiosqlite.Connection) -> None:
        resolved = await load_server_settings(
            db, environ={"KIDSPLAY_WEBP_QUALITY": "  "}
        )
        assert resolved.env_locked == frozenset()

    async def test_invalid_env_raises(self, db: aiosqlite.Connection) -> None:
        with pytest.raises(ValueError):
            await load_server_settings(db, environ={"KIDSPLAY_WEBP_QUALITY": "high"})

    async def test_invalid_saved_value_falls_back(
        self, db: aiosqlite.Connection
    ) -> None:
        await set_server_setting_value(db, "webp_quality", "500", NOW)
        resolved = await load_server_settings(db, environ={})
        assert resolved.values.webp_quality == 85

    async def test_reads_os_environ_by_default(
        self, db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_SYNC_INTERVAL_SECONDS", "600")
        resolved = await load_server_settings(db)
        assert resolved.values.sync_interval_seconds == 600


class TestUpdateServerSettings:
    async def test_saves_and_resets(self, db: aiosqlite.Connection) -> None:
        resolved = await update_server_settings(
            db, ServerSettingsUpdate(webp_quality=50), environ={}
        )
        assert resolved.values.webp_quality == 50
        resolved = await update_server_settings(
            db, ServerSettingsUpdate(reset=["webp_quality"]), environ={}
        )
        assert resolved.values.webp_quality == 85
        assert resolved.saved == frozenset()

    async def test_out_of_range_rejected(self, db: aiosqlite.Connection) -> None:
        with pytest.raises(ValueError):
            await update_server_settings(
                db, ServerSettingsUpdate(sync_interval_seconds=1), environ={}
            )
        assert await get_server_setting_values(db) == {}

    async def test_env_locked_rejected(self, db: aiosqlite.Connection) -> None:
        env = {"KIDSPLAY_SYNC_INTERVAL_SECONDS": "300"}
        with pytest.raises(EnvLockedError):
            await update_server_settings(
                db, ServerSettingsUpdate(sync_interval_seconds=600), environ=env
            )

    async def test_unknown_reset_key_rejected(self, db: aiosqlite.Connection) -> None:
        with pytest.raises(ValueError):
            await update_server_settings(
                db, ServerSettingsUpdate(reset=["nope"]), environ={}
            )


class TestCheckEnvironment:
    def test_empty_ok(self) -> None:
        check_environment({})

    def test_valid_ok(self) -> None:
        check_environment(
            {"KIDSPLAY_WEBP_QUALITY": "80", "KIDSPLAY_SYNC_INTERVAL_SECONDS": "60"}
        )

    @pytest.mark.parametrize(
        "env",
        [
            {"KIDSPLAY_WEBP_QUALITY": "0"},
            {"KIDSPLAY_SYNC_INTERVAL_SECONDS": "fast"},
        ],
    )
    def test_invalid_raises_naming_variable(self, env: dict[str, str]) -> None:
        with pytest.raises(ValueError, match=next(iter(env))):
            check_environment(env)

    def test_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIDSPLAY_WEBP_QUALITY", "999")
        with pytest.raises(ValueError):
            check_environment()


class TestLoudnessSettings:
    """The loudness targets are server settings; the switches stay in env."""

    async def test_defaults(self, db: aiosqlite.Connection) -> None:
        assert await load_loudness_config(db, {}) == LoudnessConfig()

    async def test_saved_targets(self, db: aiosqlite.Connection) -> None:
        await update_server_settings(
            db,
            ServerSettingsUpdate(
                loudness_target_lufs=-18,
                loudness_target_lufs_music=-14.5,
                loudness_target_lufs_audiobook=-12,
            ),
            {},
        )
        assert await load_loudness_config(db, {}) == LoudnessConfig(
            target_lufs=-18.0, music_target_lufs=-14.5, audiobook_target_lufs=-12.0
        )

    async def test_reset_returns_a_per_type_target_to_the_overall_one(
        self, db: aiosqlite.Connection
    ) -> None:
        await update_server_settings(
            db, ServerSettingsUpdate(loudness_target_lufs_music=-14), {}
        )
        after = await update_server_settings(
            db, ServerSettingsUpdate(reset=["loudness_target_lufs_music"]), {}
        )
        assert after.values.loudness_target_lufs_music is None
        assert (await load_loudness_config(db, {})).music_target_lufs is None

    async def test_environment_locks_and_wins(self, db: aiosqlite.Connection) -> None:
        await update_server_settings(
            db, ServerSettingsUpdate(loudness_target_lufs=-20), {}
        )
        env = {"KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIOBOOK": "-12.5"}
        resolved = await load_server_settings(db, env)
        assert resolved.env_locked == {"loudness_target_lufs_audiobook"}
        assert resolved.values.loudness_target_lufs == -20
        assert (await load_loudness_config(db, env)).audiobook_target_lufs == -12.5
        with pytest.raises(EnvLockedError, match="KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIO"):
            await update_server_settings(
                db, ServerSettingsUpdate(loudness_target_lufs_audiobook=-10), env
            )

    @pytest.mark.parametrize("value", [-4.9, -70.1, 0])
    async def test_out_of_range_target_is_refused(
        self, db: aiosqlite.Connection, value: float
    ) -> None:
        with pytest.raises(ValueError, match="loudness_target_lufs"):
            await update_server_settings(
                db, ServerSettingsUpdate(loudness_target_lufs=value), {}
            )

    def test_switches_from_environment(self) -> None:
        cfg = loudness_config(
            ServerSettings(),
            {
                "KIDSPLAY_LOUDNORM": "disabled",
                "KIDSPLAY_LOUDNORM_LIMITING": " NEVER ",
                "KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP": " -2.5 ",
            },
        )
        assert cfg == LoudnessConfig(
            enabled=False, allow_limiting=False, true_peak_dbtp=-2.5
        )

    @pytest.mark.parametrize(
        ("var", "value", "message"),
        [
            ("KIDSPLAY_LOUDNORM", "off", "KIDSPLAY_LOUDNORM"),
            ("KIDSPLAY_LOUDNORM_LIMITING", "sometimes", "KIDSPLAY_LOUDNORM_LIMITING"),
            ("KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP", "loud", "TRUE_PEAK_DBTP"),
            ("KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP", "3", "true_peak_dbtp"),
            ("KIDSPLAY_LOUDNESS_TARGET_LUFS", "loud", "KIDSPLAY_LOUDNESS_TARGET_LUFS"),
            ("KIDSPLAY_LOUDNESS_TARGET_LUFS_MUSIC", "0", "KIDSPLAY_LOUDNESS_TARGET"),
        ],
    )
    def test_invalid_environment_stops_startup(
        self, var: str, value: str, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            check_environment({var: value})

    def test_empty_environment_values_mean_default(self) -> None:
        env = {
            "KIDSPLAY_LOUDNORM": "",
            "KIDSPLAY_LOUDNORM_LIMITING": "",
            "KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP": "",
            "KIDSPLAY_LOUDNESS_TARGET_LUFS": "",
        }
        check_environment(env)
        assert loudness_config(ServerSettings(), env) == LoudnessConfig()


class TestUnknownKeys:
    def test_update_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            ServerSettingsUpdate.model_validate({"webp_qualty": 50})
