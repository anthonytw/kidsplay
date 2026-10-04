"""Text fonts for the on-screen UI.

pygame's built-in default font (FreeSansBold) covers Latin-1 and Latin
Extended-A, so it renders the Spanish á é í ó ú ñ ü ¿ ¡ without a bundled
font. ``tests/test_i18n_fonts.py`` proves that against the font's own
missing-glyph box, and fails if a future pygame drops the coverage.

A theme may bring its own font. It replaces the default for the text styles
only (never the icon font), and any failure to load it keeps the default. A
corrupt font file is only detected by drawing with it, so ``_load`` does that
once, up front.
"""

import logging
from pathlib import Path

import pygame

logger = logging.getLogger(__name__)

#: Font size in pixels for each named text style, on the 640×480 design.
TEXT_FONT_SIZES: dict[str, int] = {
    "small": 30,
    "medium": 40,
    "large": 50,
    "folder": 50,
}


#: Smallest font size, in pixels. pygame's default font at 15 px is 10 px tall,
#: which is about as small as text stays readable on a handheld's LCD; on the
#: smallest supported screen (240×180) the styles would otherwise shrink to 8 px.
MIN_FONT_PX = 15


def load_text_fonts(
    scale: float = 1.0, font_path: Path | None = None
) -> dict[str, pygame.font.Font]:
    """Create the text fonts. ``pygame.font`` must be initialised.

    Args:
        scale: Factor from the 640×480 design to the screen (``Layout.scale``).
        font_path: A theme's font file, or None for the built-in font.

    Returns:
        One font per name in ``TEXT_FONT_SIZES``, sized for the screen.
    """
    fonts: dict[str, pygame.font.Font] = {}
    for name, size in TEXT_FONT_SIZES.items():
        px = max(MIN_FONT_PX, round(size * scale))
        fonts[name] = _load(font_path, px)
    return fonts


def _load(font_path: Path | None, px: int) -> pygame.font.Font:
    if font_path is not None:
        try:
            font = pygame.font.Font(str(font_path), px)
            # pygame opens a corrupt file without complaint and then crashes
            # (or raises) on first use, so prove the font works before any
            # view relies on it.
            font.render("Ag", True, (0, 0, 0))
            return font
        except (OSError, ValueError, pygame.error) as exc:
            logger.warning("Could not load theme font %s: %s", font_path, exc)
    return pygame.font.Font(None, px)
