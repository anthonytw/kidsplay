"""Run the pairing screen as its own small pygame app.

``kidsplay-player`` calls :func:`run_pairing` when there is no ``config.json``.
It shows the screen from ``pairing_screen.py`` until the parent approves (the
config is then written) or the window is closed.

There is no config yet, so the screen size and input profile come from the
command line (``--width``, ``--height``, ``--input-profile``) or are the
defaults (640×480, ``gpi2``), and the theme is the default one. The paired
config is written with the defaults for those, and ``kidsplay-player`` then
applies the same command-line overrides to it.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import pygame

from .buttons import Button
from .config import DEFAULT_CONFIG_PATH, DeviceConfig
from .discovery import ServerDiscovery
from .fonts import load_text_fonts
from .input_profiles import DEFAULT_INPUT_PROFILE, InputMapper, resolve_profile
from .layout import REFERENCE_HEIGHT, REFERENCE_WIDTH, Layout
from .pairing import PairingSession
from .pairing_screen import PairingScreen
from .theme import DEFAULT_THEME

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

FPS = 30
DONE_SECONDS = 1.5
CODE_FONT_SIZE = 88
"""Size of the code on the pairing screen, on the 640×480 design."""


def pairing_fonts(layout: Layout) -> dict[str, pygame.font.Font]:
    """The fonts of the pairing screen, sized for the screen.

    Args:
        layout: The screen's layout.

    Returns:
        The player's text fonts plus ``huge`` for the code.
    """
    fonts = load_text_fonts(layout.scale)
    fonts["huge"] = pygame.font.Font(None, layout.px(CODE_FONT_SIZE))
    return fonts


def run_pairing(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    width: int | None = None,
    height: int | None = None,
    input_profile: str | None = None,
    discover: bool = True,
    start_url: str | None = None,
    fullscreen: bool | None = None,
) -> DeviceConfig | None:
    """Show the pairing screen until this device is paired.

    Args:
        config_path: Where ``config.json`` will be written.
        width: Screen width (``--width``); 640 when not given, as there is no
            config to say yet.
        height: Screen height (``--height``); 480 when not given.
        input_profile: Input profile name (``--input-profile``); the default
            profile when not given.
        discover: Look for servers on the network (mDNS).
        start_url: Server to pair with straight away, skipping the picker.
        fullscreen: Fill the screen with no title bar (``--fullscreen``);
            windowed when not given, as there is no config to say yet.

    Returns:
        The new config, or None if the window was closed first.
    """
    layout = Layout.for_size(width or REFERENCE_WIDTH, height or REFERENCE_HEIGHT)
    mapper = InputMapper(resolve_profile(input_profile or DEFAULT_INPUT_PROFILE))
    pygame.init()
    screen = pygame.display.set_mode(
        (layout.width, layout.height), pygame.FULLSCREEN if fullscreen else 0
    )
    pygame.display.set_caption("KidsPlay")
    pygame.mouse.set_visible(False)
    # Keep references alive so SDL does not close the devices.
    joysticks = [
        pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())
    ]
    discovery = ServerDiscovery()
    if discover:
        discovery.start()
    session = PairingSession(
        config_path,
        device_name=os.uname().nodename,
        width=layout.width,
        height=layout.height,
    )
    ui = PairingScreen(
        session,
        pairing_fonts(layout),
        lambda: discovery.servers,
        theme=DEFAULT_THEME,
        start_url=start_url,
        layout=layout,
    )
    clock = pygame.time.Clock()
    finished_at: float | None = None
    try:
        while True:
            clock.tick(FPS)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                button = mapper.map_event(event)
                if button is Button.TERMINATE:
                    return None
                if button is not None:
                    ui.handle_input(button)
            ui.update()
            ui.draw(screen)
            pygame.display.flip()
            if ui.done:
                now = pygame.time.get_ticks() / 1000
                finished_at = finished_at if finished_at is not None else now
                if now - finished_at >= DONE_SECONDS:
                    return DeviceConfig.load(config_path)
    finally:
        session.cancel()
        discovery.stop()
        joysticks.clear()
