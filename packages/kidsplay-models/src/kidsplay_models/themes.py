"""Themes: colors, and optional font, UI sounds and background images.

A theme is chosen per profile (``ProfileSettings.theme``) and reaches the
device in the sync manifest as a full ``ThemeDefinition``, so the device needs
no other source for it. Built-in themes are defined here, for both the server
(to list and validate them) and the device (to show them before its first
sync); a *custom* theme lives on the server, with its assets stored in the
content-addressed media store and synced like media.

Compatibility: every field beyond the palette has a default and unknown fields
are ignored, so an older device keeps working when a newer server adds one.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

THEME_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
"""Theme ids are short lowercase slugs: they appear in URLs and settings."""

DEFAULT_THEME_ID = "default"
"""The theme every profile has when none is chosen: the original look."""

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class ThemeAssetRole(StrEnum):
    """What a theme asset is for."""

    BACKGROUND = "background"
    """Image behind every screen except the home screen and the sleep screen."""
    HOME_BACKGROUND = "home_background"
    """Image behind the home screen; falls back to ``background``."""
    FONT = "font"
    """TrueType/OpenType font for the on-screen text."""
    SOUND_MOVE = "sound_move"
    """UI sound: the cursor moved."""
    SOUND_SELECT = "sound_select"
    """UI sound: something was chosen."""
    SOUND_BACK = "sound_back"
    """UI sound: went back."""
    SOUND_OPEN = "sound_open"
    """UI sound: opened a track, book or photo."""


class ThemeColors(BaseModel):
    """The palette every screen draws with, as ``#rrggbb`` strings.

    Attributes:
        bg: Full-screen background fill.
        surface: Card and row background.
        surface_sel: Selected card and row background.
        primary: Accent: highlights, progress bar, icons.
        text: Default list text.
        text_dim: Subdued text (artist, duration, labels).
        text_bright: Highlighted and selected text.
        accent: Secondary accent (repeat indicator, volume cap mark).
        progress_bg: Unfilled part of a progress bar.
    """

    model_config = ConfigDict(extra="ignore")

    bg: str
    surface: str
    surface_sel: str
    primary: str
    text: str
    text_dim: str
    text_bright: str
    accent: str
    progress_bg: str

    @field_validator("*")
    @classmethod
    def _hex(cls, value: str) -> str:
        if not _HEX_COLOR.match(value):
            raise ValueError(f"{value!r} is not a #rrggbb color")
        return value.lower()


class ThemeAsset(BaseModel):
    """One file of a theme, addressed by content like any synced file.

    The same file is listed in the manifest's ``files``, so the device's
    normal download and prune logic keeps it.
    """

    model_config = ConfigDict(extra="ignore")

    role: ThemeAssetRole
    content_hash: str = Field(description="SHA-256 of the file.")
    relative_path: str = Field(description="Path under the media root.")
    size_bytes: int = Field(ge=0)


class ThemeDefinition(BaseModel):
    """A complete theme.

    Attributes:
        id: Slug the profile setting refers to.
        name: Display name. Built-in names are English msgids that the web UI
            and the device translate; a custom name is shown as written.
        builtin: True for the themes shipped with KidsPlay.
        colors: The palette.
        assets: Font, sounds and background images, if any.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(pattern=THEME_ID_PATTERN)
    name: str = Field(min_length=1, max_length=40)
    builtin: bool = False
    colors: ThemeColors
    assets: list[ThemeAsset] = Field(default_factory=list)


def _theme(theme_id: str, name: str, **colors: str) -> ThemeDefinition:
    return ThemeDefinition(
        id=theme_id, name=name, builtin=True, colors=ThemeColors(**colors)
    )


# The dark base of the original themes: only surface, primary and accent vary.
_DARK = {
    "bg": "#121218",
    "text": "#dcdee6",
    "text_dim": "#787c8c",
    "text_bright": "#ffffff",
    "progress_bg": "#303444",
}

BUILTIN_THEMES: tuple[ThemeDefinition, ...] = (
    # "default" is the look every device had before themes existed.
    _theme(
        DEFAULT_THEME_ID,
        "Blue",
        surface="#20222a",
        surface_sel="#343a4e",
        primary="#6a99e5",
        accent="#ffb43c",
        **_DARK,
    ),
    _theme(
        "purple",
        "Purple",
        surface="#201c2a",
        surface_sel="#3a2a50",
        primary="#aa5ff5",
        accent="#ffb43c",
        **_DARK,
    ),
    _theme(
        "green",
        "Green",
        surface="#18241c",
        surface_sel="#224432",
        primary="#48c873",
        accent="#ffc832",
        **_DARK,
    ),
    _theme(
        "red",
        "Red",
        surface="#26161a",
        surface_sel="#52202c",
        primary="#eb4860",
        accent="#ffc850",
        **_DARK,
    ),
    _theme(
        "orange",
        "Orange",
        surface="#261e14",
        surface_sel="#54361a",
        primary="#f08a36",
        accent="#64beff",
        **_DARK,
    ),
    _theme(
        "cyan",
        "Cyan",
        surface="#142224",
        surface_sel="#16464c",
        primary="#34c4c4",
        accent="#ff9b4b",
        **_DARK,
    ),
    # Pure black and white with saturated accents: every text/background pair
    # is at least WCAG AAA (7:1); tests/test_themes.py holds the numbers.
    _theme(
        "high-contrast",
        "High contrast",
        bg="#000000",
        surface="#1a1a1a",
        surface_sel="#0033cc",
        primary="#ffee00",
        text="#ffffff",
        text_dim="#d0d0d0",
        text_bright="#ffffff",
        accent="#00e5ff",
        progress_bg="#4a4a4a",
    ),
    # Dim and warm, no bright blue: for bedtime reading in a dark room.
    _theme(
        "night",
        "Night",
        bg="#0b0a12",
        surface="#17141f",
        surface_sel="#2a2338",
        primary="#e8a860",
        text="#cfc6bd",
        text_dim="#948a82",
        text_bright="#f3e9dc",
        accent="#7fa0d8",
        progress_bg="#2a2532",
    ),
)

BUILTIN_THEMES_BY_ID: dict[str, ThemeDefinition] = {t.id: t for t in BUILTIN_THEMES}


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` color.

    Args:
        color: A ``#rrggbb`` string.

    Returns:
        A value from 0 (black) to 1 (white).
    """

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colors.

    Args:
        foreground: One color.
        background: The other color.

    Returns:
        A ratio from 1 (identical) to 21 (black on white).
    """
    a, b = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)
