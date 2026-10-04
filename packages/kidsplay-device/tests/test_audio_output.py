"""The volume cap and bedtime fade, measured on the player's real output.

``test_app_controls`` proves what volume the player *asks* the mixer for. These
tests run the real mixer on SDL's ``disk`` audio driver, play a full-scale tone
and measure the PCM that comes out, so a UI sound, a second channel or a later
refactor that bypasses the ``set_volume`` calls still fails here. No audio
device is needed. The wall and monotonic clocks are fakes, so the fade is
driven by fake time while the audio itself runs in real time.
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pygame
import pytest

from kidsplay_device import app as app_module
from kidsplay_device.app import MusicPlayerApp
from kidsplay_device.config import DeviceConfig
from kidsplay_device.controls import FADE_SECONDS, UI_SOUND_LEVEL
from kidsplay_device.database import MediaRow
from kidsplay_models import BedtimeMode, ProfileSettings

from .disk_audio import (
    DiskAudio,
    MixerFormat,
    use_disk_driver,
    write_tone,
)
from .test_app_controls import (
    MONDAY_BEDTIME,
    MONDAY_NOON,
    Clock,
    FakeSyncClient,
    bedtime,
    press,
    start_app,
    store_settings,
)

TOLERANCE = 0.02
"""Allowed error on a measured peak, as a fraction of full scale."""


def track(name: str) -> MediaRow:
    return MediaRow(
        media_id=name,
        media_type="music",
        playlist_title="Tones",
        title=name,
        artist=None,
        duration_seconds=30,
        audio_path=f"audio/{name}.wav",
        photo_path=None,
        thumbnail_small_path=None,
        thumbnail_medium_path=None,
        thumbnail_large_path=None,
    )


TONE_A = track("a")
TONE_B = track("b")


@pytest.fixture
def cfg(tmp_path: Path) -> DeviceConfig:
    media = tmp_path / "media"
    (media / "audio").mkdir(parents=True)
    write_tone(media / "audio" / "a.wav", 25, hz=441)
    write_tone(media / "audio" / "b.wav", 25, hz=882)
    return DeviceConfig(
        server_url="http://test",
        device_id="dev",
        api_key="key",
        media_root=media,
        db_path=tmp_path / "device.db",
    )


@pytest.fixture
def out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cfg: DeviceConfig
) -> Iterator[Path]:
    """The disk driver's output file, with no sync thread and settings elsewhere."""
    del cfg  # the media must exist before the app starts
    path = tmp_path / "out.pcm"
    use_disk_driver(monkeypatch, path)
    monkeypatch.setattr(MusicPlayerApp, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(app_module, "SyncClient", FakeSyncClient)
    yield path
    pygame.quit()


def start(
    cfg: DeviceConfig, clock: Clock, out: Path
) -> tuple[MusicPlayerApp, DiskAudio]:
    """Start the app on the disk driver, and read the format the mixer got."""
    app = start_app(cfg, clock)
    return app, DiskAudio(out, MixerFormat.current())


def steady_peak(audio: DiskAudio, since: int) -> float:
    """Peak of the output after it has been audible for a moment."""
    audio.wait_for_sound(since)
    # Skip the buffer that was mixed before the last volume change took effect.
    start_at = audio.settle(0.25)
    audio.settle(0.3)
    return audio.peak(start_at)


class TestVolumeCapOnRealOutput:
    def test_mixer_format_is_read_not_assumed(
        self, cfg: DeviceConfig, out: Path
    ) -> None:
        _, audio = start(cfg, Clock(MONDAY_NOON), out)
        assert audio.format.frequency == 44100
        assert audio.format.channels == 2

    def test_output_peak_is_the_cap(self, cfg: DeviceConfig, out: Path) -> None:
        store_settings(cfg, ProfileSettings(max_volume=50))
        app, audio = start(cfg, Clock(MONDAY_NOON), out)
        begin = audio.mark()
        app.play_track(TONE_A, [TONE_A, TONE_B])
        peak = steady_peak(audio, begin)
        assert peak == pytest.approx(0.5, abs=TOLERANCE)
        # The whole run, from the first sample, never went above the cap.
        assert audio.peak() <= 0.5 + TOLERANCE

    def test_uncapped_profile_is_full_scale(self, cfg: DeviceConfig, out: Path) -> None:
        """The measurement really sees the level: with no cap it is ~1.0."""
        app, audio = start(cfg, Clock(MONDAY_NOON), out)
        begin = audio.mark()
        app.play_track(TONE_A, [TONE_A])
        assert steady_peak(audio, begin) > 0.95

    def test_cap_holds_through_track_changes(
        self, cfg: DeviceConfig, out: Path
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=50))
        app, audio = start(cfg, Clock(MONDAY_NOON), out)
        begin = audio.mark()
        for playing in (TONE_A, TONE_B, TONE_A, TONE_B):
            app.play_track(playing, [TONE_A, TONE_B])
            audio.settle(0.15)
        audio.wait_for_sound(begin)
        # Every sample of every track, including the starts: not one over.
        assert audio.peak(begin) <= 0.5 + TOLERANCE
        assert audio.peak(begin) > 0.5 - TOLERANCE

    def test_cap_holds_when_the_cap_is_lowered_by_a_sync(
        self, cfg: DeviceConfig, out: Path
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=80))
        app, audio = start(cfg, Clock(MONDAY_NOON), out)
        app.play_track(TONE_A, [TONE_A])
        audio.wait_for_sound(0)
        app._on_synced_settings(ProfileSettings(max_volume=30))
        app._tick_controls()
        assert steady_peak(audio, audio.mark()) == pytest.approx(0.3, abs=TOLERANCE)

    def test_volume_buttons_never_pass_the_cap(
        self, cfg: DeviceConfig, out: Path
    ) -> None:
        store_settings(cfg, ProfileSettings(max_volume=50, volume_buttons=True))
        app, audio = start(cfg, Clock(MONDAY_NOON), out)
        begin = audio.mark()
        app.play_track(TONE_A, [TONE_A], switch_to_play=False)
        for _ in range(12):
            press(app, pygame.K_EQUALS)
        # Only the music: UI sounds are covered by their own test.
        loud = steady_peak(audio, begin)
        assert loud == pytest.approx(0.5, abs=TOLERANCE)
        # Buttons still work below the cap: two steps down is 30%.
        press(app, pygame.K_MINUS)
        press(app, pygame.K_MINUS)
        quieter_from = audio.settle(0.25)
        audio.settle(0.3)
        assert audio.peak(quieter_from) < 0.5 - TOLERANCE

    @pytest.mark.parametrize("cap", [50, 100])
    def test_ui_sounds_over_music_stay_within_the_cap(
        self, cfg: DeviceConfig, out: Path, cap: int
    ) -> None:
        """Music is ducked under a UI sound, so the sum never passes the cap.

        Both are mixed, so a beep landing on a full-scale peak of the music
        would add up to ``cap * UI_SOUND_LEVEL`` to it (and clip at cap 100).
        The app ducks the music to ``cap - beep`` while the clip plays. The
        output peak must stay at the cap, *and* the beep must really be in it:
        a change that silently dropped the beep would fail the audibility
        check below.
        """
        store_settings(cfg, ProfileSettings(max_volume=cap))
        app, audio = start(cfg, Clock(MONDAY_NOON), out)
        app.play_track(TONE_A, [TONE_A], switch_to_play=False)
        audio.wait_for_sound(0)
        music_only = audio.settle(0.3)
        assert audio.peak(0) <= cap / 100 + TOLERANCE
        for _ in range(8):
            press(app, pygame.K_DOWN)  # moves the cursor: plays the "move" sound
            audio.settle(0.12)
        # No clipping and no overshoot, in the whole run and in the beep window.
        assert audio.peak(0) <= cap / 100 + TOLERANCE
        with_beeps = audio.peak(music_only)
        assert with_beeps <= cap / 100 + TOLERANCE
        # At a 100% cap the peak cannot exceed 1.0, so look for clipping: the
        # sum of music and beep flattening at full scale.
        assert audio.longest_clipped_run(music_only) <= 1
        # The beep is audible: the music alone is only cap * (1 - level) while
        # ducked, so reaching past that needs the beep on top of it.
        ducked_music = cap / 100 * (1 - UI_SOUND_LEVEL)
        assert with_beeps > ducked_music + TOLERANCE, with_beeps

    def _quietest_window(self, audio: DiskAudio, start: int) -> float:
        """Lowest peak over 20 ms windows of the output since ``start``."""
        fmt = audio.format
        step = fmt.frame_bytes * (fmt.frequency // 50)
        end = audio.mark()
        return min(audio.peak(at, at + step) for at in range(start, end - step, step))

    def test_button_sounds_off_output_is_the_music_alone(
        self, cfg: DeviceConfig, out: Path
    ) -> None:
        """Off: the output through the button presses is the undipped music.

        Control first, with sounds on: the music dips under each beep, so some
        20 ms window is clearly quieter than the cap. With sounds off no window
        is: the music keeps its full level the whole time.
        """
        cap = 50
        quietest: dict[bool, float] = {}
        for ui_sounds in (True, False):
            store_settings(cfg, ProfileSettings(max_volume=cap, ui_sounds=ui_sounds))
            app, audio = start(cfg, Clock(MONDAY_NOON), out)
            app.play_track(TONE_A, [TONE_A], switch_to_play=False)
            audio.wait_for_sound(0)
            audio.settle(0.3)
            begin = audio.mark()
            for _ in range(8):
                press(app, pygame.K_DOWN)
                audio.settle(0.12)
            audio.settle(0.3)
            quietest[ui_sounds] = self._quietest_window(audio, begin)
            assert audio.peak(begin) <= cap / 100 + TOLERANCE
            pygame.quit()
            out.unlink()
        ducked = cap / 100 * (1 - UI_SOUND_LEVEL)
        assert quietest[True] < ducked + TOLERANCE, quietest
        assert quietest[False] >= cap / 100 - TOLERANCE, quietest

    def test_ui_sounds_alone_are_capped(self, cfg: DeviceConfig, out: Path) -> None:
        """With no music, a UI sound is at most the cap times its own level."""
        store_settings(cfg, ProfileSettings(max_volume=50))
        app, audio = start(cfg, Clock(MONDAY_NOON), out)
        begin = audio.settle(0.3)
        for _ in range(4):
            press(app, pygame.K_DOWN)
            audio.settle(0.2)
        heard = audio.peak(begin)
        assert heard > 0.0, "the UI sound was not heard"
        assert heard <= 0.5 * UI_SOUND_LEVEL + TOLERANCE


class TestBedtimeFadeOnRealOutput:
    def test_fade_decreases_monotonically_to_silence(
        self, cfg: DeviceConfig, out: Path
    ) -> None:
        store_settings(cfg, bedtime(BedtimeMode.SLEEP_SCREEN, max_volume=50))
        clock = Clock(MONDAY_BEDTIME - timedelta(seconds=5))
        app, audio = start(cfg, clock, out)
        app.play_track(TONE_A, [TONE_A])
        audio.wait_for_sound(0)
        clock.advance(5.5)  # bedtime has begun; the fade starts on this tick
        app._tick_controls()
        assert app._fader is not None

        step = 0.5
        peaks: list[float] = []
        for _ in range(int(FADE_SECONDS / step) + 2):
            begin = audio.mark()
            clock.advance(step)
            app._tick_controls()
            audio.settle(0.09)
            peaks.append(audio.peak(begin))

        assert app._current_view == "sleep"
        assert peaks[0] == pytest.approx(0.5, abs=0.1)
        # Monotonic (the mixer works in buffers, so allow a sliver of ripple).
        for earlier, later in zip(peaks, peaks[1:], strict=False):
            assert later <= earlier + 0.01, peaks
        assert peaks[len(peaks) // 2] < peaks[0] - 0.1, peaks
        # ... and ends in silence.
        assert peaks[-1] < 0.01, peaks
