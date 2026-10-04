"""Tests for the headless device capture (``demo/device_shots.py``).

``capture`` is driven with a stub app over a real pygame surface (SDL dummy
driver), so no player, database or audio device is needed.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pygame
import pytest
from PIL import Image, ImageSequence

from demo import device_shots
from demo.device_shots import Step


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> Iterator[pygame.Surface]:
    """A small dummy-driver display surface."""
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    pygame.display.init()
    try:
        yield pygame.display.set_mode((32, 24))
    finally:
        pygame.display.quit()


def _stub_app(screen: pygame.Surface) -> MagicMock:
    """An app whose frame paints a colour that changes on every key press."""
    app = MagicMock()
    presses: list[int] = []

    def run_frame(position: float | None = None) -> None:
        presses.extend(e.key for e in pygame.event.get(pygame.KEYDOWN))
        if position is not None:
            app.playback.current_position = position
        screen.fill((10 * len(presses), 0, 0))

    app.run_frame.side_effect = run_frame
    return app


def test_steps_start_home_and_name_the_readme_images() -> None:
    names = {s.save_as for s in device_shots.STEPS if s.save_as}
    assert device_shots.STEPS[0] == Step(None, 1400, "device-home")
    assert {"device-home", "device-play", "device-tracks"} <= names


def test_capture_posts_keys_and_snapshots_each_step(screen: pygame.Surface) -> None:
    app = _stub_app(screen)
    steps = (
        Step(None, 100),
        Step(pygame.K_DOWN, 100),
        Step(pygame.K_UP, 100, None, 5.0),
    )
    frames = device_shots.capture(app, steps)

    assert [step for step, _ in frames] == list(steps)
    assert [img.getpixel((0, 0)) for _, img in frames] == [
        (0, 0, 0),
        (10, 0, 0),
        (20, 0, 0),
    ]
    assert app.playback.current_position == 5.0


def test_capture_uses_only_the_players_public_hook() -> None:
    """A player refactor of its private methods must not break the screenshots."""
    source = Path(device_shots.__file__).read_text()
    assert "app._" not in source
    assert "run_frame" in source


def test_save_outputs_writes_named_pngs_and_gif(tmp_path: Path) -> None:
    frames = [
        (Step(None, 300, "first"), Image.new("RGB", (8, 6), (255, 0, 0))),
        (Step(None, 500), Image.new("RGB", (8, 6), (0, 0, 255))),
    ]
    device_shots.save_outputs(frames, tmp_path / "out")

    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "device-walkthrough.gif",
        "first.png",
    ]
    gif = Image.open(tmp_path / "out" / "device-walkthrough.gif")
    durations = [frame.info["duration"] for frame in ImageSequence.Iterator(gif)]
    assert durations == [300, 500]


def test_save_outputs_leaves_unchanged_files_alone(tmp_path: Path) -> None:
    frames = [
        (Step(None, 300, "first"), Image.new("RGB", (8, 6), (255, 0, 0))),
        (Step(None, 500), Image.new("RGB", (8, 6), (0, 0, 255))),
    ]
    out = tmp_path / "out"
    device_shots.save_outputs(frames, out)
    for path in out.iterdir():
        os.utime(path, (1_000_000, 1_000_000))

    device_shots.save_outputs(frames, out)
    assert {p.stat().st_mtime_ns for p in out.iterdir()} == {1_000_000 * 10**9}

    frames[0][1].putpixel((0, 0), (0, 255, 0))
    device_shots.save_outputs(frames, out)
    assert (out / "first.png").stat().st_mtime > 1_000_000
    assert (out / "device-walkthrough.gif").stat().st_mtime > 1_000_000


def test_main_requires_an_output_dir() -> None:
    with pytest.raises(SystemExit):
        device_shots.main([])


def test_main_captures_with_the_loaded_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = MagicMock()
    monkeypatch.setattr(device_shots.DeviceConfig, "load", MagicMock())
    monkeypatch.setattr(device_shots, "MusicPlayerApp", MagicMock(return_value=app))
    capture = MagicMock(return_value=[])
    save = MagicMock()
    monkeypatch.setattr(device_shots, "capture", capture)
    monkeypatch.setattr(device_shots, "save_outputs", save)
    monkeypatch.setattr(device_shots.pygame.mixer.music, "set_endevent", MagicMock())
    monkeypatch.setattr(device_shots.pygame.mixer.music, "stop", MagicMock())
    monkeypatch.setattr(device_shots.pygame, "quit", MagicMock())

    device_shots.main([str(tmp_path)])

    app.initialize.assert_called_once()
    capture.assert_called_once_with(app, device_shots.STEPS)
    save.assert_called_once_with([], tmp_path)
