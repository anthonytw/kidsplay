"""Shared fixtures for the headless player tests."""

from collections.abc import Iterator

import pygame
import pytest

from kidsplay_device import i18n
from kidsplay_device.layout import DEFAULT_LAYOUT
from kidsplay_device.theme import DEFAULT_THEME
from kidsplay_device.views import apply_layout, apply_theme, set_backgrounds


@pytest.fixture(autouse=True)
def _reset_view_globals() -> Iterator[None]:
    """The views keep the palette, layout and backgrounds in module globals."""
    yield
    apply_theme(DEFAULT_THEME)
    apply_layout(DEFAULT_LAYOUT)
    set_backgrounds(None, None)
    i18n.activate("en")


@pytest.fixture(autouse=True)
def _no_real_ntp_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never ask the machine running the tests whether its clock is NTP-synced.

    The app polls ``timedatectl`` in the background; on a developer machine or
    CI runner with a synchronized clock that would make the tests' fake clocks
    trusted. Tests that want NTP call ``note_ntp_synchronized`` themselves.
    """
    monkeypatch.setattr("kidsplay_device.app.read_ntp_synchronized", lambda: False)


@pytest.fixture
def headless(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """SDL dummy drivers, and pygame shut down again afterwards."""
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    yield
    pygame.quit()
