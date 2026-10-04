"""Tests for the input profiles and the event mapper."""

import logging

import pygame
import pytest

from kidsplay_device.buttons import Button
from kidsplay_device.input_profiles import (
    DEFAULT_INPUT_PROFILE,
    PROFILES,
    InputMapper,
    resolve_profile,
)

# The bindings every install had before input profiles existed, frozen here on
# purpose (not imported): the gpi2 profile must never drift from them.
LEGACY_KEYS = {
    pygame.K_UP: Button.UP,
    pygame.K_DOWN: Button.DOWN,
    pygame.K_LEFT: Button.LEFT,
    pygame.K_RIGHT: Button.RIGHT,
    pygame.K_RETURN: Button.SELECT,
    pygame.K_a: Button.SELECT,
    pygame.K_b: Button.CANCEL,
    pygame.K_x: Button.PLAYPAUSE,
    pygame.K_y: Button.REPEAT,
    pygame.K_SPACE: Button.PLAYPAUSE,
    pygame.K_ESCAPE: Button.TERMINATE,
    pygame.K_BACKSPACE: Button.CANCEL,
    pygame.K_EQUALS: Button.VOLUME_UP,
    pygame.K_PLUS: Button.VOLUME_UP,
    pygame.K_KP_PLUS: Button.VOLUME_UP,
    pygame.K_MINUS: Button.VOLUME_DOWN,
    pygame.K_KP_MINUS: Button.VOLUME_DOWN,
}
LEGACY_JOY = {
    0: Button.SELECT,
    1: Button.CANCEL,
    2: Button.PLAYPAUSE,
    3: Button.REPEAT,
    4: Button.VOLUME_DOWN,
    5: Button.VOLUME_UP,
}


def key(code: int) -> pygame.event.Event:
    return pygame.event.Event(pygame.KEYDOWN, key=code)


def joy(number: int) -> pygame.event.Event:
    return pygame.event.Event(pygame.JOYBUTTONDOWN, button=number)


def hat(x: int, y: int) -> pygame.event.Event:
    return pygame.event.Event(pygame.JOYHATMOTION, value=(x, y))


def axis(number: int, value: float) -> pygame.event.Event:
    return pygame.event.Event(pygame.JOYAXISMOTION, axis=number, value=value)


def mapper(name: str = "gpi2", **overrides: object) -> InputMapper:
    return InputMapper(resolve_profile(name, overrides or None))


class TestGpi2IsTheOldBehaviour:
    def test_is_the_default_profile(self) -> None:
        assert DEFAULT_INPUT_PROFILE == "gpi2"

    @pytest.mark.parametrize(("code", "button"), LEGACY_KEYS.items())
    def test_every_legacy_key(self, code: int, button: Button) -> None:
        assert mapper().map_event(key(code)) is button

    @pytest.mark.parametrize(("number", "button"), LEGACY_JOY.items())
    def test_every_legacy_joystick_button(self, number: int, button: Button) -> None:
        assert mapper().map_event(joy(number)) is button

    def test_nothing_else_is_bound(self) -> None:
        m = mapper()
        assert m.map_event(key(pygame.K_F1)) is None
        assert m.map_event(joy(6)) is None
        assert m.map_event(joy(7)) is None
        # No terminate on a pad: the power button handles shutdown.
        assert Button.TERMINATE not in {m.map_event(joy(n)) for n in range(32)}

    @pytest.mark.parametrize(
        ("x", "y", "button"),
        [
            (0, 1, Button.UP),
            (0, -1, Button.DOWN),
            (-1, 0, Button.LEFT),
            (1, 0, Button.RIGHT),
        ],
    )
    def test_hat_moves_the_cursor(self, x: int, y: int, button: Button) -> None:
        assert mapper().map_event(hat(x, y)) is button

    def test_hat_centre_and_sticks_do_nothing(self) -> None:
        m = mapper()
        assert m.map_event(hat(0, 0)) is None
        assert m.map_event(axis(0, 1.0)) is None
        assert m.map_event(axis(1, -1.0)) is None

    def test_escape_quits_and_back_keys_go_back(self) -> None:
        m = mapper()
        assert m.map_event(key(pygame.K_ESCAPE)) is Button.TERMINATE
        assert m.map_event(key(pygame.K_b)) is Button.CANCEL
        assert m.map_event(key(pygame.K_BACKSPACE)) is Button.CANCEL

    def test_volume_buttons_are_not_on_by_default(self) -> None:
        assert PROFILES["gpi2"].volume_buttons is False

    def test_other_events_are_ignored(self) -> None:
        assert (
            mapper().map_event(pygame.event.Event(pygame.KEYUP, key=pygame.K_UP))
            is None
        )
        assert (
            mapper().map_event(pygame.event.Event(pygame.MOUSEMOTION, pos=(0, 0)))
            is None
        )


class TestProfiles:
    def test_the_named_profiles_exist(self) -> None:
        assert {"gpi2", "keyboard", "generic-gamepad"} <= set(PROFILES)
        assert all(p.name == name for name, p in PROFILES.items())

    def test_keyboard_has_no_pad_and_volume_keys_on(self) -> None:
        profile = PROFILES["keyboard"]
        assert profile.volume_buttons is True
        m = InputMapper(profile)
        assert m.map_event(key(pygame.K_RETURN)) is Button.SELECT
        assert m.map_event(joy(0)) is None
        assert m.map_event(hat(0, 1)) is None

    def test_generic_gamepad_adds_the_left_stick(self) -> None:
        m = mapper("generic-gamepad")
        assert m.map_event(joy(0)) is Button.SELECT
        assert m.map_event(hat(0, 1)) is Button.UP
        assert m.map_event(axis(0, 0.9)) is Button.RIGHT
        assert m.map_event(axis(0, 0.9)) is None  # held: one press, not one per event
        assert m.map_event(axis(0, 0.0)) is None  # released
        assert m.map_event(axis(0, -0.9)) is Button.LEFT
        assert m.map_event(axis(1, 0.9)) is Button.DOWN
        assert m.map_event(axis(1, 0.3)) is None  # inside the dead zone
        assert m.map_event(axis(1, -0.9)) is Button.UP
        assert m.map_event(axis(2, 1.0)) is None  # an unmapped axis

    def test_unknown_name_falls_back_to_gpi2_and_says_so(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            profile = resolve_profile("no-such-hardware")
        assert profile is PROFILES["gpi2"]
        assert "no-such-hardware" in caplog.text


class TestOverrides:
    def test_rebind_and_add_a_key(self) -> None:
        m = mapper(keys={"K_F5": "repeat", "return": "playpause", "z": "cancel"})
        assert m.map_event(key(pygame.K_F5)) is Button.REPEAT
        assert m.map_event(key(pygame.K_RETURN)) is Button.PLAYPAUSE
        assert m.map_event(key(pygame.K_z)) is Button.CANCEL
        assert m.map_event(key(pygame.K_a)) is Button.SELECT  # untouched

    def test_null_removes_a_binding(self) -> None:
        m = mapper(keys={"K_a": None}, joy_buttons={"3": None})
        assert m.map_event(key(pygame.K_a)) is None
        assert m.map_event(joy(3)) is None
        assert m.map_event(key(pygame.K_RETURN)) is Button.SELECT

    def test_joystick_buttons(self) -> None:
        m = mapper(joy_buttons={"7": "playpause", "0": "cancel"})
        assert m.map_event(joy(7)) is Button.PLAYPAUSE
        assert m.map_event(joy(0)) is Button.CANCEL

    def test_volume_buttons_can_be_switched(self) -> None:
        assert resolve_profile("gpi2", {"volume_buttons": True}).volume_buttons
        assert not resolve_profile("keyboard", {"volume_buttons": False}).volume_buttons

    def test_bad_entries_are_skipped_not_fatal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            m = mapper(
                keys={"K_NOT_A_KEY": "select", "K_F1": "warp", "K_F2": "select"},
                joy_buttons={"x": "select", "-1": "select", "9": "warp", "8": "select"},
                volume_buttons="yes",
            )
        assert m.map_event(key(pygame.K_F1)) is None
        assert m.map_event(key(pygame.K_F2)) is Button.SELECT
        assert m.map_event(joy(8)) is Button.SELECT
        assert m.map_event(joy(9)) is None
        assert caplog.text.count("Ignoring input override") == 5
        assert m.profile.volume_buttons is False  # not a bool: ignored

    def test_malformed_override_sections_are_ignored(self) -> None:
        m = mapper(keys=["K_F1"], joy_buttons=3)
        assert m.map_event(key(pygame.K_RETURN)) is Button.SELECT

    def test_overrides_do_not_change_the_shared_profile(self) -> None:
        mapper(keys={"K_a": None})
        assert InputMapper(PROFILES["gpi2"]).map_event(key(pygame.K_a)) is Button.SELECT
