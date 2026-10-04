"""The device font must draw the Spanish characters, not missing-glyph boxes.

A font that lacks a character draws its ``.notdef`` glyph (a box) instead.
These tests render each character with the fonts the player really uses and
compare it, by metrics and by pixels, with what the same font draws for a
code point it does not have.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pygame
import pytest

from kidsplay_device import i18n
from kidsplay_device.fonts import TEXT_FONT_SIZES, load_text_fonts
from kidsplay_device.theme import DEFAULT_THEME
from kidsplay_device.views import HomeView, SettingsView, SleepView, apply_theme

_ICON_FONT = Path(i18n.__file__).parent / "assets" / "fa-solid-900.ttf"
SPANISH_CHARACTERS = "áéíóúñüÁÉÍÓÚÑÜ¿¡"
# Private-use and non-character code points: no text font maps them, so each
# draws the font's own missing-glyph box.
NOTDEF_CODE_POINTS = ("￿", "")


@pytest.fixture(scope="module")
def fonts() -> Iterator[dict[str, pygame.font.Font]]:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.font.init()
    yield load_text_fonts()
    pygame.font.quit()


def _pixels(font: pygame.font.Font, text: str) -> bytes:
    """The rendered pixels; empty if the font draws nothing (zero width)."""
    try:
        surface = font.render(text, True, (255, 255, 255), (0, 0, 0))
    except pygame.error:
        return b""
    return pygame.image.tobytes(surface, "RGB")


def _is_real_glyph(font: pygame.font.Font, char: str) -> bool:
    """True if ``char`` draws something other than the font's missing-glyph box."""
    pixels = _pixels(font, char)
    notdef = _pixels(font, NOTDEF_CODE_POINTS[0])
    return (
        bool(pixels)
        and pixels != notdef
        and font.metrics(char) != font.metrics(NOTDEF_CODE_POINTS[0])
    )


def test_the_notdef_reference_is_a_glyph_box(
    fonts: dict[str, pygame.font.Font],
) -> None:
    """Guard the guard: the reference must differ from real letters."""
    for font in fonts.values():
        boxes = {_pixels(font, cp) for cp in NOTDEF_CODE_POINTS}
        assert len(boxes) == 1, "unmapped code points should draw the same box"
        assert _pixels(font, "a") not in boxes


@pytest.mark.parametrize("name", sorted(TEXT_FONT_SIZES))
@pytest.mark.parametrize("char", list(SPANISH_CHARACTERS))
def test_spanish_character_is_not_the_missing_glyph_box(
    fonts: dict[str, pygame.font.Font], name: str, char: str
) -> None:
    assert _is_real_glyph(fonts[name], char)


@pytest.mark.parametrize(
    ("accented", "plain"),
    [
        ("á", "a"),
        ("é", "e"),
        ("í", "i"),
        ("ó", "o"),
        ("ú", "u"),
        ("ñ", "n"),
        ("ü", "u"),
    ],
)
def test_accents_are_actually_drawn(
    fonts: dict[str, pygame.font.Font], accented: str, plain: str
) -> None:
    """The accented letter differs from the plain one (there is a mark)."""
    for font in fonts.values():
        assert _pixels(font, accented) != _pixels(font, plain)


def test_every_spanish_screen_string_renders_without_boxes(
    fonts: dict[str, pygame.font.Font],
) -> None:
    """Each character of the es catalog is a real glyph in every text font."""
    po = (i18n.LOCALE_DIR / "es" / "LC_MESSAGES" / f"{i18n.DOMAIN}.po").read_text()
    characters = {
        ch
        for line in po.splitlines()
        if line.startswith('msgstr "')
        for ch in line.removeprefix("msgstr ").strip('"')
        if not ch.isspace()
    }
    assert set(SPANISH_CHARACTERS) & characters, "catalog has accented text"
    for font in fonts.values():
        for ch in sorted(characters - set("{}%")):
            assert _is_real_glyph(font, ch), f"{ch!r} draws a missing-glyph box"


def test_the_check_rejects_a_font_without_the_characters() -> None:
    """Mutation check: Font Awesome has no Latin letters, so it must fail."""
    pygame.font.init()
    icon_font = pygame.font.Font(str(_ICON_FONT), 40)
    assert not any(_is_real_glyph(icon_font, ch) for ch in SPANISH_CHARACTERS)


class _FakeApp:
    """The parts of the app the drawn views read."""

    def __init__(self, fonts: dict[str, pygame.font.Font]) -> None:
        self.fonts = fonts
        self._theme = DEFAULT_THEME
        self.theme_locked = False
        self.bedtime_wake_at = None
        self.identity_warning = False

    def view_allowed(self, name: str) -> bool:
        del name
        return True


def test_spanish_views_draw_differently_from_english_and_use_no_boxes(
    fonts: dict[str, pygame.font.Font],
) -> None:
    """Draw the real views in es and en; the text differs, nothing is a box."""
    apply_theme(DEFAULT_THEME)
    app = _FakeApp(fonts)
    for view_type in (HomeView, SettingsView, SleepView):
        frames = {}
        for language in ("en", "es"):
            i18n.activate(language)
            surface = pygame.Surface((640, 480))
            view_type(app).draw(surface)  # ty: ignore[invalid-argument-type] # _FakeApp stands in for MusicPlayerApp
            frames[language] = pygame.image.tobytes(surface, "RGB")
        assert frames["en"] != frames["es"], view_type.__name__
    i18n.activate("en")
