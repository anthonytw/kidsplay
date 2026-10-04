"""Drive the real device player headlessly and capture screens.

Run by ``demo.screenshots`` in a child process, with ``HOME`` pointing at the
demo device's home directory (so the player reads the demo config and never
touches a real ``~/.kidsplay``) and SDL's dummy video and audio drivers.

The player is built exactly as ``kidsplay-player`` builds it. Instead of its
real-time main loop, this module posts key presses and renders one frame per
step, so every capture is independent of timing. Playback position is set
explicitly for the same reason.

Usage (normally via ``demo.screenshots``)::

    HOME=<device-home> SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \\
        python -m demo.device_shots <out-dir>
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pygame
from PIL import Image

from demo.imagefiles import save_gif_if_changed, save_png_if_changed
from kidsplay_device.app import MusicPlayerApp
from kidsplay_device.config import DeviceConfig


@dataclass(frozen=True)
class Step:
    """One step of the scripted walkthrough.

    Attributes:
        key: pygame key to press first, or ``None`` to just render.
        hold_ms: How long the frame stays up in the GIF.
        save_as: File name (without extension) to also save as a PNG.
        position: Playback position to show, in seconds (``None`` = unchanged).
    """

    key: int | None
    hold_ms: int
    save_as: str | None = None
    position: float | None = None


# The walkthrough: home → music → a track → play view → photos → a photo →
# audiobooks. Keys are the keyboard stand-ins for the handheld's buttons.
STEPS: tuple[Step, ...] = (
    Step(None, 1400, "device-home"),
    Step(pygame.K_RETURN, 900, "device-music"),
    Step(pygame.K_DOWN, 700),
    Step(pygame.K_RETURN, 900, "device-tracks"),
    Step(pygame.K_DOWN, 600),
    Step(pygame.K_DOWN, 600),
    Step(pygame.K_RETURN, 600, None, 0.0),
    Step(None, 600, None, 4.0),
    Step(None, 600, None, 8.0),
    Step(None, 1600, "device-play", 12.0),
    Step(pygame.K_BACKSPACE, 600),
    Step(pygame.K_BACKSPACE, 500),
    Step(pygame.K_BACKSPACE, 700),
    Step(pygame.K_DOWN, 700),
    Step(pygame.K_RETURN, 800, "device-photos"),
    Step(pygame.K_RETURN, 900, "device-photo-grid"),
    Step(pygame.K_RIGHT, 600),
    Step(pygame.K_RETURN, 1400, "device-photo"),
    Step(pygame.K_BACKSPACE, 500),
    Step(pygame.K_BACKSPACE, 500),
    Step(pygame.K_BACKSPACE, 700),
    Step(pygame.K_UP, 600),
    Step(pygame.K_RIGHT, 700),
    Step(pygame.K_RETURN, 800),
    Step(pygame.K_RETURN, 1400, "device-audiobooks"),
)


def _snapshot(screen: pygame.Surface) -> Image.Image:
    """Copy the pygame screen into a Pillow image."""
    size = screen.get_size()
    return Image.frombytes("RGB", size, pygame.image.tobytes(screen, "RGB"))


def capture(
    app: MusicPlayerApp, steps: tuple[Step, ...]
) -> list[tuple[Step, Image.Image]]:
    """Run the walkthrough against an initialised app.

    Args:
        app: A player on which ``initialize()`` has been called.
        steps: The scripted steps.

    Returns:
        Each step with the frame rendered after it.
    """
    screen = pygame.display.get_surface()
    if screen is None:
        raise RuntimeError("the app has no display; call initialize() first")
    frames: list[tuple[Step, Image.Image]] = []
    for step in steps:
        if step.key is not None:
            pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=step.key))
        # The player's own event handling and rendering, minus the clock.
        app.run_frame(step.position)
        frames.append((step, _snapshot(screen)))
    return frames


def save_outputs(frames: list[tuple[Step, Image.Image]], out_dir: Path) -> None:
    """Write the named PNGs and the walkthrough GIF, skipping unchanged files.

    Args:
        frames: Output of :func:`capture`.
        out_dir: Destination directory (``docs/images/``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for step, img in frames:
        if step.save_as:
            name = f"{step.save_as}.png"
            note = "" if save_png_if_changed(img, out_dir / name) else " (unchanged)"
            print(f"  {name}{note}")

    changed = save_gif_if_changed(
        [img for _, img in frames],
        [step.hold_ms for step, _ in frames],
        out_dir / "device-walkthrough.gif",
    )
    print(f"  device-walkthrough.gif{'' if changed else ' (unchanged)'}")


def main(argv: list[str] | None = None) -> None:
    """Capture the device screens into the directory given on the command line.

    Args:
        argv: ``[out_dir]``; ``sys.argv[1:]`` if None.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m demo.device_shots <out-dir>")
    # SDL reads these when pygame.init() runs inside app.initialize().
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    config = DeviceConfig.load()  # ~/.kidsplay/config.json under the demo HOME
    app = MusicPlayerApp(config)
    app.initialize()
    try:
        # Stop the track-ended event: frames must not depend on audio timing.
        pygame.mixer.music.set_endevent(pygame.NOEVENT)
        save_outputs(capture(app, STEPS), Path(args[0]))
    finally:
        pygame.mixer.music.stop()
        pygame.quit()


if __name__ == "__main__":
    main()
