"""The pairing screen: what a device with no ``config.json`` shows.

Flow: pick the server (found on the network, or typed on the on-screen
keyboard), then a code and a QR code for the parent to approve; on approval the
config is written and the player starts.

This module draws with the shared ``Theme`` and the app's fonts but lives
apart from ``views.py``: it runs before the player exists (no database, no
media). Lengths scale with the ``Layout`` it is given, like the player's views.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

import pygame
import segno

from .buttons import Button
from .i18n import _
from .keyboard import KeyResult, OnScreenKeyboard
from .layout import DEFAULT_LAYOUT, Layout
from .pairing import PairingProblem, PairingState, Stage, normalize_server_url
from .theme import DEFAULT_THEME, Theme
from .views import truncate_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from .discovery import FoundServer


class Session(Protocol):
    """What the screen needs of a pairing attempt (``PairingSession``)."""

    @property
    def state(self) -> PairingState:
        """The latest snapshot."""
        ...

    def start(self, server_url: str) -> None:
        """Begin pairing with ``server_url``."""
        ...

    def cancel(self) -> None:
        """Stop any attempt in progress."""
        ...


class Step(Enum):
    """Which part of the flow is showing."""

    PICK_SERVER = auto()
    ENTER_ADDRESS = auto()
    PAIRING = auto()


def problem_message(problem: PairingProblem | None) -> str:
    """The sentence that explains why pairing stopped.

    Args:
        problem: Why it stopped.

    Returns:
        Translated text for the child's or parent's eyes.
    """
    if problem is PairingProblem.EXPIRED:
        return _("This code has expired.")
    if problem is PairingProblem.USED:
        return _("This code was already used.")
    if problem is PairingProblem.DECLINED:
        return _("The request was declined.")
    if problem is PairingProblem.DISABLED:
        return _("Pairing is turned off on the server.")
    if problem is PairingProblem.BUSY:
        return _("The server is busy. Try again in a minute.")
    if problem is PairingProblem.UNREACHABLE:
        return _("Couldn't reach the server.")
    if problem is PairingProblem.CANT_SAVE:
        return _("Couldn't save the settings on this player.")
    if problem is PairingProblem.WRONG_SERVER:
        return _("That isn't the server you chose. Check the address.")
    return _("Something went wrong.")


def found_label(server: FoundServer) -> str:
    """The row for a server found on the network.

    Shows what the server says it is called, where it is, and the id it
    announces, so a look-alike stands out next to the parent's page.

    Args:
        server: The discovered server.

    Returns:
        Text such as ``KidsPlay · 192.168.1.20:8000 · AB12-CD34``.
    """
    address = server.url.removeprefix("http://").removeprefix("https://")
    parts = [server.name, address]
    if server.server_id:
        parts.append(server.server_id)
    return " · ".join(parts)


def wrap_text(font: pygame.font.Font, text: str, width: int) -> list[str]:
    """Break ``text`` into lines no wider than ``width`` pixels.

    Args:
        font: Font used to measure.
        text: Text to wrap (words separated by spaces).
        width: Maximum line width in pixels.

    Returns:
        The lines; a word wider than ``width`` gets a line of its own.
    """
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and font.size(candidate)[0] > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def qr_surface(text: str, size: int) -> pygame.Surface:
    """Render ``text`` as a QR code.

    Drawn as whole-pixel modules with a 4-module quiet zone, dark on white,
    the way scanners like it.

    Args:
        text: What the code encodes.
        size: Largest side of the result in pixels.

    Returns:
        A square surface of at most ``size`` pixels.
    """
    matrix = [list(row) for row in segno.make(text, error="m").matrix]
    modules = len(matrix)
    quiet = 4
    scale = max(1, size // (modules + 2 * quiet))
    side = (modules + 2 * quiet) * scale
    surface = pygame.Surface((side, side))
    surface.fill((255, 255, 255))
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                surface.fill(
                    (0, 0, 0),
                    ((x + quiet) * scale, (y + quiet) * scale, scale, scale),
                )
    return surface


class PairingScreen:
    """State and drawing of the pairing flow.

    Args:
        session: The pairing attempt to drive.
        fonts: The app's fonts (``small``, ``medium``, ``large``; ``huge`` for
            the code if present).
        servers: Returns the servers found on the network so far.
        theme: Colors.
        start_url: Server to start pairing with straight away (skips the
            picker), if known. It stays on the picker as the first row
            ("Preset: ..."), so going back with B never means typing it.
        layout: Screen geometry (``Layout.for_size``); every length here is
            scaled from the 640×480 design by it. ``fonts`` should be loaded
            with the same scale.
    """

    def __init__(
        self,
        session: Session,
        fonts: dict[str, pygame.font.Font],
        servers: Callable[[], list[FoundServer]],
        theme: Theme = DEFAULT_THEME,
        start_url: str | None = None,
        layout: Layout = DEFAULT_LAYOUT,
    ) -> None:
        self._session = session
        self._layout = layout
        self._fonts = fonts
        self._servers = servers
        self._preset = start_url or None
        self._theme = theme
        self.step = Step.PICK_SERVER
        self._choice = 0
        self._keyboard = OnScreenKeyboard()
        self._address_error = False
        self._qr_cache: tuple[str, int, pygame.Surface] | None = None
        if start_url:
            self._begin(start_url)

    # -- state ----------------------------------------------------------

    @property
    def done(self) -> bool:
        """Whether the config has been written."""
        return self._session.state.stage is Stage.DONE

    def _begin(self, url: str) -> None:
        self._session.start(url)
        self.step = Step.PAIRING

    def _options(self) -> list[FoundServer | str | None]:
        """The picker's rows: the preset server (a ``str`` URL), then servers
        found, then ``None`` for "type an address"."""
        found = [
            server
            for server in self._servers()
            if self._preset is None or server.url.rstrip("/") != self._preset
        ]
        preset: list[FoundServer | str | None] = [self._preset] if self._preset else []
        return [*preset, *found, None]

    @staticmethod
    def _label(option: FoundServer | str | None) -> str:
        """The picker row's text for one of :meth:`_options`."""
        if option is None:
            return _("Type the address…")
        if isinstance(option, str):
            address = option.removeprefix("http://").removeprefix("https://")
            return _("Preset: {address}").format(address=address)
        return found_label(option)

    def handle_input(self, button: Button) -> None:
        """Apply a button press.

        Args:
            button: The logical button.
        """
        if self.step is Step.PICK_SERVER:
            self._pick(button)
        elif self.step is Step.ENTER_ADDRESS:
            self._type(button)
        else:
            self._pairing(button)

    def _pick(self, button: Button) -> None:
        options = self._options()
        self._choice = min(self._choice, len(options) - 1)
        if button is Button.UP:
            self._choice = (self._choice - 1) % len(options)
        elif button is Button.DOWN:
            self._choice = (self._choice + 1) % len(options)
        elif button is Button.SELECT:
            chosen = options[self._choice]
            if chosen is None:
                self._address_error = False
                self.step = Step.ENTER_ADDRESS
            else:
                self._begin(chosen if isinstance(chosen, str) else chosen.url)

    def _type(self, button: Button) -> None:
        result = self._keyboard.handle(button)
        if result is KeyResult.CANCEL:
            self.step = Step.PICK_SERVER
        elif result is KeyResult.SUBMIT:
            url = normalize_server_url(self._keyboard.text)
            if url is None:
                self._address_error = True
            else:
                self._address_error = False
                self._begin(url)
        else:
            self._address_error = False

    def _pairing(self, button: Button) -> None:
        state = self._session.state
        if button is Button.CANCEL:
            self._session.cancel()
            self.step = Step.PICK_SERVER
        elif button is Button.SELECT and state.stage is Stage.FAILED:
            self._begin(state.server_url)  # a fresh code, same server

    def update(self) -> None:
        """Per-frame housekeeping (nothing to do; the session runs itself)."""

    # -- drawing --------------------------------------------------------

    def draw(self, surface: pygame.Surface) -> None:
        """Render the current step.

        Args:
            surface: Target surface (any size; the layout scales to it).
        """
        surface.fill(self._theme.BG)
        if self.step is Step.PICK_SERVER:
            self._draw_pick(surface)
        elif self.step is Step.ENTER_ADDRESS:
            self._draw_address(surface)
        else:
            self._draw_pairing(surface)

    def _text(
        self,
        surface: pygame.Surface,
        text: str,
        font_name: str,
        color: tuple[int, int, int],
        x: int,
        y: int,
        max_width: int,
    ) -> int:
        """Draw wrapped text; return the y below it."""
        font = self._fonts[font_name]
        for line in wrap_text(font, text, max_width):
            # A single word (a long server address) can be wider than the
            # column: cut it short rather than draw past the screen.
            image = font.render(truncate_text(font, line, max_width), True, color)
            surface.blit(image, (x, y))
            y += image.get_height() + self._layout.px(2)
        return y

    def _title(self, surface: pygame.Surface, text: str) -> int:
        px = self._layout.px
        w = surface.get_width()
        font = self._fonts["medium"]
        pygame.draw.rect(
            surface, self._theme.SURFACE, (0, 0, w, font.get_height() + px(16))
        )
        image = font.render(
            truncate_text(font, text, w - px(24)), True, self._theme.TEXT_BRIGHT
        )
        surface.blit(image, (px(12), px(8)))
        return font.get_height() + px(24)

    def _legend(self, surface: pygame.Surface, entries: list[tuple[str, str]]) -> None:
        px = self._layout.px
        font = self._fonts["small"]
        w, h = surface.get_size()
        parts = [
            (font.render(f"[{b}]", True, self._theme.PRIMARY),
             font.render(f" {a}", True, self._theme.TEXT_DIM))
            for b, a in entries
        ]  # fmt: skip
        gap = px(20)
        total = sum(b.get_width() + a.get_width() for b, a in parts) + gap * (
            len(parts) - 1
        )
        x = max(px(8), (w - total) // 2)
        y = h - font.get_height() - px(8)
        for btn, act in parts:
            surface.blit(btn, (x, y))
            x += btn.get_width()
            surface.blit(act, (x, y))
            x += act.get_width() + gap

    def _draw_pick(self, surface: pygame.Surface) -> None:
        px = self._layout.px
        w = surface.get_width()
        margin = px(16)
        y = self._title(surface, _("Set up this player"))
        y = self._text(
            surface,
            _("Which KidsPlay server should it connect to?"),
            "small",
            self._theme.TEXT,
            margin,
            y + px(8),
            w - 2 * margin,
        )
        y = self._text(
            surface,
            _(
                "Pick only a server you recognize. Anyone on the network can announce "
                "one."
            ),
            "small",
            self._theme.TEXT_DIM,
            margin,
            y + px(2),
            w - 2 * margin,
        )
        options = self._options()
        self._choice = min(self._choice, len(options) - 1)
        font = self._fonts["medium"]
        row_h = font.get_height() + px(16)
        y += px(10)
        for i, option in enumerate(options):
            label = self._label(option)
            rect = pygame.Rect(margin, y, w - 2 * margin, row_h)
            chosen = i == self._choice
            pygame.draw.rect(
                surface,
                self._theme.SURFACE_SEL if chosen else self._theme.SURFACE,
                rect,
                border_radius=px(6),
            )
            image = font.render(
                truncate_text(font, label, rect.width - px(20)),
                True,
                self._theme.TEXT_BRIGHT if chosen else self._theme.TEXT,
            )
            surface.blit(
                image, (rect.x + px(10), rect.y + (row_h - image.get_height()) // 2)
            )
            y += row_h + px(8)
        if all(o is None or isinstance(o, str) for o in options):
            self._text(
                surface,
                _("Looking for a server on your network…"),
                "small",
                self._theme.TEXT_DIM,
                margin,
                y + px(4),
                w - 2 * margin,
            )
        self._legend(surface, [("A", _("Choose"))])

    def _draw_address(self, surface: pygame.Surface) -> None:
        px = self._layout.px
        w, h = surface.get_size()
        margin = px(16)
        y = self._title(surface, _("Server address"))
        font = self._fonts["medium"]
        box = pygame.Rect(margin, y + px(8), w - 2 * margin, font.get_height() + px(14))
        pygame.draw.rect(surface, self._theme.SURFACE, box, border_radius=px(6))
        pygame.draw.rect(
            surface, self._theme.PRIMARY, box, width=max(1, px(2)), border_radius=px(6)
        )
        shown = self._keyboard.text
        while shown and font.size(shown + "|")[0] > box.width - px(20):
            shown = shown[1:]
        image = font.render(shown + "|", True, self._theme.TEXT_BRIGHT)
        surface.blit(image, (box.x + px(10), box.y + px(7)))
        below = box.bottom + px(6)
        small_h = self._fonts["small"].get_height()
        if self._address_error:
            self._text(
                surface,
                _("That doesn't look like an address."),
                "small",
                self._theme.ACCENT,
                margin,
                below,
                w - 2 * margin,
            )
        legend_h = small_h + px(16)
        area = pygame.Rect(margin, below + small_h + px(8), w - 2 * margin, 0)
        area.height = h - legend_h - area.y
        self._keyboard.draw(surface, area, self._fonts["medium"], self._theme)
        self._legend(
            surface,
            [
                ("A", _("Type")),
                ("B", _("Delete")),
                ("Y", _("Keys")),
                ("X", _("Done")),
            ],
        )

    def _qr(self, url: str, size: int) -> pygame.Surface:
        cached = self._qr_cache
        if cached is None or cached[0] != url or cached[1] != size:
            cached = (url, size, qr_surface(url, size))
            self._qr_cache = cached
        return cached[2]

    def _draw_pairing(self, surface: pygame.Surface) -> None:
        px = self._layout.px
        w = surface.get_width()
        margin = px(16)
        state = self._session.state
        y = self._title(surface, _("Set up this player"))
        if state.stage is Stage.CONNECTING:
            self._text(
                surface,
                _("Connecting to {url}…").format(url=state.server_url),
                "medium",
                self._theme.TEXT,
                margin,
                y + px(20),
                w - 2 * margin,
            )
            self._legend(surface, [("B", _("Back"))])
        elif state.stage is Stage.WAITING:
            self._draw_code(surface, y)
        elif state.stage is Stage.DONE:
            self._text(
                surface,
                _("All set! Starting…"),
                "large",
                self._theme.PRIMARY,
                margin,
                y + px(40),
                w - 2 * margin,
            )
        else:
            y = self._text(
                surface,
                problem_message(state.problem),
                "large",
                self._theme.ACCENT,
                margin,
                y + px(30),
                w - 2 * margin,
            )
            self._legend(surface, [("A", _("Try again")), ("B", _("Change server"))])

    def _draw_code(self, surface: pygame.Surface, top: int) -> None:
        px = self._layout.px
        w, h = surface.get_size()
        state = self._session.state
        legend_h = self._fonts["small"].get_height() + px(16)
        qr_size = min(h - top - legend_h - px(16), w * 2 // 5)
        link = f"{state.server_url}/devices?pair={state.code}"
        qr = self._qr(link, qr_size)
        surface.blit(qr, (px(12), top + px(8)))
        x = px(12) + qr.get_width() + px(16)
        width = w - x - px(12)
        code_font = self._fonts.get("huge", self._fonts["large"])
        code = state.code
        image = code_font.render(code, True, self._theme.TEXT_BRIGHT)
        if image.get_width() > width:
            image = self._fonts["large"].render(code, True, self._theme.TEXT_BRIGHT)
        y = top + px(8)
        surface.blit(image, (x, y))
        y += image.get_height() + px(6)
        y = self._text(
            surface,
            _(
                "Scan the picture with a phone, or open {url} and type this code."
            ).format(url=f"{state.server_url}/devices"),
            "small",
            self._theme.TEXT,
            x,
            y,
            width,
        )
        minutes, seconds = divmod(state.seconds_left, 60)
        y = self._text(
            surface,
            _("Expires in {time}").format(time=f"{minutes}:{seconds:02d}"),
            "small",
            self._theme.TEXT_DIM,
            x,
            y + px(6),
            width,
        )
        if state.server_id:
            self._text(
                surface,
                _("Server ID: {id}. The approval page shows the same ID.").format(
                    id=state.server_id
                ),
                "small",
                self._theme.TEXT_DIM,
                x,
                y + px(6),
                width,
            )
        self._legend(surface, [("B", _("Change server"))])
