"""Tests for the parental controls wired into the player app.

The real ``MusicPlayerApp`` runs headless (SDL dummy video and audio). The
music stream's calls are recorded instead of decoding audio, which is how
these tests prove what volume the player asks for: the cap is enforced on
``pygame.mixer.music.set_volume`` itself. Wall and monotonic clocks are
fakes, so bedtime and the fade never depend on the real time. The sync
thread is replaced with a stub; one test runs real syncs against an
in-process server.
"""

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep

import aiosqlite
import pygame
import pytest
from httpx import ASGITransport, AsyncClient

from kidsplay_device import app as app_module
from kidsplay_device import i18n
from kidsplay_device.app import MusicPlayerApp
from kidsplay_device.config import DeviceConfig
from kidsplay_device.controls import (
    DUCK_TAIL_SECONDS,
    FADE_SECONDS,
    UI_SOUND_LEVEL,
    TimeSource,
)
from kidsplay_device.database import (
    ClockHeartbeat,
    MediaRow,
    get_clock_heartbeat,
    init_db,
    set_clock_heartbeat,
    set_profile_settings,
)
from kidsplay_device.sync import SyncClient
from kidsplay_device.views import SLEEP_BG, UiSound
from kidsplay_models import (
    BedtimeMode,
    BedtimeWindow,
    ProfileSettings,
    ThemeDefinition,
    Weekday,
)
from kidsplay_server.api.app import create_app
from kidsplay_server.auth import (
    create_admin_token,
    init_auth_db,
    set_initial_admin_password,
)
from kidsplay_server.database import configure_conn
from kidsplay_server.database import init_db as init_server_db

TZ = timezone(timedelta(hours=2))
MONDAY_NOON = datetime(2026, 9, 28, 12, 0, tzinfo=TZ)
MONDAY_BEDTIME = datetime(2026, 9, 28, 20, 0, tzinfo=TZ)
TUESDAY_WAKE = datetime(2026, 9, 29, 7, 0, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class MixerRecorder:
    """Stands in for ``pygame.mixer.music``'s playback calls."""

    def __init__(self) -> None:
        self.volumes: list[float] = []
        self.calls: list[str] = []

    def set_volume(self, value: float) -> None:
        self.volumes.append(value)
        self.calls.append("set_volume")

    def load(self, path: str) -> None:
        del path
        self.calls.append("load")

    def play(self) -> None:
        self.calls.append("play")

    def stop(self) -> None:
        self.calls.append("stop")

    def pause(self) -> None:
        self.calls.append("pause")

    def unpause(self) -> None:
        self.calls.append("unpause")

    def get_pos(self) -> int:
        return 0


class FakeSyncClient:
    """Replaces the sync thread; keeps the callbacks the app registers."""

    instances: list["FakeSyncClient"] = []

    def __init__(
        self,
        config: DeviceConfig,
        *,
        on_settings: Callable[[ProfileSettings], None] | None = None,
        on_server_time: Callable[[datetime], None] | None = None,
        on_theme: Callable[[ThemeDefinition | None], None] | None = None,
        on_identity: Callable[[bool], None] | None = None,
    ) -> None:
        del config
        self.on_settings = on_settings
        self.on_server_time = on_server_time
        self.on_theme = on_theme
        self.on_identity = on_identity
        FakeSyncClient.instances.append(self)

    def run_sync_loop(self) -> None:
        """No network in tests."""


class MixerIdle:
    """Stand-in for "is the mixer still playing a UI sound?".

    These tests drive time with :class:`Clock`, while the real mixer (SDL's
    dummy driver) plays the clips in real time, so the two would disagree. The
    mixer is idle unless a test says otherwise; the real-output side is covered
    by ``test_audio_output.py``.
    """

    def __init__(self) -> None:
        self.playing = False

    def __call__(self) -> bool:
        # Set on the class but not a function, so not bound: no ``self`` of the app.
        return self.playing


@pytest.fixture(autouse=True)
def ui_mixer(monkeypatch: pytest.MonkeyPatch) -> MixerIdle:
    """Make whether a UI sound is still playing follow the test, not real time."""
    idle = MixerIdle()
    monkeypatch.setattr(MusicPlayerApp, "_ui_sound_playing", idle)
    return idle


class Clock:
    """Fake wall clock plus fake monotonic clock, advanced together."""

    def __init__(self, wall: datetime) -> None:
        self.wall = wall
        self.mono = 1000.0

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def wall_now(self) -> datetime:
        return self.wall

    def mono_now(self) -> float:
        return self.mono


MUSIC = MediaRow(
    media_id="m1",
    media_type="music",
    playlist_title="Album",
    title="Song",
    artist=None,
    duration_seconds=100,
    audio_path="audio/song.mp3",
    photo_path=None,
    thumbnail_small_path=None,
    thumbnail_medium_path=None,
    thumbnail_large_path=None,
)
BOOK = MediaRow(
    media_id="b1",
    media_type="audiobook",
    playlist_title="Book",
    title="Chapter 1",
    artist=None,
    duration_seconds=100,
    audio_path="audio/chapter.mp3",
    photo_path=None,
    thumbnail_small_path=None,
    thumbnail_medium_path=None,
    thumbnail_large_path=None,
)


@pytest.fixture
def cfg(tmp_path: Path) -> DeviceConfig:
    media = tmp_path / "media"
    (media / "audio").mkdir(parents=True)
    for row in (MUSIC, BOOK):
        assert row.audio_path is not None
        (media / row.audio_path).write_bytes(b"not decoded: the mixer is recorded")
    return DeviceConfig(
        server_url="http://test",
        device_id="dev",
        api_key="key",
        media_root=media,
        db_path=tmp_path / "device.db",
    )


@pytest.fixture
def mixer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MixerRecorder]:
    """Headless pygame with a recorded music stream and no sync thread."""
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    monkeypatch.setattr(MusicPlayerApp, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(app_module, "SyncClient", FakeSyncClient)
    FakeSyncClient.instances.clear()
    rec = MixerRecorder()
    for name in ("set_volume", "load", "play", "stop", "pause", "unpause", "get_pos"):
        monkeypatch.setattr(pygame.mixer.music, name, getattr(rec, name))
    yield rec
    pygame.quit()


def store_settings(cfg: DeviceConfig, settings: ProfileSettings) -> None:
    """Persist settings as a previous sync would have."""
    conn = init_db(cfg.db_path)
    set_profile_settings(conn, settings)
    conn.commit()
    conn.close()


def start_app(
    cfg: DeviceConfig, clock: Clock, last_synced: datetime | None = None
) -> MusicPlayerApp:
    ts = TimeSource(last_synced, wall=clock.wall_now, mono=clock.mono_now, tz=TZ)
    app = MusicPlayerApp(cfg, time_source=ts, mono=clock.mono_now)
    app.initialize()
    return app


def press(app: MusicPlayerApp, key: int) -> None:
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=key))
    app._process_events()


def bedtime(mode: BedtimeMode, **extra: object) -> ProfileSettings:
    return ProfileSettings.model_validate(
        {
            "bedtime_mode": mode,
            "bedtime_schedule": {
                Weekday.MONDAY: BedtimeWindow(bedtime=time(20), wake=time(7))
            },
            **extra,
        }
    )


def tick_for(
    app: MusicPlayerApp, clock: Clock, seconds: float, step: float = 0.5
) -> None:
    """Run the per-frame controls for ``seconds`` of fake time."""
    elapsed = 0.0
    while elapsed < seconds:
        clock.advance(step)
        elapsed += step
        app._tick_controls()


# ---------------------------------------------------------------------------
# Volume cap
# ---------------------------------------------------------------------------


class TestVolumeCap:
    def test_default_full_volume(self, cfg: DeviceConfig, mixer: MixerRecorder) -> None:
        app = start_app(cfg, Clock(MONDAY_NOON))
        app.play_track(MUSIC, [MUSIC])
        assert mixer.volumes[-1] == 1.0

    def test_cap_applied_on_every_track_load(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=40))
        app = start_app(cfg, Clock(MONDAY_NOON))
        for _ in range(3):
            mixer.calls.clear()
            app.play_track(MUSIC, [MUSIC, BOOK])
            # The volume is (re)set after play() starts the new stream.
            assert mixer.calls[-3:] == ["load", "play", "set_volume"]
            assert mixer.volumes[-1] == pytest.approx(0.4)

    def test_never_above_cap(self, cfg: DeviceConfig, mixer: MixerRecorder) -> None:
        """No sequence of loads, button presses, syncs and fades exceeds the cap."""
        store_settings(
            cfg, bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=30, volume_buttons=True)
        )
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=30))
        app = start_app(cfg, clock)
        app.play_track(MUSIC, [MUSIC, BOOK])
        for _ in range(15):
            press(app, pygame.K_EQUALS)
        app.play_track(BOOK, [MUSIC, BOOK])
        assert mixer.volumes
        assert max(mixer.volumes) <= 0.30 + 1e-9
        # A sync lowers the cap mid-track.
        mark = len(mixer.volumes)
        app._on_synced_settings(
            bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=20, volume_buttons=True)
        )
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(0.20)
        for _ in range(5):
            press(app, pygame.K_PLUS)
        app.play_track(MUSIC, [MUSIC, BOOK])
        # Bedtime starts: the fade runs through set_volume too.
        tick_for(app, clock, 30 + FADE_SECONDS + 2)
        assert app._current_view == "sleep"
        after_sync = mixer.volumes[mark:]
        assert len(after_sync) > 10
        assert max(after_sync) <= 0.20 + 1e-9

    def test_ui_sounds_attenuated_by_cap(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=40))
        app = start_app(cfg, Clock(MONDAY_NOON))
        assert app._sounds, "UI sounds should load with the dummy audio driver"
        for clip in app._sounds.values():
            assert clip.get_volume() == pytest.approx(0.5 * 0.4, abs=0.01)

    def test_synced_settings_applied_on_next_frame(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app = start_app(cfg, Clock(MONDAY_NOON))
        app.play_track(MUSIC, [MUSIC])
        # The app registered its callback with the sync client.
        sync = FakeSyncClient.instances[-1]
        assert sync.on_settings is not None
        sync.on_settings(ProfileSettings(max_volume=25))
        assert mixer.volumes[-1] == 1.0  # not applied from the sync thread
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(0.25)

    def test_cap_persists_offline_across_reboot(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=55))
        start_app(cfg, Clock(MONDAY_NOON))
        pygame.quit()
        mixer.volumes.clear()
        # "Reboot" with no network: a fresh app reads the stored settings.
        app = start_app(cfg, Clock(MONDAY_NOON))
        assert mixer.volumes and mixer.volumes[0] == pytest.approx(0.55)
        app.play_track(MUSIC, [MUSIC])
        assert mixer.volumes[-1] == pytest.approx(0.55)


class TestVolumeButtons:
    _ALL_KEYS = (
        pygame.K_EQUALS,
        pygame.K_PLUS,
        pygame.K_KP_PLUS,
        pygame.K_MINUS,
        pygame.K_KP_MINUS,
        pygame.K_UP,
        pygame.K_DOWN,
        pygame.K_LEFT,
        pygame.K_RIGHT,
        pygame.K_RETURN,
        pygame.K_b,
        pygame.K_x,
        pygame.K_y,
    )

    def test_off_by_default_no_input_changes_volume(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=60))
        clock = Clock(MONDAY_NOON)
        app = start_app(cfg, clock)
        app.play_track(MUSIC, [MUSIC])
        level = app.volume.level
        for key in self._ALL_KEYS:
            press(app, key)
            app.switch_view("home")
        for button in range(8):
            pygame.event.post(
                pygame.event.Event(pygame.JOYBUTTONDOWN, button=button, joy=0)
            )
            app._process_events()
            app.switch_view("home")
        assert mixer.volumes
        # The cap, or the cap ducked under a UI sound: never a button's doing.
        ducked = 0.6 * (1 - UI_SOUND_LEVEL)
        assert all(
            v == pytest.approx(0.6) or v == pytest.approx(ducked) for v in mixer.volumes
        )
        clock.advance(1.0)  # the UI sounds are over
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(0.6)
        assert app.volume.level == level
        assert app.volume.effective_percent() == 60

    def test_on_buttons_capped(self, cfg: DeviceConfig, mixer: MixerRecorder) -> None:
        store_settings(cfg, ProfileSettings(max_volume=50, volume_buttons=True))
        clock = Clock(MONDAY_NOON)
        app = start_app(cfg, clock)
        app.play_track(MUSIC, [MUSIC])
        assert mixer.volumes[-1] == pytest.approx(0.5)

        def settled() -> float:
            """The music volume once the button's UI sound has ended."""
            clock.advance(1.0)
            app._tick_controls()
            return mixer.volumes[-1]

        press(app, pygame.K_MINUS)
        press(app, pygame.K_MINUS)
        assert settled() == pytest.approx(0.3)
        for _ in range(10):
            press(app, pygame.K_EQUALS)
            assert max(mixer.volumes) <= 0.5 + 1e-9
            assert settled() <= 0.5 + 1e-9
        assert mixer.volumes[-1] == pytest.approx(0.5)

    def test_gamepad_shoulders(self, cfg: DeviceConfig, mixer: MixerRecorder) -> None:
        store_settings(cfg, ProfileSettings(max_volume=80, volume_buttons=True))
        app = start_app(cfg, Clock(MONDAY_NOON))
        pygame.event.post(pygame.event.Event(pygame.JOYBUTTONDOWN, button=4, joy=0))
        app._process_events()
        assert app.volume.effective_percent() == 70
        pygame.event.post(pygame.event.Event(pygame.JOYBUTTONDOWN, button=5, joy=0))
        app._process_events()
        assert app.volume.effective_percent() == 80

    def test_level_remembered_across_reboot(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=90, volume_buttons=True))
        app = start_app(cfg, Clock(MONDAY_NOON))
        for _ in range(4):
            press(app, pygame.K_MINUS)
        assert app.volume.effective_percent() == 50
        pygame.quit()
        app = start_app(cfg, Clock(MONDAY_NOON))
        assert app.volume.effective_percent() == 50
        app.play_track(MUSIC, [MUSIC])
        assert mixer.volumes[-1] == pytest.approx(0.5)

    def test_overlay_renders(self, cfg: DeviceConfig, mixer: MixerRecorder) -> None:
        store_settings(cfg, ProfileSettings(max_volume=70, volume_buttons=True))
        app = start_app(cfg, Clock(MONDAY_NOON))
        press(app, pygame.K_MINUS)
        app._render()  # draws the volume overlay without error


class TestVolumeCapFeedback:
    """Pressing up at the cap shows the overlay: the bar full at the cap mark."""

    def overlay_up(self, app: MusicPlayerApp) -> bool:
        return app._mono() < app._volume_overlay_until

    def test_up_at_the_cap_shows_the_overlay(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=60, volume_buttons=True))
        app = start_app(cfg, Clock(MONDAY_NOON))
        assert app.volume.effective_percent() == 60
        assert not self.overlay_up(app)
        press(app, pygame.K_EQUALS)  # already at the cap: nothing to change
        assert app.volume.effective_percent() == 60
        assert self.overlay_up(app)
        app._render()  # draws it without error

    def test_up_at_the_cap_leaves_the_volume_alone(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=60, volume_buttons=True))
        app = start_app(cfg, Clock(MONDAY_NOON))
        app.play_track(MUSIC, [MUSIC])
        before = (app.volume.effective_percent(), len(mixer.volumes))
        assert app.change_volume(1) is False  # nothing changed...
        assert self.overlay_up(app)  # ...but the child sees the limit
        assert (app.volume.effective_percent(), len(mixer.volumes)) == before

    def test_down_at_zero_shows_the_overlay_too(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=20, volume_buttons=True))
        app = start_app(cfg, Clock(MONDAY_NOON))
        press(app, pygame.K_MINUS)
        press(app, pygame.K_MINUS)
        assert app.volume.effective_percent() == 0
        app._volume_overlay_until = float("-inf")
        press(app, pygame.K_MINUS)
        assert self.overlay_up(app)

    def test_buttons_off_show_nothing(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=60, volume_buttons=False))
        app = start_app(cfg, Clock(MONDAY_NOON))
        press(app, pygame.K_EQUALS)
        assert not self.overlay_up(app)


class SpyClip:
    """Stands in for a loaded UI sound clip: records plays, makes no noise."""

    def __init__(self, real: pygame.mixer.Sound) -> None:
        self.plays = 0
        self._length = real.get_length()
        self._volume = real.get_volume()

    def play(self) -> None:
        self.plays += 1

    def get_length(self) -> float:
        return self._length

    def get_volume(self) -> float:
        return self._volume

    def set_volume(self, volume: float) -> None:
        self._volume = volume

    def get_num_channels(self) -> int:
        return 0


def spy_on_sounds(app: MusicPlayerApp) -> dict[UiSound, SpyClip]:
    """Swap every loaded UI clip for a spy, so plays can be counted."""
    spies = {sound: SpyClip(clip) for sound, clip in app._sounds.items()}
    app._sounds = dict(spies)  # ty: ignore[invalid-assignment]  # duck-typed Sound
    return spies


class TestUiSoundsSetting:
    """``ui_sounds`` off: no button sound, and no duck with nothing to duck for."""

    CAP = 0.6

    def test_on_by_default_plays_and_ducks(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=60))
        app = start_app(cfg, Clock(MONDAY_NOON))
        spies = spy_on_sounds(app)
        app.play_track(MUSIC, [MUSIC])
        app._play_ui_sound(UiSound.MOVE)
        assert spies[UiSound.MOVE].plays == 1
        assert mixer.volumes[-1] == pytest.approx(self.CAP * (1 - UI_SOUND_LEVEL))

    def test_off_plays_no_clip_and_does_not_duck(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=60, ui_sounds=False))
        app = start_app(cfg, Clock(MONDAY_NOON))
        spies = spy_on_sounds(app)
        app.play_track(MUSIC, [MUSIC])
        assert mixer.volumes[-1] == pytest.approx(self.CAP)
        calls = len(mixer.calls)
        for sound in UiSound:
            app._play_ui_sound(sound)
        assert all(spy.plays == 0 for spy in spies.values())
        assert not app._ducked
        assert mixer.volumes[-1] == pytest.approx(self.CAP)
        assert len(mixer.calls) == calls  # the music volume was not touched

    def test_off_real_button_presses_are_silent(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=60, ui_sounds=False))
        app = start_app(cfg, Clock(MONDAY_NOON))
        spies = spy_on_sounds(app)
        app.play_track(MUSIC, [MUSIC], switch_to_play=False)
        for key in (pygame.K_DOWN, pygame.K_UP, pygame.K_RETURN, pygame.K_ESCAPE):
            press(app, key)
        assert all(spy.plays == 0 for spy in spies.values())
        assert min(mixer.volumes[1:]) == pytest.approx(self.CAP)  # never dipped

    def test_synced_setting_takes_effect_without_a_restart(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=60))
        app = start_app(cfg, Clock(MONDAY_NOON))
        app.play_track(MUSIC, [MUSIC], switch_to_play=False)

        def plays_after(key: int) -> int:
            # A sync reloads the clips, so spy on the current ones each time.
            spies = spy_on_sounds(app)
            press(app, key)
            return sum(spy.plays for spy in spies.values())

        assert plays_after(pygame.K_DOWN) == 1
        # A sync turns them off: applied on the UI thread's next tick.
        app._on_synced_settings(ProfileSettings(max_volume=60, ui_sounds=False))
        app._tick_controls()
        assert plays_after(pygame.K_UP) == 0
        # And a later sync turns them back on.
        app._on_synced_settings(ProfileSettings(max_volume=60))
        app._tick_controls()
        assert plays_after(pygame.K_DOWN) == 1

    def test_off_silences_a_themes_custom_sounds_too(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        """Themed and bundled clips share one table and one play path."""
        store_settings(cfg, ProfileSettings(ui_sounds=False))
        app = start_app(cfg, Clock(MONDAY_NOON))
        spies = spy_on_sounds(app)
        app._play_ui_sound(UiSound.OPEN)
        assert spies[UiSound.OPEN].plays == 0


class TestUiSoundDucking:
    """Music dips under a UI sound so music + beep never pass the cap."""

    CAP = 0.6

    def _start(
        self, cfg: DeviceConfig, extra: ProfileSettings | None = None
    ) -> tuple[MusicPlayerApp, Clock, float]:
        store_settings(cfg, extra or ProfileSettings(max_volume=60))
        clock = Clock(MONDAY_NOON)
        app = start_app(cfg, clock)
        length = app._sounds[UiSound.MOVE].get_length()
        assert 0 < length < 5
        return app, clock, length

    def test_beep_over_music_ducks_then_restores(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app, clock, length = self._start(cfg)
        app.play_track(MUSIC, [MUSIC])
        assert mixer.volumes[-1] == pytest.approx(self.CAP)
        app._play_ui_sound(UiSound.MOVE)
        ducked = self.CAP * (1 - UI_SOUND_LEVEL)
        assert mixer.volumes[-1] == pytest.approx(ducked)
        # Music + beep is the cap; the beep itself keeps its level.
        beep = app._sounds[UiSound.MOVE].get_volume()
        assert beep == pytest.approx(UI_SOUND_LEVEL * self.CAP, abs=0.01)
        assert mixer.volumes[-1] + beep <= self.CAP + 0.01
        # Still ducked while the clip plays (and through the output-latency tail).
        clock.advance(length + DUCK_TAIL_SECONDS / 2)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(ducked)
        clock.advance(DUCK_TAIL_SECONDS)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(self.CAP)
        # Restored once, not re-set every frame.
        count = len(mixer.volumes)
        app._tick_controls()
        assert len(mixer.volumes) == count

    def test_duck_holds_until_the_mixer_has_finished_the_beep(
        self, cfg: DeviceConfig, mixer: MixerRecorder, ui_mixer: MixerIdle
    ) -> None:
        """A busy CPU can leave the clip playing past its estimated end; the
        music must stay ducked until the mixer is done with it."""
        app, clock, length = self._start(cfg)
        app.play_track(MUSIC, [MUSIC])
        ducked = self.CAP * (1 - UI_SOUND_LEVEL)
        app._play_ui_sound(UiSound.MOVE)
        ui_mixer.playing = True
        clock.advance(length + DUCK_TAIL_SECONDS + 1.0)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(ducked)
        ui_mixer.playing = False
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(self.CAP)

    def test_overlapping_beeps_extend_the_duck_without_compounding(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app, clock, length = self._start(cfg)
        app.play_track(MUSIC, [MUSIC])
        ducked = self.CAP * (1 - UI_SOUND_LEVEL)
        app._play_ui_sound(UiSound.MOVE)
        clock.advance(length / 2)
        app._play_ui_sound(UiSound.MOVE)
        assert mixer.volumes[-1] == pytest.approx(ducked)  # not ducked twice
        # Past the first beep's end: the second still holds the duck.
        clock.advance(length / 2 + DUCK_TAIL_SECONDS + 0.01)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(ducked)
        clock.advance(length / 2 + DUCK_TAIL_SECONDS)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(self.CAP)
        assert min(mixer.volumes[1:]) == pytest.approx(ducked)

    def test_synced_cap_change_while_ducked(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app, clock, length = self._start(cfg)
        app.play_track(MUSIC, [MUSIC])
        app._play_ui_sound(UiSound.MOVE)
        app._on_synced_settings(ProfileSettings(max_volume=30))
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(0.3 * (1 - UI_SOUND_LEVEL))
        clock.advance(length + DUCK_TAIL_SECONDS + 0.01)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(0.3)

    def test_volume_button_while_ducked(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app, clock, length = self._start(
            cfg, ProfileSettings(max_volume=60, volume_buttons=True)
        )
        app.play_track(MUSIC, [MUSIC])
        app._play_ui_sound(UiSound.MOVE)
        press(app, pygame.K_MINUS)  # 60 -> 50, and its own UI sound
        assert mixer.volumes[-1] == pytest.approx(0.5 * (1 - UI_SOUND_LEVEL))
        clock.advance(length + DUCK_TAIL_SECONDS + 0.01)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(0.5)

    def test_track_change_while_ducked(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app, clock, length = self._start(cfg)
        app.play_track(MUSIC, [MUSIC, BOOK])
        app._play_ui_sound(UiSound.MOVE)
        mixer.calls.clear()
        app.play_track(BOOK, [MUSIC, BOOK])
        assert mixer.calls[-3:] == ["load", "play", "set_volume"]
        # The new stream starts ducked, so it never peaks over the beep.
        assert mixer.volumes[-1] == pytest.approx(self.CAP * (1 - UI_SOUND_LEVEL))
        clock.advance(length + DUCK_TAIL_SECONDS + 0.01)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(self.CAP)

    def test_duck_with_bedtime_fade(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=60))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=1))
        app = start_app(cfg, clock)
        length = app._sounds[UiSound.MOVE].get_length()
        app.play_track(MUSIC, [MUSIC])
        clock.advance(1.5)
        app._tick_controls()  # bedtime: the fade starts
        assert app._fader is not None
        ducked = self.CAP * (1 - UI_SOUND_LEVEL)
        # Early in the fade the fader is above the duck: the duck wins.
        app._play_ui_sound(UiSound.MOVE)
        assert mixer.volumes[-1] == pytest.approx(ducked)
        # Late in the fade the fader is below the duck: it is never raised.
        clock.advance(FADE_SECONDS * 0.8)
        app._tick_controls()
        faded = mixer.volumes[-1]
        assert faded < ducked
        app._play_ui_sound(UiSound.MOVE)
        assert mixer.volumes[-1] <= faded + 1e-9
        # After the duck ends the fade carries on from where it is, not from
        # the pre-duck level.
        clock.advance(length + DUCK_TAIL_SECONDS + 0.01)
        app._tick_controls()
        assert mixer.volumes[-1] < faded
        assert max(mixer.volumes[-3:]) <= self.CAP + 1e-9

    def test_beep_without_music_leaves_the_beep_level_alone(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app, clock, length = self._start(cfg)
        app._play_ui_sound(UiSound.MOVE)
        beep = app._sounds[UiSound.MOVE].get_volume()
        assert beep == pytest.approx(UI_SOUND_LEVEL * self.CAP, abs=0.01)
        clock.advance(length + DUCK_TAIL_SECONDS + 0.01)
        app._tick_controls()
        assert mixer.volumes[-1] == pytest.approx(self.CAP)


# ---------------------------------------------------------------------------
# Bedtime
# ---------------------------------------------------------------------------


class TestBedtimeSleepScreen:
    def test_enter_during_playback_fades_then_sleeps(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=80))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=5))
        app = start_app(cfg, clock)
        app.play_track(MUSIC, [MUSIC])
        assert app._current_view == "play"
        mixer.volumes.clear()
        mixer.calls.clear()

        tick_for(app, clock, 5.5)  # bedtime starts
        assert app._fader is not None
        assert "stop" not in mixer.calls  # no abrupt cut
        assert app._current_view == "play"

        tick_for(app, clock, FADE_SECONDS / 2)
        assert "stop" not in mixer.calls
        assert 0.0 < mixer.volumes[-1] < 0.8

        tick_for(app, clock, FADE_SECONDS / 2 + 1)
        assert app._fader is None
        assert "stop" in mixer.calls
        assert app._current_view == "sleep"
        assert not app.playback.is_playing
        assert app.playback.current_track is None
        # Volume went down steadily from the cap to silence, then stopped.
        faded = mixer.volumes[: mixer.volumes.index(0.0) + 1]
        assert faded == sorted(faded, reverse=True)
        assert faded[0] <= 0.8
        assert faded[-1] == 0.0
        # The fade took about FADE_SECONDS.
        assert len(faded) >= int(FADE_SECONDS / 0.5) - 1

    def test_escape_does_not_quit_from_the_sleep_screen(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        """Every other button does nothing at bedtime; Escape used to quit."""
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        app = start_app(cfg, Clock(MONDAY_BEDTIME + timedelta(hours=1)))
        assert app._current_view == "sleep"
        app._running = True
        press(app, pygame.K_ESCAPE)
        assert app._running is True
        assert app._current_view == "sleep"

    def test_escape_does_not_quit_during_the_fade(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=5))
        app = start_app(cfg, clock)
        app.play_track(MUSIC, [MUSIC])
        tick_for(app, clock, 5.5)
        assert app._fader is not None
        app._running = True
        press(app, pygame.K_ESCAPE)
        assert app._running is True

    def test_escape_still_quits_outside_bedtime(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        app = start_app(cfg, Clock(MONDAY_NOON))
        app._running = True
        press(app, pygame.K_ESCAPE)
        assert app._running is False

    def test_sleep_screen_ignores_buttons(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        app = start_app(cfg, Clock(MONDAY_BEDTIME + timedelta(hours=1)))
        assert app._current_view == "sleep"
        mixer.calls.clear()
        for key in (pygame.K_RETURN, pygame.K_x, pygame.K_b, pygame.K_UP):
            press(app, key)
        assert app._current_view == "sleep"
        assert mixer.calls == []
        app.play_track(MUSIC, [MUSIC])
        assert "play" not in mixer.calls
        app.switch_view("music")
        assert app._current_view == "sleep"
        app._render()

    def test_show_sleep_screen_is_the_supported_way_in_outside_bedtime(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        """``switch_view("sleep")`` stays refused; ``show_sleep_screen`` enters."""
        del mixer
        app = start_app(cfg, Clock(MONDAY_BEDTIME - timedelta(hours=5)))
        app.switch_view("sleep")
        assert app._current_view == "home"
        app.show_sleep_screen(datetime(2026, 9, 29, 7, 30))
        assert app._current_view == "sleep"
        assert app.bedtime_wake_at == datetime(2026, 9, 29, 7, 30)
        assert not app.view_allowed("music")
        app._render()
        screen = pygame.display.get_surface()
        assert screen is not None
        assert screen.get_at((3, 3))[:3] == SLEEP_BG

    def test_enter_when_idle_goes_straight_to_sleep(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=2))
        app = start_app(cfg, clock)
        assert app._current_view == "home"
        tick_for(app, clock, 3)
        assert app._fader is None
        assert app._current_view == "sleep"
        assert app.bedtime_wake_at == TUESDAY_WAKE.replace(tzinfo=None)

    def test_leaving_bedtime_returns_home(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        clock = Clock(TUESDAY_WAKE - timedelta(seconds=3))
        app = start_app(cfg, clock)
        assert app._current_view == "sleep"
        tick_for(app, clock, 4)
        assert app._current_view == "home"
        app.play_track(MUSIC, [MUSIC])
        assert "play" in mixer.calls

    def test_bedtime_ending_mid_fade_restores_volume(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=70))
        clock = Clock(MONDAY_BEDTIME)
        app = start_app(cfg, clock)
        assert app._current_view == "sleep"
        # Leave bedtime by a sync turning the mode off, start a track, turn
        # bedtime back on, and off again mid-fade.
        app._on_synced_settings(ProfileSettings(max_volume=70))
        tick_for(app, clock, 1.5)
        assert app._current_view == "home"
        app.play_track(MUSIC, [MUSIC])
        app._on_synced_settings(bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=70))
        tick_for(app, clock, 3)
        assert app._fader is not None
        app._on_synced_settings(ProfileSettings(max_volume=70))
        tick_for(app, clock, 1)
        assert app._fader is None
        assert mixer.volumes[-1] == pytest.approx(0.7)
        assert app.playback.is_playing

    def test_cap_lowered_mid_fade_is_respected(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=80))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=1))
        app = start_app(cfg, clock)
        app.play_track(MUSIC, [MUSIC])
        tick_for(app, clock, 2)
        assert app._fader is not None
        mark = len(mixer.volumes)
        app._on_synced_settings(bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=10))
        tick_for(app, clock, FADE_SECONDS)
        assert app._current_view == "sleep"
        assert max(mixer.volumes[mark:]) <= 0.10 + 1e-9

    def test_fade_dims_screen(self, cfg: DeviceConfig, mixer: MixerRecorder) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=1))
        app = start_app(cfg, clock)
        app.play_track(MUSIC, [MUSIC])
        app._render()
        screen = pygame.display.get_surface()
        assert screen is not None
        before = pygame.transform.average_color(screen)
        tick_for(app, clock, 1 + FADE_SECONDS * 0.8)
        app._render()
        after = pygame.transform.average_color(screen)
        assert sum(after[:3]) < sum(before[:3])


class TestBedtimeAudiobooksOnly:
    def test_music_fades_and_audiobooks_shown(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.AUDIOBOOKS_ONLY))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=1))
        app = start_app(cfg, clock)
        app.play_track(MUSIC, [MUSIC])
        tick_for(app, clock, 1.5)
        assert app._fader is not None
        tick_for(app, clock, FADE_SECONDS + 1)
        assert "stop" in mixer.calls
        assert app._current_view == "audiobooks"
        assert not app.playback.is_playing
        assert app.playback.current_track is None

    def test_audiobook_keeps_playing(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.AUDIOBOOKS_ONLY, max_volume=60))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=1))
        app = start_app(cfg, clock)
        app.play_track(BOOK, [BOOK])
        mixer.calls.clear()
        tick_for(app, clock, FADE_SECONDS + 3)
        assert app._fader is None
        assert "stop" not in mixer.calls
        assert app.playback.is_playing
        assert app._current_view == "play"
        assert all(v == pytest.approx(0.6) for v in mixer.volumes[-3:])

    def test_only_audiobooks_allowed(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.AUDIOBOOKS_ONLY))
        app = start_app(cfg, Clock(MONDAY_BEDTIME + timedelta(hours=2)))
        for name in ("music", "photos", "settings", "sleep"):
            assert not app.view_allowed(name)
            app.switch_view(name)
            assert app._current_view != name
        for name in ("home", "audiobooks", "play"):
            assert app.view_allowed(name)
        mixer.calls.clear()
        app.play_track(MUSIC, [MUSIC])
        assert "play" not in mixer.calls
        app.play_track(BOOK, [BOOK])
        assert "play" in mixer.calls
        app.switch_view("home")
        app._render()  # home with locked tiles

    def test_leaving_restores_all_views(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.AUDIOBOOKS_ONLY))
        clock = Clock(TUESDAY_WAKE - timedelta(seconds=1))
        app = start_app(cfg, clock)
        assert not app.view_allowed("music")
        tick_for(app, clock, 2)
        assert app.view_allowed("music")
        app.switch_view("music")
        assert app._current_view == "music"


class TestBedtimeClock:
    def test_untrusted_clock_not_enforced(
        self, cfg: DeviceConfig, mixer: MixerRecorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A clock earlier than the last sync must never lock the kid out."""
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        # The clock reads Monday 21:00 (bedtime!) but the device last synced
        # on Tuesday afternoon: the clock went backwards, so it is wrong.
        clock = Clock(MONDAY_BEDTIME + timedelta(hours=1))
        with caplog.at_level("WARNING"):
            app = start_app(cfg, clock, last_synced=TUESDAY_WAKE + timedelta(hours=7))
        assert app._current_view == "home"
        assert "Clock untrusted" in caplog.text
        app.play_track(MUSIC, [MUSIC])
        tick_for(app, clock, FADE_SECONDS + 2)
        assert app._fader is None
        assert app.playback.is_playing

    def test_server_time_after_sync_enforces(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        clock = Clock(datetime(1970, 1, 1, tzinfo=UTC))
        app = start_app(cfg, clock, last_synced=MONDAY_NOON)
        assert app._current_view == "home"
        sync = FakeSyncClient.instances[-1]
        assert sync.on_server_time is not None
        sync.on_server_time(MONDAY_BEDTIME + timedelta(minutes=5))
        tick_for(app, clock, 1.5)
        assert app._current_view == "sleep"

    def test_plausible_clock_enforced_offline(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        app = start_app(
            cfg, Clock(MONDAY_BEDTIME + timedelta(hours=1)), last_synced=MONDAY_NOON
        )
        assert app._current_view == "sleep"


# ---------------------------------------------------------------------------
# End to end: web UI/API change -> sync -> device, then offline reboot
# ---------------------------------------------------------------------------


async def _admin_token(db_path: Path) -> str:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_server_db(conn)
        await init_auth_db(conn)
        await set_initial_admin_password(conn, "device-tests-password")
        token = await create_admin_token(conn, "device-tests")
        await conn.commit()
    return token.token


class TestEndToEnd:
    async def test_max_volume_reaches_device_and_survives_reboot(
        self, cfg: DeviceConfig, mixer: MixerRecorder, tmp_path: Path
    ) -> None:
        server = create_app(tmp_path / "server.db", tmp_path / "server_media")
        token = await _admin_token(server.state.db_path)
        transport = ASGITransport(app=server)
        async with (
            AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={"Authorization": f"Bearer {token}"},
            ) as admin,
            AsyncClient(transport=transport, base_url="http://test") as device_http,
        ):
            profile = (
                await admin.post("/api/v1/profiles", json={"name": "Leo"})
            ).json()
            device = (
                await admin.post(
                    "/api/v1/devices", json={"name": "GB", "profile_id": profile["id"]}
                )
            ).json()
            cfg.device_id = device["id"]
            cfg.api_key = device["api_key"]

            app = start_app(cfg, Clock(MONDAY_NOON))
            app.play_track(MUSIC, [MUSIC])
            assert mixer.volumes[-1] == 1.0

            r = await admin.put(
                f"/api/v1/profiles/{profile['id']}/settings", json={"max_volume": 35}
            )
            assert r.status_code == 200
            sync = SyncClient(
                cfg, http_client=device_http, on_settings=app._on_synced_settings
            )
            await sync.sync()
            app._tick_controls()
            assert mixer.volumes[-1] == pytest.approx(0.35)

        # Reboot with the server gone: the stored cap still applies.
        pygame.quit()
        mixer.volumes.clear()
        app = start_app(cfg, Clock(MONDAY_NOON))
        app.play_track(MUSIC, [MUSIC])
        assert mixer.volumes
        assert max(mixer.volumes) == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# A bad persisted setting must never stop the player
# ---------------------------------------------------------------------------


class TestBadBedtimeSettingFailsOpen:
    def aware_settings(self) -> ProfileSettings:
        """Settings a pre-fix server could have delivered: an aware time."""
        window = BedtimeWindow.model_construct(
            bedtime=time(20, tzinfo=UTC), wake=time(7)
        )
        return ProfileSettings.model_construct(
            version=1,
            max_volume=100,
            volume_buttons=False,
            bedtime_mode=BedtimeMode.SLEEP_SCREEN,
            bedtime_schedule={Weekday.MONDAY: window},
        )

    def test_tick_survives_and_logs_once(
        self,
        cfg: DeviceConfig,
        mixer: MixerRecorder,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        clock = Clock(MONDAY_BEDTIME + timedelta(hours=1))
        app = start_app(cfg, clock)
        app.volume.settings = self.aware_settings()
        with caplog.at_level(logging.ERROR, logger="kidsplay_device.app"):
            tick_for(app, clock, 10)  # no TypeError, many checks
        assert app._bedtime.mode is BedtimeMode.OFF
        assert app._current_view != "sleep"
        errors = [r for r in caplog.records if "Bedtime evaluation failed" in r.message]
        assert len(errors) == 1

    def test_recovers_when_settings_are_replaced(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        clock = Clock(MONDAY_BEDTIME + timedelta(hours=1))
        app = start_app(cfg, clock)
        app.volume.settings = self.aware_settings()
        tick_for(app, clock, 2)
        app._on_synced_settings(bedtime(BedtimeMode.SLEEP_SCREEN))
        tick_for(app, clock, 2)
        assert app._bedtime.mode is BedtimeMode.SLEEP_SCREEN


# ---------------------------------------------------------------------------
# Wall-clock heartbeat and the restored-clock lockout
# ---------------------------------------------------------------------------


def stored_heartbeat(cfg: DeviceConfig) -> ClockHeartbeat | None:
    conn = init_db(cfg.db_path)
    try:
        return get_clock_heartbeat(conn)
    finally:
        conn.close()


def start_app_from_db(cfg: DeviceConfig, clock: Clock, boot_id: str) -> MusicPlayerApp:
    """Start like ``initialize()`` does in production: heartbeat from the DB."""
    conn = init_db(cfg.db_path)
    heartbeat = get_clock_heartbeat(conn)
    conn.close()
    ts = TimeSource(
        None,
        heartbeat=heartbeat,
        boot_id=boot_id,
        wall=clock.wall_now,
        mono=clock.mono_now,
        tz=TZ,
    )
    app = MusicPlayerApp(cfg, time_source=ts, mono=clock.mono_now)
    app.initialize()
    return app


class TestClockHeartbeat:
    def test_written_at_start_then_every_five_minutes(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        clock = Clock(MONDAY_NOON)
        app = start_app(cfg, clock)
        app._tick_controls()
        beat = stored_heartbeat(cfg)
        assert beat is not None
        assert beat.wall == MONDAY_NOON
        tick_for(app, clock, 200)
        beat = stored_heartbeat(cfg)
        assert beat is not None
        assert beat.wall == MONDAY_NOON  # not rewritten within five minutes
        tick_for(app, clock, 110)
        beat = stored_heartbeat(cfg)
        assert beat is not None
        assert beat.wall > MONDAY_NOON + timedelta(seconds=290)

    def test_written_on_clean_shutdown(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        clock = Clock(MONDAY_NOON)
        app = start_app(cfg, clock)
        app._tick_controls()
        clock.advance(120)
        app._shutdown()
        beat = stored_heartbeat(cfg)
        assert beat is not None
        assert beat.wall == MONDAY_NOON + timedelta(seconds=120)

    def test_restored_clock_boot_does_not_enforce_bedtime(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        """Off at 20:30 with heartbeat, on offline at noon reading 20:31."""
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        conn = init_db(cfg.db_path)
        set_clock_heartbeat(
            conn, ClockHeartbeat(MONDAY_BEDTIME + timedelta(minutes=30), "old", True)
        )
        conn.commit()
        conn.close()
        clock = Clock(MONDAY_BEDTIME + timedelta(minutes=31))
        app = start_app_from_db(cfg, clock, boot_id="new")
        tick_for(app, clock, 5)
        assert not app._bedtime.active
        assert app._current_view != "sleep"
        # Still not enforced after a player restart in the same boot.
        pygame.quit()
        app = start_app_from_db(cfg, clock, boot_id="new")
        tick_for(app, clock, 5)
        assert not app._bedtime.active

    def test_enforced_once_synced_this_boot(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        conn = init_db(cfg.db_path)
        set_clock_heartbeat(
            conn, ClockHeartbeat(MONDAY_BEDTIME + timedelta(minutes=30), "old", True)
        )
        conn.commit()
        conn.close()
        clock = Clock(MONDAY_BEDTIME + timedelta(minutes=31))
        app = start_app_from_db(cfg, clock, boot_id="new")
        tick_for(app, clock, 3)
        assert not app._bedtime.active
        # The server confirms it really is 20:32 on Monday.
        app._require_time_source().note_server_time(clock.wall)
        tick_for(app, clock, 3)
        assert app._bedtime.mode is BedtimeMode.SLEEP_SCREEN

    def test_enforced_once_ntp_synchronized(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        """All-in-one: no server sync can confirm the clock, NTP does."""
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        conn = init_db(cfg.db_path)
        set_clock_heartbeat(
            conn, ClockHeartbeat(MONDAY_BEDTIME + timedelta(minutes=30), "old", True)
        )
        conn.commit()
        conn.close()
        clock = Clock(MONDAY_BEDTIME + timedelta(minutes=31))
        app = start_app_from_db(cfg, clock, boot_id="new")
        tick_for(app, clock, 3)
        assert not app._bedtime.active
        assert app._current_view != "sleep"

        app._require_time_source().note_ntp_synchronized()
        tick_for(app, clock, 3)
        assert app._bedtime.mode is BedtimeMode.SLEEP_SCREEN
        assert app._current_view == "sleep"
        # The trust is remembered for the next start of the same boot.
        app._write_heartbeat()
        beat = stored_heartbeat(cfg)
        assert beat is not None and beat.trusted

    def test_the_ntp_watch_runs_in_the_background_and_ends_with_the_app(
        self,
        cfg: DeviceConfig,
        mixer: MixerRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probes: list[int] = []
        answers = iter([False, False, True])

        def probe() -> bool:
            probes.append(1)
            return next(answers, True)

        monkeypatch.setattr("kidsplay_device.app.read_ntp_synchronized", probe)
        monkeypatch.setattr("kidsplay_device.app.NTP_POLL_INTERVAL", 0.01)
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        clock = Clock(MONDAY_BEDTIME + timedelta(minutes=31))
        conn = init_db(cfg.db_path)
        set_clock_heartbeat(
            conn, ClockHeartbeat(MONDAY_BEDTIME + timedelta(minutes=30), "old", True)
        )
        conn.commit()
        conn.close()
        app = start_app_from_db(cfg, clock, boot_id="new")
        deadline = monotonic() + 5
        while not app._require_time_source().ntp_synchronized:
            assert monotonic() < deadline, "the watch never confirmed"
            sleep(0.01)
        assert len(probes) == 3  # polled until the first yes, then stopped
        app._shutdown()
        assert app._ntp_stop.is_set()

    def test_real_elapsed_time_follows_normal_rules(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        """Clock far past the heartbeat (NTP set it): bedtime applies."""
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN))
        conn = init_db(cfg.db_path)
        set_clock_heartbeat(
            conn, ClockHeartbeat(MONDAY_NOON - timedelta(hours=5), "old", True)
        )
        conn.commit()
        conn.close()
        clock = Clock(MONDAY_BEDTIME + timedelta(hours=1))
        app = start_app_from_db(cfg, clock, boot_id="new")
        tick_for(app, clock, 3)
        assert app._bedtime.mode is BedtimeMode.SLEEP_SCREEN


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_language() -> Iterator[None]:
    before = i18n.current_language()
    yield
    i18n.activate(before)


def _home_frame() -> bytes:
    surface = pygame.display.get_surface()
    assert surface is not None
    return pygame.image.tobytes(surface, "RGB")


def test_existing_profile_without_a_language_keeps_spanish(
    cfg: DeviceConfig, mixer: MixerRecorder, restore_language: None
) -> None:
    """An install upgraded from before language was a setting stays Spanish."""
    store_settings(cfg, ProfileSettings(max_volume=70))  # no language stored
    start_app(cfg, Clock(MONDAY_NOON))
    assert i18n.current_language() == "es"
    assert i18n._("Music") == "Música"


def test_device_that_never_synced_is_spanish(
    cfg: DeviceConfig, mixer: MixerRecorder, restore_language: None
) -> None:
    start_app(cfg, Clock(MONDAY_NOON))
    assert i18n.current_language() == "es"


def test_profile_language_setting_is_used(
    cfg: DeviceConfig, mixer: MixerRecorder, restore_language: None
) -> None:
    store_settings(cfg, ProfileSettings(language="en"))
    start_app(cfg, Clock(MONDAY_NOON))
    assert i18n.current_language() == "en"
    assert i18n._("Music") == "Music"


def test_config_override_beats_the_profile(
    cfg: DeviceConfig, mixer: MixerRecorder, restore_language: None
) -> None:
    store_settings(cfg, ProfileSettings(language="en"))
    cfg.language = "es"
    start_app(cfg, Clock(MONDAY_NOON))
    assert i18n.current_language() == "es"


def test_synced_language_change_switches_the_screen_live(
    cfg: DeviceConfig, mixer: MixerRecorder, restore_language: None
) -> None:
    store_settings(cfg, ProfileSettings(language="en"))
    app = start_app(cfg, Clock(MONDAY_NOON))
    app._render()
    english = _home_frame()

    sync = FakeSyncClient.instances[-1]
    assert sync.on_settings is not None
    sync.on_settings(ProfileSettings(language="es"))
    app._tick_controls()
    app._render()
    spanish = _home_frame()

    assert i18n.current_language() == "es"
    assert spanish != english


def test_override_survives_a_synced_change(
    cfg: DeviceConfig, mixer: MixerRecorder, restore_language: None
) -> None:
    cfg.language = "en"
    app = start_app(cfg, Clock(MONDAY_NOON))
    sync = FakeSyncClient.instances[-1]
    assert sync.on_settings is not None
    sync.on_settings(ProfileSettings(language="es"))
    app._tick_controls()
    assert i18n.current_language() == "en"


def test_unsupported_synced_language_does_not_break_the_screen(
    cfg: DeviceConfig, mixer: MixerRecorder, restore_language: None
) -> None:
    app = start_app(cfg, Clock(MONDAY_NOON))
    sync = FakeSyncClient.instances[-1]
    assert sync.on_settings is not None
    sync.on_settings(ProfileSettings.model_validate({"language": "fr"}))
    app._tick_controls()
    app._render()
    assert i18n.current_language() == "en"


async def test_language_round_trips_profile_to_manifest_to_device(
    cfg: DeviceConfig, mixer: MixerRecorder, tmp_path: Path, restore_language: None
) -> None:
    """Profile setting -> sync manifest -> device database -> on-screen language."""
    server = create_app(tmp_path / "server.db", tmp_path / "server_media")
    token = await _admin_token(server.state.db_path)
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
        cfg.device_id = device["id"]
        cfg.api_key = device["api_key"]
        settings_url = f"/api/v1/profiles/{profile['id']}/settings"

        app = start_app(cfg, Clock(MONDAY_NOON))
        assert i18n.current_language() == "es"  # never synced: legacy Spanish

        async def sync() -> None:
            await SyncClient(
                cfg, http_client=device_http, on_settings=app._on_synced_settings
            ).sync()
            app._tick_controls()

        # A new profile is English by default.
        await sync()
        assert i18n.current_language() == "en"

        # The parent picks Spanish for the child.
        r = await admin.put(settings_url, json={"language": "es"})
        assert r.status_code == 200
        await sync()
        assert i18n.current_language() == "es"

        # And back; then a profile with no stored language (as before this
        # setting existed) shows Spanish again.
        await admin.put(settings_url, json={"language": "en"})
        await sync()
        assert i18n.current_language() == "en"
        # (a PUT that omits the key keeps the stored language)
        await admin.put(settings_url, json={"max_volume": 80})
        await sync()
        assert i18n.current_language() == "en"
        await admin.put(settings_url, json={"language": None})
        await sync()
        assert i18n.current_language() == "es"

        # The synced value survives a reboot with the server gone.
        await admin.put(settings_url, json={"language": "en"})
        await sync()

    pygame.quit()
    start_app(cfg, Clock(MONDAY_NOON))
    assert i18n.current_language() == "en"


# ---------------------------------------------------------------------------
# run_frame: the public single-step hook
# ---------------------------------------------------------------------------


class TestRunFrame:
    def test_handles_input_draws_and_shows_the_given_position(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app = start_app(cfg, Clock(MONDAY_NOON))
        app.play_track(MUSIC, [MUSIC])
        screen = pygame.display.get_surface()
        assert screen is not None
        app.run_frame(0.0)
        before = pygame.image.tobytes(screen, "RGB")

        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_SPACE))
        app.run_frame(42.0)

        assert app.playback.current_position == 42.0
        assert app.playback.is_paused, "the posted key press was handled"
        assert pygame.image.tobytes(screen, "RGB") != before

    def test_keeps_the_position_when_none_is_given(
        self, cfg: DeviceConfig, mixer: MixerRecorder
    ) -> None:
        app = start_app(cfg, Clock(MONDAY_NOON))
        app.playback.current_position = 7.0
        app.run_frame()
        assert app.playback.current_position == 7.0

    def test_needs_initialize(self, cfg: DeviceConfig) -> None:
        app = MusicPlayerApp(cfg)
        with pytest.raises(RuntimeError, match="initialize"):
            app.run_frame()
