"""Themes for the KidsPlay device player.

A ``Theme`` is what the views draw with: the palette, and optionally a font,
UI sounds and background images. The definitions come from
``kidsplay_models.themes`` (the built-in ones are shipped there so the device
has them before its first sync; a custom theme arrives in the sync manifest
with its asset files, which the sync stores under ``media_root``).

Which theme is shown:

1. The profile's theme, when the profile chose one (``ProfileSettings.theme``).
   It is authoritative: the on-device color picker is disabled. If the device
   cannot resolve it (a theme id it has no definition for) it shows the
   default theme.
2. Otherwise the color the child picked on the device, else the default.

Assets are best-effort: a missing or unreadable file drops that one asset, and
the default font, sounds or plain background stay. A broken theme never stops
the player.

Usage::

    from .theme import THEMES, DEFAULT_THEME
    from .views import apply_theme

    apply_theme(THEMES[2])   # switch to green
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kidsplay_models import (
    BUILTIN_THEMES,
    BUILTIN_THEMES_BY_ID,
    DEFAULT_THEME_ID,
    ThemeAssetRole,
    ThemeDefinition,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

Color = tuple[int, int, int]

#: UI sound roles → the names the app uses for them.
SOUND_ROLES: dict[ThemeAssetRole, str] = {
    ThemeAssetRole.SOUND_MOVE: "move",
    ThemeAssetRole.SOUND_SELECT: "select",
    ThemeAssetRole.SOUND_BACK: "back",
    ThemeAssetRole.SOUND_OPEN: "open",
}


@dataclass(frozen=True)
class ThemeAssets:
    """The asset files of a theme that are present on this device.

    Attributes:
        background: Image behind the screens, or None.
        home_background: Image behind the home screen, or None.
        font: Font file for the text, or None for the built-in font.
        sounds: UI sound files by name (``move``, ``select``, ``back``,
            ``open``); a missing name keeps the built-in sound.
    """

    background: Path | None = None
    home_background: Path | None = None
    font: Path | None = None
    sounds: Mapping[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class Theme:
    """Complete color palette (and assets) for one visual theme.

    Args:
        name: Display name; for the six original colors, the Spanish name
            that ``settings.json`` has always stored.
        BG: Full-screen background fill.
        SURFACE: Card / row background.
        SURFACE_SEL: Selected card / row background.
        PRIMARY: Accent color — highlights, progress bar, icons.
        TEXT: Default list text.
        TEXT_DIM: Subdued text (artist, duration, labels).
        TEXT_BRIGHT: Highlighted / selected text.
        ACCENT: Secondary accent (repeat indicator, badges).
        PROGRESS_BG: Unfilled portion of progress bar.
        theme_id: The definition's id (empty for a hand-made palette).
        assets: Font, sounds and backgrounds, if the theme has any.
    """

    name: str
    BG: Color
    SURFACE: Color
    SURFACE_SEL: Color
    PRIMARY: Color
    TEXT: Color
    TEXT_DIM: Color
    TEXT_BRIGHT: Color
    ACCENT: Color
    PROGRESS_BG: Color
    theme_id: str = ""
    assets: ThemeAssets = field(default_factory=ThemeAssets)

    @property
    def PROGRESS_FG(self) -> Color:
        """Progress fill — same as PRIMARY."""
        return self.PRIMARY


def _rgb(color: str) -> Color:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


#: ``Theme.name`` of the original six colors: what ``settings.json`` stored
#: before themes had ids, so a child's choice survives the upgrade.
_LEGACY_NAMES: dict[str, str] = {
    "default": "Azul",
    "purple": "Morado",
    "green": "Verde",
    "red": "Rojo",
    "orange": "Naranja",
    "cyan": "Cian",
}


def theme_from_definition(
    definition: ThemeDefinition, media_root: Path | None = None
) -> Theme:
    """Build a ``Theme`` from a definition.

    Args:
        definition: A built-in or synced theme.
        media_root: Where asset files were synced to. None means the
            definition's assets are not looked for (built-ins have none).

    Returns:
        The theme. Assets whose file is missing are left out.
    """
    c = definition.colors
    background = home_background = font = None
    sounds: dict[str, Path] = {}
    if media_root is not None:
        for asset in definition.assets:
            path = media_root / asset.relative_path
            if not path.is_file():
                logger.warning(
                    "Theme %s: %s is not on this device yet (%s)",
                    definition.id,
                    asset.role.value,
                    asset.relative_path,
                )
                continue
            if asset.role is ThemeAssetRole.BACKGROUND:
                background = path
            elif asset.role is ThemeAssetRole.HOME_BACKGROUND:
                home_background = path
            elif asset.role is ThemeAssetRole.FONT:
                font = path
            elif asset.role in SOUND_ROLES:
                sounds[SOUND_ROLES[asset.role]] = path
    return Theme(
        _LEGACY_NAMES.get(definition.id, definition.name),
        BG=_rgb(c.bg),
        SURFACE=_rgb(c.surface),
        SURFACE_SEL=_rgb(c.surface_sel),
        PRIMARY=_rgb(c.primary),
        TEXT=_rgb(c.text),
        TEXT_DIM=_rgb(c.text_dim),
        TEXT_BRIGHT=_rgb(c.text_bright),
        ACCENT=_rgb(c.accent),
        PROGRESS_BG=_rgb(c.progress_bg),
        theme_id=definition.id,
        assets=ThemeAssets(background, home_background, font, sounds),
    )


#: The built-in themes, in the order the on-device picker cycles through them.
THEMES: list[Theme] = [theme_from_definition(d) for d in BUILTIN_THEMES]

#: Default theme used on first launch: the original blue.
DEFAULT_THEME: Theme = next(t for t in THEMES if t.theme_id == DEFAULT_THEME_ID)

#: Lookup by name for persistence.
THEMES_BY_NAME: dict[str, Theme] = {t.name: t for t in THEMES}


def resolve_profile_theme(
    theme_id: str | None,
    synced: ThemeDefinition | None,
    media_root: Path,
) -> Theme | None:
    """The theme the profile dictates, if it dictates one.

    Args:
        theme_id: ``ProfileSettings.theme``.
        synced: The theme definition from the last sync, if any.
        media_root: Where synced asset files live.

    Returns:
        None when the profile chose no theme (the child's own choice
        applies). Otherwise the synced definition when it is the chosen one,
        else the built-in of that id, else the default theme.
    """
    if theme_id is None:
        return None
    if synced is not None and synced.id == theme_id:
        return theme_from_definition(synced, media_root)
    builtin = BUILTIN_THEMES_BY_ID.get(theme_id)
    if builtin is not None:
        return theme_from_definition(builtin)
    logger.warning("Unknown theme %r; showing the default theme", theme_id)
    return DEFAULT_THEME
