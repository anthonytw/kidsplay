"""Tests for the parental-control logic (volume cap, fade, clock, bedtime).

Pure logic: every clock is a fake, so nothing depends on the real time.
"""

import logging
import subprocess
import threading
from datetime import UTC, datetime, time, timedelta, timezone

import pytest

from kidsplay_device.controls import (
    FADE_SECONDS,
    NOT_BEDTIME,
    RESTORED_CLOCK_WINDOW,
    UI_SOUND_LEVEL,
    VOLUME_STEP,
    BedtimeStatus,
    Fader,
    TimeSource,
    VolumeControl,
    bedtime_status,
    music_output_volume,
    read_ntp_synchronized,
    run_ntp_watch,
)
from kidsplay_device.database import ClockHeartbeat
from kidsplay_models import BedtimeMode, BedtimeWindow, ProfileSettings, Weekday

# A fixed non-UTC zone, so tests never depend on the machine's local zone.
TZ = timezone(timedelta(hours=-5))


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Local time on a day of the week 2026-09-28 (Mon) + ``day``."""
    base = datetime(2026, 9, 28, hour, minute, tzinfo=TZ)
    return base + timedelta(days=day)


MON, TUE, WED, SAT, SUN = 0, 1, 2, 5, 6


def settings_with(
    mode: BedtimeMode = BedtimeMode.SLEEP_SCREEN,
    **days: tuple[time, time],
) -> ProfileSettings:
    return ProfileSettings(
        bedtime_mode=mode,
        bedtime_schedule={
            Weekday(day): BedtimeWindow(bedtime=b, wake=w)
            for day, (b, w) in days.items()
        },
    )


class FakeMono:
    """Settable monotonic clock."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


class TestMusicOutputVolume:
    def test_plain_cap(self) -> None:
        assert music_output_volume(0.6) == pytest.approx(0.6)

    def test_ducked_leaves_room_for_the_beep(self) -> None:
        cap = 0.6
        ducked = music_output_volume(cap, ducked=True)
        assert ducked + UI_SOUND_LEVEL * cap == pytest.approx(cap)

    def test_fade_only_lowers(self) -> None:
        assert music_output_volume(0.6, fade_volume=0.1) == pytest.approx(0.1)
        assert music_output_volume(0.6, fade_volume=0.5) == pytest.approx(0.5)
        assert music_output_volume(0.6, fade_volume=0.9) == pytest.approx(0.6)

    def test_fade_and_duck_take_the_lower(self) -> None:
        assert music_output_volume(0.6, 0.5, ducked=True) == pytest.approx(0.3)
        assert music_output_volume(0.6, 0.1, ducked=True) == pytest.approx(0.1)


class TestVolumeControl:
    def test_default_is_full_volume(self) -> None:
        v = VolumeControl(ProfileSettings())
        assert v.music_volume() == 1.0
        assert v.ui_volume() == UI_SOUND_LEVEL

    def test_cap_applies_without_buttons(self) -> None:
        v = VolumeControl(ProfileSettings(max_volume=40), level=100)
        assert v.effective_percent() == 40
        assert v.music_volume() == pytest.approx(0.4)

    def test_ui_sounds_attenuated_by_cap(self) -> None:
        v = VolumeControl(ProfileSettings(max_volume=40))
        assert v.ui_volume() == pytest.approx(0.5 * 0.4)

    def test_level_ignored_when_buttons_off(self) -> None:
        v = VolumeControl(ProfileSettings(max_volume=80), level=10)
        assert v.effective_percent() == 80

    def test_level_used_under_cap_when_buttons_on(self) -> None:
        on = ProfileSettings(max_volume=80, volume_buttons=True)
        assert VolumeControl(on, level=30).effective_percent() == 30
        assert VolumeControl(on, level=95).effective_percent() == 80

    def test_step_is_noop_when_buttons_off(self) -> None:
        v = VolumeControl(ProfileSettings(max_volume=50), level=20)
        assert v.step(+1) is False
        assert v.step(-1) is False
        assert v.level == 20
        assert v.effective_percent() == 50

    def test_step_up_clamped_to_cap(self) -> None:
        v = VolumeControl(ProfileSettings(max_volume=35, volume_buttons=True), level=0)
        for _ in range(20):
            v.step(+1)
            assert v.effective_percent() <= 35
        assert v.effective_percent() == 35
        assert v.step(+1) is False

    def test_step_down_to_zero(self) -> None:
        v = VolumeControl(ProfileSettings(volume_buttons=True), level=15)
        assert v.step(-1) is True
        assert v.effective_percent() == 15 - VOLUME_STEP
        v.step(-1)
        assert v.effective_percent() == 0
        assert v.step(-1) is False

    def test_step_down_from_above_cap_starts_at_cap(self) -> None:
        v = VolumeControl(ProfileSettings(max_volume=50, volume_buttons=True), 90)
        v.step(-1)
        assert v.effective_percent() == 50 - VOLUME_STEP

    def test_level_clamped_on_construction(self) -> None:
        assert VolumeControl(ProfileSettings(), level=500).level == 100
        assert VolumeControl(ProfileSettings(), level=-3).level == 0

    def test_lowering_cap_lowers_volume(self) -> None:
        v = VolumeControl(ProfileSettings(volume_buttons=True), level=90)
        v.settings = ProfileSettings(max_volume=20, volume_buttons=True)
        assert v.effective_percent() == 20
        assert v.cap == 20


class TestFader:
    def test_linear_fade_over_duration(self) -> None:
        f = Fader(0.6, started_at=100.0)
        assert f.volume_at(100.0) == pytest.approx(0.6)
        assert f.volume_at(100.0 + FADE_SECONDS / 2) == pytest.approx(0.3)
        assert f.volume_at(100.0 + FADE_SECONDS) == 0.0
        assert f.done(100.0 + FADE_SECONDS)
        assert not f.done(100.0 + FADE_SECONDS - 0.1)

    def test_never_above_start_or_below_zero(self) -> None:
        f = Fader(0.5, started_at=10.0, duration=4.0)
        for t in (0.0, 9.0, 10.0, 11.5, 14.0, 99.0):
            assert 0.0 <= f.volume_at(t) <= 0.5

    def test_monotonic_decrease(self) -> None:
        f = Fader(1.0, started_at=0.0)
        samples = [f.volume_at(t / 10) for t in range(0, 110)]
        assert samples == sorted(samples, reverse=True)

    def test_zero_duration_is_done(self) -> None:
        f = Fader(1.0, started_at=0.0, duration=0.0)
        assert f.done(0.0)
        assert f.volume_at(0.0) == 0.0


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


class TestTimeSource:
    def test_wall_clock_when_never_synced(self) -> None:
        ts = TimeSource(None, wall=lambda: at(MON, 21), tz=TZ)
        assert ts.now() == at(MON, 21)
        assert not ts.synced_since_boot

    def test_wall_clock_trusted_when_after_last_sync(self) -> None:
        ts = TimeSource(at(MON, 9), wall=lambda: at(MON, 21), tz=TZ)
        assert ts.now() == at(MON, 21)

    def test_untrusted_when_earlier_than_last_sync(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Booted offline with the clock reset to 1970.
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        ts = TimeSource(at(MON, 9), wall=lambda: epoch, tz=TZ)
        with caplog.at_level(logging.WARNING, logger="kidsplay_device.controls"):
            assert ts.now() is None
            assert ts.now() is None
        # Logged once, not every check.
        assert sum("Clock untrusted" in r.message for r in caplog.records) == 1

    def test_server_time_wins_over_wall(self) -> None:
        mono = FakeMono(500.0)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        ts = TimeSource(at(MON, 9), wall=lambda: epoch, mono=mono, tz=TZ)
        assert ts.now() is None
        ts.note_server_time(at(TUE, 20).astimezone(UTC))
        assert ts.synced_since_boot
        assert ts.now() == at(TUE, 20)
        mono.value += 90 * 60
        assert ts.now() == at(TUE, 21, 30)

    def test_server_anchor_ignores_later_wall_jumps(self) -> None:
        wall = {"now": at(MON, 12)}
        mono = FakeMono()
        ts = TimeSource(None, wall=lambda: wall["now"], mono=mono, tz=TZ)
        ts.note_server_time(at(MON, 12))
        wall["now"] = at(MON, 23)  # someone changes the system clock
        assert ts.now() == at(MON, 12)

    def test_result_in_requested_zone(self) -> None:
        ts = TimeSource(None, wall=lambda: datetime(2026, 9, 28, 12, tzinfo=UTC), tz=TZ)
        now = ts.now()
        assert now is not None
        assert now.utcoffset() == timedelta(hours=-5)
        assert now.hour == 7

    def test_default_zone_is_local(self) -> None:
        now = TimeSource().now()
        assert now is not None
        assert now.tzinfo is not None


class TestRestoredClock:
    """Pi OS restores the clock from the last shutdown, so an offline boot at
    noon can read 20:31 -- later than the last sync, yet 15 hours wrong."""

    BOOT_A = "boot-a"
    BOOT_B = "boot-b"

    def beat(
        self, when: datetime, boot: str | None = BOOT_A, trusted: bool = True
    ) -> ClockHeartbeat:
        return ClockHeartbeat(wall=when, boot_id=boot, trusted=trusted)

    def source(self, wall: datetime, heartbeat: ClockHeartbeat | None) -> TimeSource:
        return TimeSource(
            at(MON, 9),  # last sync earlier the same day
            heartbeat=heartbeat,
            boot_id=self.BOOT_B,
            wall=lambda: wall,
            mono=FakeMono(),
            tz=TZ,
        )

    def test_restored_clock_after_reboot_not_enforced(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ts = self.source(at(MON, 20, 31), self.beat(at(MON, 20, 30)))
        with caplog.at_level(logging.WARNING, logger="kidsplay_device.controls"):
            assert ts.now() is None
            assert ts.now() is None
        assert sum("looks restored" in r.message for r in caplog.records) == 1
        settings = settings_with(mon=(time(20), time(7)))
        assert bedtime_status(settings, ts.now()) is NOT_BEDTIME

    def test_stays_untrusted_while_the_restored_clock_runs_on(self) -> None:
        wall = {"now": at(MON, 20, 31)}
        ts = TimeSource(
            at(MON, 9),
            heartbeat=self.beat(at(MON, 20, 30)),
            boot_id=self.BOOT_B,
            wall=lambda: wall["now"],
            tz=TZ,
        )
        assert ts.now() is None
        # An hour later the (still wrong) clock is past the window: the
        # decision was made at startup and holds until a sync.
        wall["now"] = at(MON, 21, 40)
        assert ts.now() is None

    def test_enforced_after_a_sync_this_boot(self) -> None:
        ts = self.source(at(MON, 20, 31), self.beat(at(MON, 20, 30)))
        assert ts.now() is None
        ts.note_server_time(at(TUE, 12))
        assert ts.now() == at(TUE, 12)

    def test_clock_far_past_heartbeat_follows_normal_rules(self) -> None:
        # Real time elapsed (e.g. NTP set the clock): next morning.
        ts = self.source(at(TUE, 7, 45), self.beat(at(MON, 20, 30)))
        assert ts.now() == at(TUE, 7, 45)

    def test_just_outside_the_window_is_trusted(self) -> None:
        beat = self.beat(at(MON, 20, 30))
        edge = at(MON, 20, 30) + RESTORED_CLOCK_WINDOW
        assert self.source(edge, beat).now() == edge
        assert self.source(edge - timedelta(seconds=1), beat).now() is None

    def test_clock_before_heartbeat_untrusted(self) -> None:
        ts = self.source(at(MON, 19, 0), self.beat(at(MON, 20, 30)))
        assert ts.now() is None

    def test_no_heartbeat_keeps_old_behaviour(self) -> None:
        assert self.source(at(MON, 20, 31), None).now() == at(MON, 20, 31)

    def test_player_restart_in_same_boot_is_trusted(self) -> None:
        beat = self.beat(at(MON, 20, 30), boot=self.BOOT_B)
        ts = self.source(at(MON, 20, 31), beat)
        assert ts.now() == at(MON, 20, 31)

    def test_restart_in_same_boot_keeps_untrusted_mark(self) -> None:
        beat = self.beat(at(MON, 20, 30), boot=self.BOOT_B, trusted=False)
        assert self.source(at(MON, 20, 50), beat).now() is None

    def test_unknown_boot_id_falls_back_to_window(self) -> None:
        beat = self.beat(at(MON, 20, 30), boot=None)
        ts = TimeSource(
            None,
            heartbeat=beat,
            boot_id=None,
            wall=lambda: at(MON, 20, 31),
            tz=TZ,
        )
        assert ts.now() is None

    def test_heartbeat_records_trust(self) -> None:
        ts = self.source(at(MON, 20, 31), self.beat(at(MON, 20, 30)))
        assert ts.heartbeat() == ClockHeartbeat(at(MON, 20, 31), self.BOOT_B, False)
        ts.note_server_time(at(MON, 20, 31))
        assert ts.heartbeat().trusted
        healthy = self.source(at(TUE, 8), None)
        assert healthy.heartbeat().trusted


class TestNtpSynchronized:
    """An NTP-synchronized clock confirms the time when no server sync can
    (all-in-one mode), and never blocks or trusts anything when there is no
    network."""

    def restored(self, wall: datetime) -> TimeSource:
        return TimeSource(
            at(MON, 9),
            heartbeat=ClockHeartbeat(at(MON, 20, 30), "boot-a", True),
            boot_id="boot-b",
            wall=lambda: wall,
            mono=FakeMono(),
            tz=TZ,
        )

    def test_restored_clock_is_untrusted_until_ntp_confirms(self) -> None:
        ts = self.restored(at(MON, 20, 31))
        assert ts.now() is None
        assert not ts.heartbeat().trusted
        ts.note_ntp_synchronized()
        assert ts.ntp_synchronized
        assert ts.now() == at(MON, 20, 31)
        assert ts.heartbeat().trusted
        assert not ts.synced_since_boot  # NTP is not a server sync

    def test_clock_earlier_than_last_sync_is_trusted_once_ntp_synced(self) -> None:
        ts = TimeSource(at(MON, 9), wall=lambda: datetime(1970, 1, 1, tzinfo=UTC))
        assert ts.now() is None
        ts.note_ntp_synchronized()
        assert ts.now() is not None

    def test_server_anchor_still_wins_over_ntp(self) -> None:
        ts = self.restored(at(MON, 20, 31))
        ts.note_ntp_synchronized()
        ts.note_server_time(at(TUE, 12))
        assert ts.now() == at(TUE, 12)

    def test_fails_open_without_ntp(self) -> None:
        ts = self.restored(at(MON, 20, 31))
        assert ts.now() is None
        settings = settings_with(mon=(time(20), time(7)))
        assert bedtime_status(settings, ts.now()) is NOT_BEDTIME

    def test_bedtime_enforced_after_ntp_confirms_a_restored_clock(self) -> None:
        ts = self.restored(at(MON, 20, 31))
        ts.note_ntp_synchronized()
        settings = settings_with(mon=(time(20), time(7)))
        assert bedtime_status(settings, ts.now()).active

    def test_watch_confirms_on_the_first_positive_probe(self) -> None:
        ts = self.restored(at(MON, 20, 31))
        answers = iter([False, False, True])
        run_ntp_watch(ts, probe=lambda: next(answers), interval=0.001)
        assert ts.ntp_synchronized

    def test_watch_stops_when_asked_without_confirming(self) -> None:
        ts = self.restored(at(MON, 20, 31))
        stop = threading.Event()

        def probe() -> bool:
            stop.set()  # the app is quitting
            return False

        run_ntp_watch(ts, probe=probe, interval=0.001, stop=stop)
        assert not ts.ntp_synchronized

    @pytest.mark.parametrize(
        ("stdout", "returncode", "expected"),
        [("yes\n", 0, True), ("no\n", 0, False), ("yes\n", 1, False), ("", 0, False)],
    )
    def test_read_ntp_synchronized_parses_timedatectl(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stdout: str,
        returncode: int,
        expected: bool,
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert read_ntp_synchronized() is expected
        assert calls == [["timedatectl", "show", "-p", "NTPSynchronized", "--value"]]

    @pytest.mark.parametrize(
        "error", [FileNotFoundError(), subprocess.TimeoutExpired("timedatectl", 5)]
    )
    def test_read_ntp_synchronized_is_false_without_timedatectl(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        def broken(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise error

        monkeypatch.setattr(subprocess, "run", broken)
        assert read_ntp_synchronized() is False


# ---------------------------------------------------------------------------
# Bedtime
# ---------------------------------------------------------------------------


class TestBedtimeStatus:
    overnight = settings_with(mon=(time(20), time(7)))

    def test_before_bedtime(self) -> None:
        assert bedtime_status(self.overnight, at(MON, 19, 59)) == NOT_BEDTIME

    def test_entering_bedtime(self) -> None:
        status = bedtime_status(self.overnight, at(MON, 20))
        assert status.active
        assert status.mode is BedtimeMode.SLEEP_SCREEN
        assert status.wake_at == at(TUE, 7).replace(tzinfo=None)

    def test_after_midnight_still_bedtime(self) -> None:
        # Monday's window runs into Tuesday morning, though Tuesday has none.
        assert bedtime_status(self.overnight, at(TUE, 3)).active

    def test_leaving_bedtime_at_wake(self) -> None:
        assert bedtime_status(self.overnight, at(TUE, 6, 59)).active
        assert bedtime_status(self.overnight, at(TUE, 7)) == NOT_BEDTIME

    def test_day_without_bedtime(self) -> None:
        assert bedtime_status(self.overnight, at(WED, 22)) == NOT_BEDTIME

    def test_same_day_window(self) -> None:
        nap = settings_with(sat=(time(13), time(15)))
        assert bedtime_status(nap, at(SAT, 12, 59)) == NOT_BEDTIME
        status = bedtime_status(nap, at(SAT, 14))
        assert status.wake_at == at(SAT, 15).replace(tzinfo=None)
        assert bedtime_status(nap, at(SAT, 15)) == NOT_BEDTIME

    def test_week_wraps_sunday_to_monday(self) -> None:
        s = settings_with(sun=(time(19, 30), time(6, 45)))
        assert bedtime_status(s, at(SUN, 23)).active
        # The next Monday's early morning is still Sunday's night.
        assert bedtime_status(s, at(SUN + 1, 6)).active

    @pytest.mark.parametrize(
        "mode", [BedtimeMode.AUDIOBOOKS_ONLY, BedtimeMode.SLEEP_SCREEN]
    )
    def test_each_mode_reported(self, mode: BedtimeMode) -> None:
        s = settings_with(mode, mon=(time(20), time(7)))
        assert bedtime_status(s, at(MON, 21)).mode is mode

    def test_off_mode_never_active(self) -> None:
        s = settings_with(BedtimeMode.OFF, mon=(time(20), time(7)))
        assert bedtime_status(s, at(MON, 21)) == NOT_BEDTIME

    def test_untrusted_clock_never_active(self) -> None:
        assert bedtime_status(self.overnight, None) == NOT_BEDTIME

    def test_uses_wall_clock_fields_not_utc(self) -> None:
        # 01:30 UTC Tuesday is 20:30 Monday at UTC-5: bedtime.
        utc = datetime(2026, 9, 29, 1, 30, tzinfo=UTC)
        assert bedtime_status(self.overnight, utc.astimezone(TZ)).active

    def test_status_active_flag(self) -> None:
        assert not BedtimeStatus().active
        assert BedtimeStatus(mode=BedtimeMode.AUDIOBOOKS_ONLY).active
