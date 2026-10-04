"""Input profiles: which key, joystick button or axis does what.

A profile is a named table, so supporting other hardware is a matter of adding
a table (see ``docs/HARDWARE.md``), not of changing code. The player picks one
by ``input_profile`` in ``config.json`` (default ``gpi2``) and applies the
config's ``input_overrides`` on top.

This module names things with strings (``"K_RETURN"``, ``"select"``) and only
touches pygame to look a name up, so the tables can be read and tested
without a display.

Behaviour that is *not* configurable, on purpose: what each logical button does
in each screen, which buttons the bedtime sleep screen ignores, and that
``Escape`` quits. Those live in the views and the app.

Overrides (``config.json``)::

    "input_profile": "keyboard",
    "input_overrides": {
        "keys": {"K_F5": "repeat", "K_a": null},
        "joy_buttons": {"7": "playpause"},
        "volume_buttons": true
    }

A ``null`` value removes a binding. A key is a pygame constant name
(``"K_RETURN"``) or the bare name (``"return"``, ``"a"``). A bad entry is
logged and skipped: a typo must not stop a child's device from starting.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Final

import pygame

from .buttons import Button

logger = logging.getLogger(__name__)

DEFAULT_INPUT_PROFILE: Final = "gpi2"

_AXIS_THRESHOLD = 0.6
"""How far a stick must be pushed to count as a direction."""


@dataclass(frozen=True)
class InputProfile:
    """The bindings of one kind of hardware.

    Attributes:
        name: The profile's name, as written in ``config.json``.
        keys: pygame key constant name → logical button name.
        joy_buttons: Joystick button number → logical button name.
        hat: Whether the joystick hat (d-pad) moves the cursor.
        axes: Joystick axis number → ``"horizontal"`` or ``"vertical"``: a
            stick that also moves the cursor, for pads whose d-pad is an axis.
        volume_buttons: Whether the in-app volume buttons are on even when the
            child's profile leaves them off. For hardware with no volume dial
            (a plain keyboard); the volume cap still applies.
    """

    name: str
    keys: Mapping[str, str] = field(default_factory=dict)
    joy_buttons: Mapping[int, str] = field(default_factory=dict)
    hat: bool = True
    axes: Mapping[int, str] = field(default_factory=dict)
    volume_buttons: bool = False


# The reference hardware, and the bindings every install had before profiles
# existed: keep it exactly as it was.
_KEYS: Final[dict[str, str]] = {
    "K_UP": "up",
    "K_DOWN": "down",
    "K_LEFT": "left",
    "K_RIGHT": "right",
    "K_RETURN": "select",
    "K_a": "select",
    "K_b": "cancel",
    "K_x": "playpause",
    "K_y": "repeat",
    "K_SPACE": "playpause",
    "K_ESCAPE": "terminate",
    "K_BACKSPACE": "cancel",
    "K_EQUALS": "volume_up",
    "K_PLUS": "volume_up",
    "K_KP_PLUS": "volume_up",
    "K_MINUS": "volume_down",
    "K_KP_MINUS": "volume_down",
}
_PAD_BUTTONS: Final[dict[int, str]] = {
    0: "select",
    1: "cancel",
    2: "playpause",
    3: "repeat",
    # Shoulder buttons: act only while the volume buttons are on.
    4: "volume_down",
    5: "volume_up",
    # No "terminate" on a pad: the power button handles shutdown.
}

PROFILES: Final[dict[str, InputProfile]] = {
    p.name: p
    for p in (
        InputProfile("gpi2", keys=_KEYS, joy_buttons=_PAD_BUTTONS),
        # A computer keyboard: no pad, and no volume dial, so the in-app
        # volume keys are on by default.
        InputProfile("keyboard", keys=_KEYS, hat=False, volume_buttons=True),
        # Any SDL gamepad: the same face buttons as the GPi Case 2, plus the
        # left stick, since many pads have no hat.
        InputProfile(
            "generic-gamepad",
            keys=_KEYS,
            joy_buttons=_PAD_BUTTONS,
            axes={0: "horizontal", 1: "vertical"},
        ),
    )
}


def _key_code(name: str) -> int | None:
    """Look a key name up: ``K_RETURN``, ``return`` and ``a`` all work."""
    for candidate in (name, f"K_{name}", f"K_{name.upper()}"):
        value = getattr(pygame, candidate, None)
        if isinstance(value, int) and candidate.startswith("K_"):
            return value
    return None


def _button(name: object) -> Button | None:
    """Look a logical button up by its lower-case name."""
    if isinstance(name, str):
        try:
            return Button[name.upper()]
        except KeyError:
            return None
    return None


def resolve_profile(
    name: str, overrides: Mapping[str, object] | None = None
) -> InputProfile:
    """Find a profile by name and apply the config's overrides.

    Args:
        name: ``input_profile`` from the config.
        overrides: ``input_overrides`` from the config, or None.

    Returns:
        The profile. An unknown name falls back to ``gpi2`` (logged); bad
        override entries are skipped (logged).
    """
    profile = PROFILES.get(name)
    if profile is None:
        logger.warning(
            "Unknown input_profile %r (known: %s); using %s",
            name,
            ", ".join(sorted(PROFILES)),
            DEFAULT_INPUT_PROFILE,
        )
        profile = PROFILES[DEFAULT_INPUT_PROFILE]
    if not overrides:
        return profile

    keys = dict(profile.keys)
    joy = dict(profile.joy_buttons)
    volume = profile.volume_buttons

    raw_keys = overrides.get("keys", {})
    if isinstance(raw_keys, Mapping):
        for key, value in raw_keys.items():
            if _key_code(str(key)) is None or (
                value is not None and _button(value) is None
            ):
                logger.warning("Ignoring input override key %r: %r", key, value)
            elif value is None:
                keys.pop(str(key), None)
            else:
                keys[str(key)] = str(value).lower()
    raw_joy = overrides.get("joy_buttons", {})
    if isinstance(raw_joy, Mapping):
        for key, value in raw_joy.items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                number = -1
            if number < 0 or (value is not None and _button(value) is None):
                logger.warning("Ignoring input override button %r: %r", key, value)
            elif value is None:
                joy.pop(number, None)
            else:
                joy[number] = str(value).lower()
    if isinstance(overrides.get("volume_buttons"), bool):
        volume = bool(overrides["volume_buttons"])
    return replace(profile, keys=keys, joy_buttons=joy, volume_buttons=volume)


class InputMapper:
    """Turns pygame events into logical buttons for one profile.

    Args:
        profile: The bindings to use.
    """

    def __init__(self, profile: InputProfile) -> None:
        self.profile = profile
        self._keys: dict[int, Button] = {}
        for name, button in profile.keys.items():
            code, logical = _key_code(name), _button(button)
            if code is not None and logical is not None:
                self._keys[code] = logical
        self._joy: dict[int, Button] = {}
        for number, button in profile.joy_buttons.items():
            logical = _button(button)
            if logical is not None:
                self._joy[number] = logical
        # Last direction each stick axis was held in, so holding a stick sends
        # one press rather than one per event.
        self._axis_state: dict[int, int] = {}

    def map_event(self, event: pygame.event.Event) -> Button | None:
        """Map one pygame event.

        Args:
            event: Any pygame event.

        Returns:
            The logical button it presses, or None.
        """
        if event.type == pygame.KEYDOWN:
            return self._keys.get(event.key)
        if event.type == pygame.JOYBUTTONDOWN:
            return self._joy.get(event.button)
        if event.type == pygame.JOYHATMOTION and self.profile.hat:
            hx, hy = event.value
            if hy == 1:
                return Button.UP
            if hy == -1:
                return Button.DOWN
            if hx == -1:
                return Button.LEFT
            if hx == 1:
                return Button.RIGHT
        if event.type == pygame.JOYAXISMOTION:
            return self._map_axis(event.axis, event.value)
        return None

    def _map_axis(self, axis: int, value: float) -> Button | None:
        direction = self.profile.axes.get(axis)
        if direction is None:
            return None
        state = 1 if value > _AXIS_THRESHOLD else -1 if value < -_AXIS_THRESHOLD else 0
        if state == self._axis_state.get(axis, 0):
            return None
        self._axis_state[axis] = state
        if state == 0:
            return None
        if direction == "horizontal":
            return Button.RIGHT if state > 0 else Button.LEFT
        return Button.DOWN if state > 0 else Button.UP
