"""Tests for device themes: the built-ins, definitions, and profile resolution."""

import logging
from pathlib import Path

import pygame
import pytest

from kidsplay_device.fonts import TEXT_FONT_SIZES, load_text_fonts
from kidsplay_device.theme import (
    DEFAULT_THEME,
    THEMES,
    THEMES_BY_NAME,
    resolve_profile_theme,
    theme_from_definition,
)
from kidsplay_models import (
    BUILTIN_THEMES,
    ThemeAsset,
    ThemeAssetRole,
    ThemeDefinition,
)

# Theme.name of the original six colors as settings.json stored them.
LEGACY_NAMES = ["Azul", "Morado", "Verde", "Rojo", "Naranja", "Cian"]


def custom(tmp_path: Path, *roles: ThemeAssetRole) -> ThemeDefinition:
    base = BUILTIN_THEMES[0]
    assets = []
    for role in roles:
        rel = f"themes/aa/{role.value}.bin"
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"x")
        assets.append(
            ThemeAsset(
                role=role, content_hash="a" * 64, relative_path=rel, size_bytes=1
            )
        )
    return base.model_copy(update={"id": "mine", "name": "Mine", "assets": assets})


def test_original_colors_keep_their_persisted_names_and_order() -> None:
    assert [t.name for t in THEMES[:6]] == LEGACY_NAMES
    assert DEFAULT_THEME.name == "Azul"
    assert THEMES_BY_NAME["Azul"] is DEFAULT_THEME


def test_new_builtins_are_in_the_picker() -> None:
    assert [t.theme_id for t in THEMES] == [t.id for t in BUILTIN_THEMES]
    assert {"high-contrast", "night"} <= {t.theme_id for t in THEMES}


def test_default_theme_is_the_original_look() -> None:
    """The exact RGB values the palette had before themes."""
    t = DEFAULT_THEME
    assert t.BG == (18, 18, 24)
    assert t.SURFACE == (32, 34, 42)
    assert t.SURFACE_SEL == (52, 58, 78)
    assert t.PRIMARY == (106, 153, 229)
    assert t.TEXT == (220, 222, 230)
    assert t.TEXT_DIM == (120, 124, 140)
    assert t.TEXT_BRIGHT == (255, 255, 255)
    assert t.ACCENT == (255, 180, 60)
    assert t.PROGRESS_BG == (48, 52, 68)
    assert t.PROGRESS_FG == t.PRIMARY
    assert t.assets.background is None and t.assets.font is None


def test_assets_that_are_on_disk_are_resolved(tmp_path: Path) -> None:
    definition = custom(
        tmp_path,
        ThemeAssetRole.BACKGROUND,
        ThemeAssetRole.HOME_BACKGROUND,
        ThemeAssetRole.FONT,
        ThemeAssetRole.SOUND_MOVE,
        ThemeAssetRole.SOUND_OPEN,
    )
    theme = theme_from_definition(definition, tmp_path)
    assert theme.name == "Mine"
    assert theme.assets.background == tmp_path / "themes/aa/background.bin"
    assert theme.assets.home_background == tmp_path / "themes/aa/home_background.bin"
    assert theme.assets.font == tmp_path / "themes/aa/font.bin"
    assert set(theme.assets.sounds) == {"move", "open"}


def test_missing_assets_are_dropped_not_fatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    definition = custom(tmp_path, ThemeAssetRole.FONT, ThemeAssetRole.SOUND_BACK)
    (tmp_path / "themes/aa/font.bin").unlink()
    with caplog.at_level(logging.WARNING):
        theme = theme_from_definition(definition, tmp_path)
    assert theme.assets.font is None
    assert "back" in theme.assets.sounds
    assert "font" in caplog.text


def test_without_a_media_root_assets_are_not_looked_for(tmp_path: Path) -> None:
    definition = custom(tmp_path, ThemeAssetRole.FONT)
    assert theme_from_definition(definition).assets.font is None


class TestResolveProfileTheme:
    def test_no_choice_leaves_it_to_the_child(self, tmp_path: Path) -> None:
        assert resolve_profile_theme(None, None, tmp_path) is None
        assert resolve_profile_theme(None, BUILTIN_THEMES[1], tmp_path) is None

    def test_builtin_id(self, tmp_path: Path) -> None:
        theme = resolve_profile_theme("night", None, tmp_path)
        assert theme is not None and theme.theme_id == "night"

    def test_synced_definition_wins_for_its_id(self, tmp_path: Path) -> None:
        definition = custom(tmp_path, ThemeAssetRole.FONT)
        theme = resolve_profile_theme("mine", definition, tmp_path)
        assert theme is not None
        assert theme.assets.font is not None

    def test_a_synced_definition_of_another_id_is_not_used(
        self, tmp_path: Path
    ) -> None:
        definition = custom(tmp_path)
        theme = resolve_profile_theme("night", definition, tmp_path)
        assert theme is not None and theme.theme_id == "night"

    def test_unknown_id_falls_back_to_the_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            theme = resolve_profile_theme("from-the-future", None, tmp_path)
        assert theme is DEFAULT_THEME
        assert "from-the-future" in caplog.text


class TestFonts:
    @pytest.fixture(autouse=True)
    def _pygame_fonts(self) -> None:
        pygame.font.init()

    def test_sizes_scale_with_the_screen(self) -> None:
        reference = load_text_fonts(1.0)
        half = load_text_fonts(0.5)
        assert set(reference) == set(TEXT_FONT_SIZES) == set(half)
        for name in reference:
            assert half[name].get_height() < reference[name].get_height()

    def test_a_theme_font_replaces_the_default(self) -> None:
        font_file = (
            Path(__file__).parents[1] / "src/kidsplay_device/assets/fa-solid-900.ttf"
        )
        default = load_text_fonts(1.0)["small"]
        themed = load_text_fonts(1.0, font_file)["small"]
        assert themed.render("A", True, (255, 255, 255)).get_size() != (
            default.render("A", True, (255, 255, 255)).get_size()
        )

    @pytest.mark.parametrize(
        "garbage",
        [b"not a font", b"\0" * 4096, b"\x00\x01\x00\x00" + b"\0" * 100, b""],
        ids=["text", "zeros", "ttf-magic-only", "empty"],
    )
    def test_a_broken_font_keeps_the_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, garbage: bytes
    ) -> None:
        broken = tmp_path / "broken.ttf"
        broken.write_bytes(garbage)
        with caplog.at_level(logging.WARNING):
            fonts = load_text_fonts(1.0, broken)
        assert fonts["small"].get_height() == load_text_fonts(1.0)["small"].get_height()
        assert "broken.ttf" in caplog.text
