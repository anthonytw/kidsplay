"""Tests for the per-profile settings models."""

import uuid
from datetime import time

import pytest
from pydantic import ValidationError

from kidsplay_models import (
    PROFILE_SETTINGS_VERSION,
    BedtimeMode,
    BedtimeWindow,
    ProfileSettings,
    SyncManifest,
    Weekday,
)


class TestProfileSettings:
    def test_defaults(self) -> None:
        s = ProfileSettings()
        assert s.version == PROFILE_SETTINGS_VERSION
        assert s.max_volume == 100
        assert s.volume_buttons is None  # undecided: the device's profile decides
        assert s.bedtime_mode is BedtimeMode.OFF
        assert s.bedtime_schedule == {}

    def test_undecided_volume_buttons_are_left_out_of_the_json(self) -> None:
        """An older device has a plain ``bool`` field and must never see null."""
        assert "volume_buttons" not in ProfileSettings().model_dump(mode="json")
        assert "volume_buttons" not in ProfileSettings().model_dump_json()

    @pytest.mark.parametrize("value", [True, False])
    def test_explicit_volume_buttons_round_trip(self, value: bool) -> None:
        s = ProfileSettings(volume_buttons=value)
        assert s.model_dump(mode="json")["volume_buttons"] is value
        assert ProfileSettings.model_validate_json(s.model_dump_json()) == s

    def test_missing_or_null_volume_buttons_mean_undecided(self) -> None:
        assert ProfileSettings.model_validate({}).volume_buttons is None
        assert ProfileSettings.model_validate({"volume_buttons": None}) == (
            ProfileSettings()
        )

    def test_ui_sounds_default_on_and_round_trip(self) -> None:
        assert ProfileSettings().ui_sounds is True
        assert ProfileSettings().model_dump(mode="json")["ui_sounds"] is True
        off = ProfileSettings(ui_sounds=False)
        assert ProfileSettings.model_validate_json(off.model_dump_json()) == off
        assert not ProfileSettings.model_validate_json(off.model_dump_json()).ui_sounds

    def test_missing_ui_sounds_from_an_older_server_means_on(self) -> None:
        assert ProfileSettings.model_validate({"max_volume": 40}).ui_sounds is True

    def test_non_boolean_ui_sounds_rejected_by_the_model(self) -> None:
        """The API says so; it is the device that reads it leniently."""
        with pytest.raises(ValidationError):
            ProfileSettings.model_validate({"ui_sounds": "banana"})

    def test_empty_json_gives_defaults(self) -> None:
        assert ProfileSettings.model_validate({}) == ProfileSettings()

    @pytest.mark.parametrize("value", [-1, 101])
    def test_max_volume_out_of_range_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            ProfileSettings(max_volume=value)

    @pytest.mark.parametrize("value", [0, 55, 100])
    def test_max_volume_bounds_accepted(self, value: int) -> None:
        assert ProfileSettings(max_volume=value).max_volume == value

    def test_unknown_field_ignored(self) -> None:
        s = ProfileSettings.model_validate(
            {"max_volume": 40, "sparkles": "fr", "future": {"x": 1}}
        )
        assert s.max_volume == 40
        assert "sparkles" not in s.model_dump()

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProfileSettings.model_validate({"bedtime_mode": "party"})

    def test_schedule_round_trip(self) -> None:
        s = ProfileSettings(
            bedtime_mode=BedtimeMode.SLEEP_SCREEN,
            bedtime_schedule={
                Weekday.MONDAY: BedtimeWindow(bedtime=time(20), wake=time(7))
            },
        )
        data = s.model_dump(mode="json")
        assert data["bedtime_schedule"] == {
            "mon": {"bedtime": "20:00:00", "wake": "07:00:00"}
        }
        assert ProfileSettings.model_validate(data) == s

    def test_schedule_accepts_hh_mm(self) -> None:
        s = ProfileSettings.model_validate(
            {"bedtime_schedule": {"fri": {"bedtime": "20:30", "wake": "08:00"}}}
        )
        assert s.bedtime_schedule[Weekday.FRIDAY].bedtime == time(20, 30)

    def test_unknown_weekday_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProfileSettings.model_validate(
                {"bedtime_schedule": {"funday": {"bedtime": "20:00", "wake": "7:00"}}}
            )


class TestBedtimeWindow:
    def test_equal_times_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BedtimeWindow(bedtime=time(20), wake=time(20))

    @pytest.mark.parametrize(
        ("bedtime", "wake"),
        [("20:00+05:00", "07:00"), ("20:00", "07:00Z"), ("20:00Z", "07:00-04:00")],
    )
    def test_timezone_suffix_rejected(self, bedtime: str, wake: str) -> None:
        # An aware time cannot be compared with a naive one on the device.
        with pytest.raises(ValidationError, match="without a timezone"):
            BedtimeWindow.model_validate({"bedtime": bedtime, "wake": wake})

    def test_unknown_field_ignored(self) -> None:
        w = BedtimeWindow.model_validate(
            {"bedtime": "20:00", "wake": "07:00", "dim": True}
        )
        assert w.wake == time(7)


class TestWeekday:
    def test_from_index_matches_date_weekday(self) -> None:
        assert Weekday.from_index(0) is Weekday.MONDAY
        assert Weekday.from_index(6) is Weekday.SUNDAY

    def test_from_index_wraps(self) -> None:
        assert Weekday.from_index(7) is Weekday.MONDAY


class TestManifestSettings:
    def _manifest(self, **extra: object) -> dict[str, object]:
        return {
            "device_id": str(uuid.uuid4()),
            "profile_id": str(uuid.uuid4()),
            "manifest_hash": "h",
            **extra,
        }

    def test_manifest_without_settings_gets_defaults(self) -> None:
        m = SyncManifest.model_validate(self._manifest())
        assert m.profile_settings == ProfileSettings()
        assert m.sync_interval_seconds is None

    def test_unknown_settings_field_in_manifest_ignored(self) -> None:
        m = SyncManifest.model_validate(
            self._manifest(
                profile_settings={"max_volume": 30, "theme": "ocean"},
                some_future_section=[1, 2, 3],
            )
        )
        assert m.profile_settings.max_volume == 30

    def test_sync_interval_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            SyncManifest.model_validate(self._manifest(sync_interval_seconds=0))
