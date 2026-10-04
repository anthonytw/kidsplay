"""Measure what the player really outputs, through SDL's ``disk`` audio driver.

``SDL_AUDIODRIVER=disk`` writes the final mixed PCM to the file named by
``SDL_DISKAUDIOFILE`` instead of a sound card, in real time. The tests read it
back to see the output level, which is what the volume cap promises to bound,
whatever code path (music stream, UI sound channel, fade) produced the sound.

Not a test module: helpers only.
"""

import array
import math
import os
import struct
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import pygame
import pytest

TONE_HZ = 441
"""441 Hz at 44.1 kHz is exactly 100 samples per period, so the peak is exact."""

FULL_SCALE = 32767
"""Peak of the generated tone: the loudest a 16-bit sample can be."""


def write_tone(path: Path, seconds: float, hz: int = TONE_HZ) -> None:
    """Write a mono, full-scale sine tone as a 16-bit WAV file.

    Args:
        path: Destination file.
        seconds: Length of the tone.
        hz: Frequency; a divisor of 44100 keeps whole periods.
    """
    rate = 44100
    period = rate // hz
    one = [
        round(FULL_SCALE * math.sin(2 * math.pi * i / period)) for i in range(period)
    ]
    frames = (one * (rate * int(seconds * 10) // (10 * period) + 1))[
        : int(rate * seconds)
    ]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{len(frames)}h", *frames))


@dataclass(frozen=True)
class MixerFormat:
    """The mixer's actual output format, as ``pygame.mixer.get_init`` reports.

    Attributes:
        frequency: Sample rate in Hz.
        channels: Interleaved channel count.
    """

    frequency: int
    channels: int

    @classmethod
    def current(cls) -> "MixerFormat":
        """Read the format from the running mixer, not from what was asked for.

        Returns:
            The mixer's frequency and channels.

        Raises:
            AssertionError: If the mixer is not initialised, or does not output
                signed 16-bit samples (the only format these helpers decode).
        """
        init = pygame.mixer.get_init()
        assert init is not None, "the mixer is not initialised"
        frequency, size, channels = init
        assert size == -16, f"unsupported sample format {size}; expected signed 16"
        return cls(frequency, channels)

    @property
    def frame_bytes(self) -> int:
        """Bytes per (all-channel) sample frame."""
        return 2 * self.channels


class DiskAudio:
    """The PCM the disk driver has written so far.

    Args:
        path: The ``SDL_DISKAUDIOFILE`` the driver writes to.
        fmt: The mixer's format, from :meth:`MixerFormat.current`.
    """

    def __init__(self, path: Path, fmt: MixerFormat) -> None:
        self.path = path
        self.format = fmt

    def mark(self) -> int:
        """A position in the output, in bytes, for later :meth:`peak` windows.

        Returns:
            The current length of the output, rounded down to a whole frame.
        """
        size = self.path.stat().st_size if self.path.exists() else 0
        return size - size % self.format.frame_bytes

    def peak(self, start: int = 0, end: int | None = None) -> float:
        """Loudest sample in a window of the output.

        Args:
            start: First byte (from :meth:`mark`).
            end: End byte, exclusive; the current end if ``None``.

        Returns:
            The peak as a fraction of full scale (0.0 to 1.0).
        """
        stop = self.mark() if end is None else end
        with self.path.open("rb") as f:
            f.seek(start)
            data = f.read(stop - start)
        data = data[: len(data) - len(data) % 2]
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder == "big":
            samples.byteswap()  # the driver writes native-endian s16
        if not samples:
            return 0.0
        return max(abs(s) for s in samples) / 32768

    def longest_clipped_run(self, start: int = 0) -> int:
        """Longest run of consecutive frames at digital full scale (channel 0).

        A pure full-scale tone touches full scale for one frame per half
        period. A longer run is two streams summing past 1.0 and being
        clipped, which a peak measurement cannot show at a 100% cap.

        Args:
            start: First byte (from :meth:`mark`).

        Returns:
            The run length in frames, 0 if nothing reached full scale.
        """
        with self.path.open("rb") as f:
            f.seek(start)
            data = f.read()
        data = data[: len(data) - len(data) % 2]
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder == "big":
            samples.byteswap()
        longest = run = 0
        for value in samples[:: self.format.channels]:
            run = run + 1 if abs(value) >= FULL_SCALE else 0
            longest = max(longest, run)
        return longest

    def wait_for_sound(self, since: int, timeout: float = 5.0) -> None:
        """Block until something louder than silence has been output.

        Args:
            since: Byte position to look from.
            timeout: Seconds to wait.

        Raises:
            AssertionError: If nothing audible arrives in time.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.peak(since) > 0.0:
                return
            time.sleep(0.02)
        raise AssertionError("no audible output from the disk audio driver")

    def settle(self, seconds: float) -> int:
        """Let the driver run for a while and return the new output position.

        Args:
            seconds: Real time to wait; the disk driver runs in real time.

        Returns:
            :meth:`mark` after waiting.
        """
        time.sleep(seconds)
        return self.mark()


def use_disk_driver(monkeypatch: pytest.MonkeyPatch, out: Path) -> None:
    """Point SDL at the disk audio driver (call before ``pygame.init``).

    Args:
        monkeypatch: The test's monkeypatch, so the environment is restored.
        out: File the driver writes raw PCM to.
    """
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SDL_AUDIODRIVER", "disk")
    monkeypatch.setenv("SDL_DISKAUDIOFILE", os.fspath(out))
