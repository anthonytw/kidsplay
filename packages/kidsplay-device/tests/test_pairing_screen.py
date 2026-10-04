"""Tests for the pairing screen, its QR code and the pairing app's input map."""

import os
from collections.abc import Iterator

import pygame
import pytest

from kidsplay_device import i18n
from kidsplay_device.buttons import Button
from kidsplay_device.discovery import FoundServer
from kidsplay_device.fonts import load_text_fonts
from kidsplay_device.input_profiles import InputMapper, resolve_profile
from kidsplay_device.keyboard import Layout
from kidsplay_device.pairing import PairingProblem, PairingState, Stage
from kidsplay_device.pairing_screen import (
    PairingScreen,
    Step,
    problem_message,
    qr_surface,
    wrap_text,
)

# SDL reads this when the display is first initialised, not when pygame is imported.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

SERVER = "http://192.168.1.20:8000"


class FakeSession:
    """Stands in for ``PairingSession``: records calls, shows a set state."""

    def __init__(self) -> None:
        self.state = PairingState()
        self.started: list[str] = []
        self.cancelled = 0

    def start(self, server_url: str) -> None:
        self.started.append(server_url)
        self.state = PairingState(Stage.CONNECTING, server_url)

    def cancel(self) -> None:
        self.cancelled += 1


@pytest.fixture(autouse=True)
def _pygame() -> Iterator[None]:
    pygame.font.init()
    yield
    i18n.activate("en")


@pytest.fixture
def fonts() -> dict[str, pygame.font.Font]:
    fonts = load_text_fonts()
    fonts["huge"] = pygame.font.Font(None, 88)
    return fonts


def make(
    fonts: dict[str, pygame.font.Font],
    found: list[FoundServer] | None = None,
    start_url: str | None = None,
) -> tuple[PairingScreen, FakeSession]:
    session = FakeSession()
    screen = PairingScreen(
        session,
        fonts,
        lambda: found or [],
        start_url=start_url,
    )
    return screen, session


def press(screen: PairingScreen, *buttons: Button) -> None:
    for b in buttons:
        screen.handle_input(b)


def render(screen: PairingScreen, size: tuple[int, int] = (640, 480)) -> pygame.Surface:
    surface = pygame.Surface(size)
    screen.draw(surface)
    return surface


def type_address(screen: PairingScreen, text: str) -> None:
    kb = screen._keyboard
    for ch in text:
        for r, row in enumerate(kb.rows):
            for c, key in enumerate(row):
                if key.char == ch:
                    kb.row, kb.col = r, c
        press(screen, Button.SELECT)


class TestServerChoice:
    def test_starts_on_the_picker_with_manual_entry(self, fonts: dict) -> None:
        screen, _ = make(fonts)
        assert screen.step is Step.PICK_SERVER
        assert screen._options() == [None]

    def test_found_servers_come_first(self, fonts: dict) -> None:
        found = [FoundServer("KidsPlay", SERVER)]
        screen, session = make(fonts, found)
        assert screen._options() == [*found, None]
        press(screen, Button.SELECT)  # first option: the found server
        assert session.started == [SERVER]
        assert screen.step is Step.PAIRING

    def test_choice_wraps(self, fonts: dict) -> None:
        screen, _ = make(fonts, [FoundServer("A", "http://a:1")])
        press(screen, Button.UP)
        assert screen._choice == 1
        press(screen, Button.DOWN)
        assert screen._choice == 0

    def test_manual_entry_then_pair(self, fonts: dict) -> None:
        screen, session = make(fonts)
        press(screen, Button.SELECT)  # "type the address"
        assert screen.step is Step.ENTER_ADDRESS
        type_address(screen, "192.168.1.20")
        press(screen, Button.PLAYPAUSE)
        assert session.started == ["http://192.168.1.20:8000"]
        assert screen.step is Step.PAIRING

    def test_bad_address_is_refused_in_place(self, fonts: dict) -> None:
        screen, session = make(fonts)
        press(screen, Button.SELECT)
        press(screen, Button.PLAYPAUSE)  # nothing typed
        assert session.started == []
        assert screen.step is Step.ENTER_ADDRESS
        assert screen._address_error
        type_address(screen, "1")
        assert not screen._address_error

    def test_b_on_empty_keyboard_goes_back(self, fonts: dict) -> None:
        screen, _ = make(fonts)
        press(screen, Button.SELECT, Button.CANCEL)
        assert screen.step is Step.PICK_SERVER

    def test_start_url_skips_the_picker(self, fonts: dict) -> None:
        screen, session = make(fonts, start_url=SERVER)
        assert session.started == [SERVER]
        assert screen.step is Step.PAIRING

    def test_servers_appearing_later_are_offered(self, fonts: dict) -> None:
        found: list[FoundServer] = []
        session = FakeSession()
        screen = PairingScreen(session, fonts, lambda: found)
        assert screen._options() == [None]
        found.append(FoundServer("KidsPlay", SERVER))
        assert len(screen._options()) == 2


class TestPresetServer:
    """A preset server survives B: the parent never has to type it."""

    def test_b_then_a_on_the_preset_row_gets_a_fresh_code(self, fonts: dict) -> None:
        screen, session = make(fonts, start_url=SERVER)
        press(screen, Button.CANCEL)
        assert screen.step is Step.PICK_SERVER
        assert screen._options()[0] == SERVER  # first, before "type an address"
        press(screen, Button.SELECT)
        assert session.started == [SERVER, SERVER]
        assert screen.step is Step.PAIRING

    def test_preset_comes_before_found_servers_and_manual_entry(
        self, fonts: dict
    ) -> None:
        other = FoundServer("KidsPlay", "http://192.168.1.99:8000")
        screen, _session = make(fonts, found=[other], start_url=SERVER)
        press(screen, Button.CANCEL)
        assert screen._options() == [SERVER, other, None]

    def test_a_found_copy_of_the_preset_is_not_listed_twice(self, fonts: dict) -> None:
        screen, _session = make(
            fonts, found=[FoundServer("KidsPlay", SERVER)], start_url=SERVER
        )
        assert screen._options() == [SERVER, None]

    def test_no_preset_changes_nothing(self, fonts: dict) -> None:
        screen, _session = make(fonts)
        assert screen._options() == [None]

    def test_the_row_says_preset_and_shows_the_address(self, fonts: dict) -> None:
        screen, _session = make(fonts, start_url="https://kidsplay.example.net")
        press(screen, Button.CANCEL)
        assert screen._label(screen._options()[0]) == "Preset: kidsplay.example.net"
        assert render(screen).get_size() == (640, 480)  # draws without error

    def test_the_row_is_translated(self, fonts: dict) -> None:
        i18n.activate("es")
        screen, _session = make(fonts, start_url=SERVER)
        assert screen._label(SERVER) == "Preestablecido: 192.168.1.20:8000"


class TestPairingStep:
    def test_failure_offers_retry_with_the_same_server(self, fonts: dict) -> None:
        screen, session = make(fonts, start_url=SERVER)
        session.state = PairingState(
            Stage.FAILED, SERVER, problem=PairingProblem.EXPIRED
        )
        press(screen, Button.SELECT)
        assert session.started == [SERVER, SERVER]

    def test_select_does_nothing_while_waiting(self, fonts: dict) -> None:
        screen, session = make(fonts, start_url=SERVER)
        session.state = PairingState(Stage.WAITING, SERVER, "ABCD-2345", None, 500)
        press(screen, Button.SELECT)
        assert session.started == [SERVER]

    def test_b_cancels_and_returns_to_the_picker(self, fonts: dict) -> None:
        screen, session = make(fonts, start_url=SERVER)
        press(screen, Button.CANCEL)
        assert session.cancelled == 1
        assert screen.step is Step.PICK_SERVER

    def test_done(self, fonts: dict) -> None:
        screen, session = make(fonts, start_url=SERVER)
        assert not screen.done
        session.state = PairingState(Stage.DONE, SERVER)
        assert screen.done


class TestMessages:
    @pytest.mark.parametrize("problem", [*PairingProblem, None])
    def test_every_problem_has_a_sentence(self, problem: PairingProblem | None) -> None:
        assert problem_message(problem)

    def test_expired_and_used_are_distinct_and_clear(self) -> None:
        assert problem_message(PairingProblem.EXPIRED) == "This code has expired."
        assert problem_message(PairingProblem.USED) == "This code was already used."

    def test_spanish(self) -> None:
        i18n.activate("es")
        assert problem_message(PairingProblem.EXPIRED) == "Este código venció."
        assert problem_message(PairingProblem.USED) == "Este código ya se usó."


class TestQr:
    def test_matrix_matches_the_encoded_text(self) -> None:
        import segno

        text = f"{SERVER}/devices?pair=ABCD-2345"
        surface = qr_surface(text, 240)
        assert surface.get_width() == surface.get_height() <= 240
        matrix = [list(r) for r in segno.make(text, error="m").matrix]
        scale = surface.get_width() // (len(matrix) + 8)
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                px = surface.get_at(
                    ((x + 4) * scale + scale // 2, (y + 4) * scale + scale // 2)
                )
                assert (px[:3] == (0, 0, 0)) == bool(dark)
        assert surface.get_at((1, 1))[:3] == (255, 255, 255)  # quiet zone

    def test_fits_a_small_size(self) -> None:
        assert qr_surface("http://a", 100).get_width() <= 100


class TestWrap:
    def test_wraps_on_words(self) -> None:
        font = pygame.font.Font(None, 30)
        lines = wrap_text(font, "one two three four five six seven", 120)
        assert len(lines) > 1
        assert all(font.size(line)[0] <= 120 for line in lines)
        assert " ".join(lines) == "one two three four five six seven"

    def test_long_word_gets_its_own_line(self) -> None:
        font = pygame.font.Font(None, 30)
        assert wrap_text(font, "a supercalifragilistic b", 40) == [
            "a",
            "supercalifragilistic",
            "b",
        ]

    def test_empty(self) -> None:
        assert wrap_text(pygame.font.Font(None, 30), "", 100) == []


class TestDrawing:
    """Every step renders at the handheld's size and at another, without error."""

    @pytest.mark.parametrize("size", [(640, 480), (320, 240), (800, 480)])
    def test_all_steps(self, fonts: dict, size: tuple[int, int]) -> None:
        screen, session = make(fonts, [FoundServer("KidsPlay", SERVER)])
        render(screen, size)  # picker
        press(screen, Button.DOWN, Button.SELECT)
        render(screen, size)  # keyboard
        screen._keyboard.layout = Layout.LETTERS
        render(screen, size)
        press(screen, Button.CANCEL)
        press(screen, Button.UP, Button.SELECT)
        for state in (
            PairingState(Stage.CONNECTING, SERVER),
            PairingState(Stage.WAITING, SERVER, "ABCD-2345", None, 583),
            PairingState(Stage.DONE, SERVER),
            *(PairingState(Stage.FAILED, SERVER, problem=p) for p in PairingProblem),
        ):
            session.state = state
            render(screen, size)

    def test_code_and_qr_are_on_screen(self, fonts: dict) -> None:
        screen, session = make(fonts, start_url=SERVER)
        session.state = PairingState(Stage.WAITING, SERVER, "ABCD-2345", None, 583)
        surface = render(screen)
        # The QR is black-on-white at the left; the right half is not blank.
        assert surface.get_at((14, 60))[:3] == (255, 255, 255)
        right = pygame.Surface((300, 200))
        right.blit(surface, (0, 0), (330, 60, 300, 200))
        assert (
            len(
                {
                    tuple(right.get_at((x, y))[:3])
                    for x in range(0, 300, 2)
                    for y in range(0, 200, 2)
                }
            )
            > 1
        )

    def test_qr_links_into_the_web_ui(
        self, fonts: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        real = qr_surface
        monkeypatch.setattr(
            "kidsplay_device.pairing_screen.qr_surface",
            lambda text, size: (seen.append(text), real(text, size))[1],
        )
        screen, session = make(fonts, start_url=SERVER)
        session.state = PairingState(Stage.WAITING, SERVER, "ABCD-2345", None, 583)
        render(screen)
        render(screen)  # cached: not re-encoded every frame
        assert seen == [f"{SERVER}/devices?pair=ABCD-2345"]

    @pytest.mark.parametrize("language", ["en", "es"])
    def test_keyboard_legend_fits_the_screen(self, fonts: dict, language: str) -> None:
        """Four hints on one line, in both languages, must not be clipped."""
        i18n.activate(language)
        screen, _ = make(fonts)
        press(screen, Button.SELECT)
        surface = render(screen)
        bg = screen._theme.BG
        row = surface.get_height() - 12
        assert surface.get_at((0, row))[:3] == bg
        assert surface.get_at((surface.get_width() - 1, row))[:3] == bg
        legend_left = next(
            x for x in range(surface.get_width()) if surface.get_at((x, row))[:3] != bg
        )
        assert legend_left >= 8

    def test_spanish_renders(self, fonts: dict) -> None:
        i18n.activate("es")
        screen, session = make(fonts, start_url=SERVER)
        session.state = PairingState(Stage.WAITING, SERVER, "ABCD-2345", None, 583)
        render(screen)


class TestButtonMap:
    """The pairing app maps input through the player's input profiles."""

    mapper = InputMapper(resolve_profile("gpi2"))

    def key(self, key: int) -> pygame.event.Event:
        return pygame.event.Event(pygame.KEYDOWN, key=key)

    def test_keyboard(self) -> None:
        assert self.mapper.map_event(self.key(pygame.K_RETURN)) is Button.SELECT
        assert self.mapper.map_event(self.key(pygame.K_b)) is Button.CANCEL
        assert self.mapper.map_event(self.key(pygame.K_y)) is Button.REPEAT
        assert self.mapper.map_event(self.key(pygame.K_x)) is Button.PLAYPAUSE
        assert self.mapper.map_event(self.key(pygame.K_ESCAPE)) is Button.TERMINATE
        assert self.mapper.map_event(self.key(pygame.K_q)) is None

    def test_gamepad(self) -> None:
        def joy(type_: int, **attrs: object) -> pygame.event.Event:
            return pygame.event.Event(type_, **attrs)

        assert (
            self.mapper.map_event(joy(pygame.JOYBUTTONDOWN, button=0)) is Button.SELECT
        )
        assert (
            self.mapper.map_event(joy(pygame.JOYBUTTONDOWN, button=1)) is Button.CANCEL
        )
        assert (
            self.mapper.map_event(joy(pygame.JOYHATMOTION, value=(0, 1))) is Button.UP
        )
        assert (
            self.mapper.map_event(joy(pygame.JOYHATMOTION, value=(-1, 0)))
            is Button.LEFT
        )
        assert self.mapper.map_event(joy(pygame.JOYHATMOTION, value=(0, 0))) is None
