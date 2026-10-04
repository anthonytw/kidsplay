"""The pairing screen at every supported resolution, in both languages, and
driven through the input profiles.

Like ``test_screens.py`` for the player: each screen of the pairing flow is
drawn onto a ``SpySurface`` at 320×240, 640×480, 800×480 and 1280×720, saved as
a PNG (in ``$KIDSPLAY_SHOTS_DIR`` if set), and the test fails if anything was
blitted past the surface's edge or the frame is blank. Navigation is checked
with the ``gpi2`` and ``keyboard`` input profiles, the way the pairing app
maps events.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pygame
import pytest
from PIL import Image

from kidsplay_device import i18n
from kidsplay_device.buttons import Button
from kidsplay_device.discovery import FoundServer
from kidsplay_device.input_profiles import InputMapper, resolve_profile
from kidsplay_device.keyboard import Layout as KeyLayout
from kidsplay_device.layout import Layout
from kidsplay_device.pairing import PairingProblem, PairingState, Stage
from kidsplay_device.pairing_app import pairing_fonts
from kidsplay_device.pairing_screen import PairingScreen, Step

from .test_screens import SIZES, SpySurface, shots_dir

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

SERVER = "http://192.168.1.20:8000"
LONG_SERVER = "http://kidsplay-server-with-a-long-name.example.local:8000"


class FakeSession:
    def __init__(self) -> None:
        self.state = PairingState()
        self.started: list[str] = []

    def start(self, server_url: str) -> None:
        self.started.append(server_url)
        self.state = PairingState(Stage.CONNECTING, server_url)

    def cancel(self) -> None: ...


@pytest.fixture(autouse=True)
def _pygame() -> Iterator[None]:
    pygame.font.init()
    yield
    i18n.activate("en")


def make(
    size: tuple[int, int], preset: str | None = None
) -> tuple[PairingScreen, FakeSession]:
    layout = Layout.for_size(*size)
    session = FakeSession()
    screen = PairingScreen(
        session,
        pairing_fonts(layout),
        lambda: [FoundServer("KidsPlay", SERVER), FoundServer("Other", LONG_SERVER)],
        start_url=preset,
        layout=layout,
    )
    return screen, session


def scenes(
    screen: PairingScreen, session: FakeSession
) -> Iterator[tuple[str, Callable[[], None]]]:
    """Each screen of the flow, as (name, put the screen in that state)."""

    def picker() -> None:
        screen.step = Step.PICK_SERVER

    def keyboard(layout: KeyLayout) -> Callable[[], None]:
        def show() -> None:
            screen.step = Step.ENTER_ADDRESS
            screen._keyboard.layout = layout
            screen._keyboard.text = "192.168.100.200:8000"

        return show

    def pairing(state: PairingState) -> Callable[[], None]:
        def show() -> None:
            screen.step = Step.PAIRING
            session.state = state

        return show

    yield "picker", picker
    yield "keyboard-numeric", keyboard(KeyLayout.NUMERIC)
    yield "keyboard-letters", keyboard(KeyLayout.LETTERS)
    yield "connecting", pairing(PairingState(Stage.CONNECTING, LONG_SERVER))
    yield (
        "code",
        pairing(PairingState(Stage.WAITING, LONG_SERVER, "ABCD-2345", None, 583)),
    )
    yield (
        "expired",
        pairing(PairingState(Stage.FAILED, SERVER, problem=PairingProblem.EXPIRED)),
    )
    yield (
        "used",
        pairing(PairingState(Stage.FAILED, SERVER, problem=PairingProblem.USED)),
    )
    yield "done", pairing(PairingState(Stage.DONE, SERVER))


@pytest.mark.parametrize("lang", ["en", "es"])
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_every_pairing_screen_fits(
    tmp_path: Path, size: tuple[int, int], lang: str
) -> None:
    i18n.activate(lang)
    out = shots_dir(tmp_path, size, lang) / "pairing"
    out.mkdir(parents=True, exist_ok=True)
    screen, session = make(size)
    surface = SpySurface(size)
    names = []
    # The fullest picker: the preset row, two found servers, "type an address".
    preset_screen, _ = make(size, preset="http://192.168.1.50:8000")
    preset_screen.step = Step.PICK_SERVER
    SpySurface.spills = []
    preset_screen.draw(surface)
    assert SpySurface.spills == [], f"picker with a preset: {SpySurface.spills}"
    pygame.image.save(surface, str(out / "picker-preset.png"))
    for name, show in scenes(screen, session):
        SpySurface.spills = []
        show()
        screen.draw(surface)
        assert SpySurface.spills == [], f"{name}: {SpySurface.spills}"
        pygame.image.save(surface, str(out / f"{name}.png"))
        names.append(name)
        with Image.open(out / f"{name}.png") as frame:
            assert frame.size == size
            colours = frame.convert("RGB").getcolors(maxcolors=size[0] * size[1])
        assert colours is not None and len(colours) >= 4, f"{name} is blank"
    assert len(names) == 8


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_the_code_grows_with_the_screen(size: tuple[int, int]) -> None:
    """The code is the point of the screen: its font follows the layout scale."""
    layout = Layout.for_size(*size)
    assert pairing_fonts(layout)["huge"].get_height() >= round(60 * layout.scale)


def joy(kind: int, **attrs: object) -> pygame.event.Event:
    return pygame.event.Event(kind, **attrs)


def key(code: int) -> pygame.event.Event:
    return pygame.event.Event(pygame.KEYDOWN, key=code)


#: The same journey on each profile: down to "Type the address", open the
#: keyboard, type a digit, delete it, delete on empty to go back, then pick the
#: found server.
GPI2 = {
    "down": joy(pygame.JOYHATMOTION, value=(0, -1)),
    "up": joy(pygame.JOYHATMOTION, value=(0, 1)),
    "select": joy(pygame.JOYBUTTONDOWN, button=0),
    "cancel": joy(pygame.JOYBUTTONDOWN, button=1),
    "keys": joy(pygame.JOYBUTTONDOWN, button=3),
    "done": joy(pygame.JOYBUTTONDOWN, button=2),
}
KEYBOARD = {
    "down": key(pygame.K_DOWN),
    "up": key(pygame.K_UP),
    "select": key(pygame.K_RETURN),
    "cancel": key(pygame.K_BACKSPACE),
    "keys": key(pygame.K_y),
    "done": key(pygame.K_x),
}


@pytest.mark.parametrize(
    ("profile", "events"), [("gpi2", GPI2), ("keyboard", KEYBOARD)]
)
def test_navigation_through_the_input_profile(
    profile: str, events: dict[str, pygame.event.Event]
) -> None:
    mapper = InputMapper(resolve_profile(profile))
    screen, session = make((640, 480))

    def press(name: str) -> None:
        button = mapper.map_event(events[name])
        assert button is not None, name
        screen.handle_input(button)

    press("down")
    press("down")  # two found servers, then "Type the address..."
    press("select")
    assert screen.step is Step.ENTER_ADDRESS
    press("select")  # types the highlighted key ("1")
    assert screen._keyboard.text == "1"
    press("keys")
    assert screen._keyboard.layout is KeyLayout.LETTERS
    press("cancel")
    assert screen._keyboard.text == ""
    press("done")  # empty address: refused in place
    assert screen.step is Step.ENTER_ADDRESS
    press("cancel")  # nothing left to delete: back to the picker
    assert screen.step is Step.PICK_SERVER
    press("up")
    press("up")
    press("select")
    assert screen.step is Step.PAIRING
    assert session.started == [SERVER]


def test_the_keyboard_profile_has_no_pad_but_still_navigates() -> None:
    """A profile without the hat (a computer keyboard) ignores hat motion."""
    mapper = InputMapper(resolve_profile("keyboard"))
    assert mapper.map_event(GPI2["down"]) is None
    assert mapper.map_event(KEYBOARD["down"]) is Button.DOWN
