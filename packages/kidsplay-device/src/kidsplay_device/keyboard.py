"""An on-screen keyboard for a handheld with no keyboard.

Used to type the server address while pairing, so the first layout is the
numeric one (an IP address like ``192.168.1.20:8000``); a second layout has
letters for host names such as ``kidsplay.local``.

Buttons: the D-pad moves, SELECT (A) types the highlighted key, CANCEL (B)
deletes the last character (and leaves when there is nothing left to delete),
REPEAT (Y) switches layout, PLAYPAUSE (X) submits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

import pygame

from .buttons import Button
from .i18n import N_, _
from .views import truncate_text

if TYPE_CHECKING:
    from .theme import Theme


class Layout(Enum):
    """Which keys are on offer."""

    NUMERIC = auto()
    LETTERS = auto()


class KeyResult(Enum):
    """What the keyboard tells its owner."""

    SUBMIT = auto()
    CANCEL = auto()


class Action(Enum):
    """Non-character keys."""

    BACKSPACE = auto()
    SWITCH = auto()
    SUBMIT = auto()


@dataclass(frozen=True)
class Key:
    """One key: a character to type, or an action.

    Attributes:
        char: Character typed, if a character key.
        action: Action performed, if an action key.
    """

    char: str = ""
    action: Action | None = None


def _chars(text: str) -> list[Key]:
    return [Key(char=c) for c in text]


_ACTIONS = [
    Key(action=Action.BACKSPACE),
    Key(action=Action.SWITCH),
    Key(action=Action.SUBMIT),
]

LAYOUTS: dict[Layout, list[list[Key]]] = {
    Layout.NUMERIC: [
        _chars("123"),
        _chars("456"),
        _chars("789"),
        _chars(".0:"),
        _ACTIONS,
    ],
    Layout.LETTERS: [
        _chars("qwertyuiop"),
        _chars("asdfghjkl-"),
        _chars("zxcvbnm.:_"),
        _chars("1234567890"),
        _ACTIONS,
    ],
}
"""Key rows of each layout; the last row is always the three action keys."""

_ACTION_LABELS = {
    Action.BACKSPACE: N_("Del"),
    Action.SUBMIT: N_("OK"),
}


class OnScreenKeyboard:
    """Text entry driven by the D-pad.

    Args:
        text: Initial text.
        max_length: Longest text accepted.
    """

    def __init__(self, text: str = "", *, max_length: int = 64) -> None:
        self.text = text
        self.max_length = max_length
        self.layout = Layout.NUMERIC
        self.row = 0
        self.col = 0

    @property
    def rows(self) -> list[list[Key]]:
        """Key rows of the current layout."""
        return LAYOUTS[self.layout]

    @property
    def selected(self) -> Key:
        """The highlighted key."""
        return self.rows[self.row][self.col]

    def _switch_layout(self) -> None:
        self.layout = (
            Layout.LETTERS if self.layout is Layout.NUMERIC else Layout.NUMERIC
        )
        self.row = min(self.row, len(self.rows) - 1)
        self.col = min(self.col, len(self.rows[self.row]) - 1)
        if self.row == len(self.rows) - 1:
            self.col = 1  # stay on the switch key, so a second press switches back

    def _move_vertical(self, delta: int) -> None:
        old = self.rows[self.row]
        self.row = (self.row + delta) % len(self.rows)
        new = self.rows[self.row]
        # Keep the same horizontal position, whatever the row length.
        fraction = self.col / max(1, len(old) - 1)
        self.col = round(fraction * (len(new) - 1))

    def _backspace(self) -> KeyResult | None:
        if not self.text:
            return KeyResult.CANCEL
        self.text = self.text[:-1]
        return None

    def handle(self, button: Button) -> KeyResult | None:
        """Apply a button press.

        Args:
            button: The logical button.

        Returns:
            ``SUBMIT`` or ``CANCEL`` when the owner should act, else None.
        """
        if button is Button.LEFT:
            self.col = (self.col - 1) % len(self.rows[self.row])
        elif button is Button.RIGHT:
            self.col = (self.col + 1) % len(self.rows[self.row])
        elif button is Button.UP:
            self._move_vertical(-1)
        elif button is Button.DOWN:
            self._move_vertical(1)
        elif button is Button.REPEAT:
            self._switch_layout()
        elif button is Button.PLAYPAUSE:
            return KeyResult.SUBMIT
        elif button is Button.CANCEL:
            return self._backspace()
        elif button is Button.SELECT:
            key = self.selected
            if key.action is Action.BACKSPACE:
                return self._backspace()
            if key.action is Action.SWITCH:
                self._switch_layout()
            elif key.action is Action.SUBMIT:
                return KeyResult.SUBMIT
            elif len(self.text) < self.max_length:
                self.text += key.char
        return None

    def label(self, key: Key) -> str:
        """The text drawn on ``key``."""
        if key.action is Action.SWITCH:
            return "abc" if self.layout is Layout.NUMERIC else "123"
        if key.action is not None:
            return _(_ACTION_LABELS[key.action])
        return key.char

    def draw(
        self,
        surface: pygame.Surface,
        area: pygame.Rect,
        font: pygame.font.Font,
        theme: Theme,
    ) -> None:
        """Draw the keys inside ``area`` (the text box is the owner's job).

        Args:
            surface: Target surface.
            area: Rectangle the keyboard fills.
            font: Font for the key labels.
            theme: Colors.
        """
        gap = max(2, area.height // 60)
        row_h = (area.height - gap * (len(self.rows) - 1)) // len(self.rows)
        for r, keys in enumerate(self.rows):
            key_w = (area.width - gap * (len(keys) - 1)) // len(keys)
            for c, key in enumerate(keys):
                rect = pygame.Rect(
                    area.x + c * (key_w + gap),
                    area.y + r * (row_h + gap),
                    key_w,
                    row_h,
                )
                chosen = (r, c) == (self.row, self.col)
                pygame.draw.rect(
                    surface,
                    theme.PRIMARY if chosen else theme.SURFACE,
                    rect,
                    border_radius=max(2, row_h // 8),
                )
                text = truncate_text(font, self.label(key), rect.width - 4)
                color = theme.BG if chosen else theme.TEXT
                label = font.render(text, True, color)
                surface.blit(label, label.get_rect(center=rect.center))
