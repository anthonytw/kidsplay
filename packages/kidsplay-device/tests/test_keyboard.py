"""Tests for the on-screen keyboard."""

import os

import pygame

from kidsplay_device.keyboard import (
    LAYOUTS,
    Action,
    KeyResult,
    Layout,
    OnScreenKeyboard,
)
from kidsplay_device.theme import DEFAULT_THEME
from kidsplay_device.views import Button

# SDL reads this when the display is first initialised, not when pygame is imported.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")


def press(kb: OnScreenKeyboard, *buttons: Button) -> KeyResult | None:
    result = None
    for button in buttons:
        result = kb.handle(button)
    return result


def type_text(kb: OnScreenKeyboard, text: str) -> None:
    """Navigate to and press each character, the way a player would."""
    for ch in text:
        for r, row in enumerate(kb.rows):
            for c, key in enumerate(row):
                if key.char == ch:
                    kb.row, kb.col = r, c
                    press(kb, Button.SELECT)
                    break
            else:
                continue
            break
        else:
            raise AssertionError(f"{ch!r} not on the {kb.layout} layout")


class TestLayouts:
    def test_numeric_first_and_ip_friendly(self) -> None:
        kb = OnScreenKeyboard()
        assert kb.layout is Layout.NUMERIC
        chars = {k.char for row in LAYOUTS[Layout.NUMERIC] for k in row}
        assert set("0123456789.:") <= chars

    def test_letters_cover_host_names(self) -> None:
        chars = {k.char for row in LAYOUTS[Layout.LETTERS] for k in row}
        assert set("abcdefghijklmnopqrstuvwxyz.-:_0123456789") <= chars

    def test_last_row_is_the_actions_in_both_layouts(self) -> None:
        for rows in LAYOUTS.values():
            assert [k.action for k in rows[-1]] == [
                Action.BACKSPACE,
                Action.SWITCH,
                Action.SUBMIT,
            ]


class TestTyping:
    def test_types_an_ip_address(self) -> None:
        kb = OnScreenKeyboard()
        type_text(kb, "192.168.1.20:8000")
        assert kb.text == "192.168.1.20:8000"

    def test_switches_layout_for_a_host_name(self) -> None:
        kb = OnScreenKeyboard()
        type_text(kb, "10")
        press(kb, Button.REPEAT)
        assert kb.layout is Layout.LETTERS
        type_text(kb, "nas.lan")
        press(kb, Button.REPEAT)
        assert kb.layout is Layout.NUMERIC
        assert kb.text == "10nas.lan"

    def test_select_on_switch_key_toggles(self) -> None:
        kb = OnScreenKeyboard()
        kb.row, kb.col = len(kb.rows) - 1, 1
        press(kb, Button.SELECT)
        assert kb.layout is Layout.LETTERS
        assert kb.selected.action is Action.SWITCH  # still on it: press to go back
        press(kb, Button.SELECT)
        assert kb.layout is Layout.NUMERIC

    def test_max_length(self) -> None:
        kb = OnScreenKeyboard(max_length=3)
        type_text(kb, "12345")
        assert kb.text == "123"


class TestEditing:
    def test_b_deletes_then_leaves_when_empty(self) -> None:
        kb = OnScreenKeyboard("12")
        assert press(kb, Button.CANCEL) is None
        assert kb.text == "1"
        assert press(kb, Button.CANCEL) is None
        assert kb.text == ""
        assert press(kb, Button.CANCEL) is KeyResult.CANCEL

    def test_delete_key(self) -> None:
        kb = OnScreenKeyboard("12")
        kb.row, kb.col = len(kb.rows) - 1, 0
        press(kb, Button.SELECT)
        assert kb.text == "1"

    def test_x_and_ok_key_submit(self) -> None:
        kb = OnScreenKeyboard("1")
        assert press(kb, Button.PLAYPAUSE) is KeyResult.SUBMIT
        kb.row, kb.col = len(kb.rows) - 1, 2
        assert press(kb, Button.SELECT) is KeyResult.SUBMIT


class TestNavigation:
    def test_wraps_horizontally_and_vertically(self) -> None:
        kb = OnScreenKeyboard()
        press(kb, Button.LEFT)
        assert kb.col == len(kb.rows[0]) - 1
        press(kb, Button.RIGHT)
        assert kb.col == 0
        press(kb, Button.UP)
        assert kb.row == len(kb.rows) - 1
        press(kb, Button.DOWN)
        assert kb.row == 0

    def test_vertical_moves_keep_the_column_between_uneven_rows(self) -> None:
        kb = OnScreenKeyboard()
        kb.layout = Layout.LETTERS
        kb.row, kb.col = 3, 9  # last digit, far right
        press(kb, Button.DOWN)  # action row has 3 keys
        assert kb.selected.action is Action.SUBMIT
        press(kb, Button.UP)
        assert kb.col == 9

    def test_every_position_is_reachable_and_valid(self) -> None:
        for layout in Layout:
            kb = OnScreenKeyboard()
            kb.layout = layout
            for _ in range(60):
                for button in (Button.DOWN, Button.RIGHT, Button.UP, Button.LEFT):
                    press(kb, button)
                    assert 0 <= kb.row < len(kb.rows)
                    assert 0 <= kb.col < len(kb.rows[kb.row])


class TestDrawing:
    def test_draws_highlight_inside_the_area(self) -> None:
        pygame.font.init()
        font = pygame.font.Font(None, 30)
        surface = pygame.Surface((640, 480))
        surface.fill((0, 0, 0))
        area = pygame.Rect(16, 200, 608, 230)
        kb = OnScreenKeyboard()
        kb.draw(surface, area, font, DEFAULT_THEME)
        # Nothing outside the area, and the highlighted key is in the primary colour.
        assert surface.get_at((5, 5))[:3] == (0, 0, 0)
        assert surface.get_at((5, 300))[:3] == (0, 0, 0)
        assert any(
            surface.get_at((x, y))[:3] == DEFAULT_THEME.PRIMARY
            for x in range(area.left, area.right, 3)
            for y in range(area.top, area.bottom, 3)
        )

    def test_labels_are_translated(self) -> None:
        from kidsplay_device.i18n import activate

        kb = OnScreenKeyboard()
        try:
            activate("es")
            assert kb.label(kb.rows[-1][0]) == "Borrar"
            assert kb.label(kb.rows[-1][2]) == "Listo"
        finally:
            activate("en")
