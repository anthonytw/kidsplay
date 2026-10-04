"""Tests for the shared theme models."""

import pytest
from pydantic import ValidationError

from kidsplay_models import (
    BUILTIN_THEMES,
    BUILTIN_THEMES_BY_ID,
    DEFAULT_THEME_ID,
    ProfileSettings,
    SyncManifest,
    ThemeAsset,
    ThemeAssetRole,
    ThemeColors,
    ThemeDefinition,
    contrast_ratio,
)

# The palettes the device shipped before themes existed, as RGB tuples. The
# built-in themes must reproduce them exactly: existing profiles look the same.
_LEGACY: dict[str, dict[str, tuple[int, int, int]]] = {
    "default": dict(
        surface=(32, 34, 42), surface_sel=(52, 58, 78),
        primary=(106, 153, 229), accent=(255, 180, 60),
    ),
    "purple": dict(
        surface=(32, 28, 42), surface_sel=(58, 42, 80),
        primary=(170, 95, 245), accent=(255, 180, 60),
    ),
    "green": dict(
        surface=(24, 36, 28), surface_sel=(34, 68, 50),
        primary=(72, 200, 115), accent=(255, 200, 50),
    ),
    "red": dict(
        surface=(38, 22, 26), surface_sel=(82, 32, 44),
        primary=(235, 72, 96), accent=(255, 200, 80),
    ),
    "orange": dict(
        surface=(38, 30, 20), surface_sel=(84, 54, 26),
        primary=(240, 138, 54), accent=(100, 190, 255),
    ),
    "cyan": dict(
        surface=(20, 34, 36), surface_sel=(22, 70, 76),
        primary=(52, 196, 196), accent=(255, 155, 75),
    ),
}  # fmt: skip
_LEGACY_BASE = dict(
    bg=(18, 18, 24),
    text=(220, 222, 230),
    text_dim=(120, 124, 140),
    text_bright=(255, 255, 255),
    progress_bg=(48, 52, 68),
)


def _rgb(color: str) -> tuple[int, int, int]:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


def test_builtin_ids_are_unique_and_include_the_default() -> None:
    ids = [t.id for t in BUILTIN_THEMES]
    assert len(ids) == len(set(ids))
    assert DEFAULT_THEME_ID in BUILTIN_THEMES_BY_ID
    assert {"default", "high-contrast", "night"} <= set(ids)
    assert all(t.builtin for t in BUILTIN_THEMES)


@pytest.mark.parametrize("theme_id", sorted(_LEGACY))
def test_legacy_palettes_are_reproduced_exactly(theme_id: str) -> None:
    colors = BUILTIN_THEMES_BY_ID[theme_id].colors
    expected = {**_LEGACY_BASE, **_LEGACY[theme_id]}
    assert {k: _rgb(getattr(colors, k)) for k in expected} == expected


@pytest.mark.parametrize("theme", BUILTIN_THEMES, ids=lambda t: t.id)
def test_text_is_readable_on_every_surface(theme: ThemeDefinition) -> None:
    c = theme.colors
    assert contrast_ratio(c.text, c.bg) >= 4.5
    assert contrast_ratio(c.text, c.surface) >= 4.5
    assert contrast_ratio(c.text_dim, c.surface) >= 3.0
    assert contrast_ratio(c.text_bright, c.surface_sel) >= 4.5
    assert contrast_ratio(c.primary, c.surface) >= 3.0


def test_high_contrast_meets_aaa() -> None:
    c = BUILTIN_THEMES_BY_ID["high-contrast"].colors
    for fg, bg in (
        (c.text, c.bg),
        (c.text, c.surface),
        (c.text_dim, c.surface),
        (c.text_bright, c.surface_sel),
        (c.primary, c.surface),
        (c.primary, c.surface_sel),
    ):
        assert contrast_ratio(fg, bg) >= 7.0, (fg, bg)


def test_contrast_ratio_extremes() -> None:
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast_ratio("#123456", "#123456") == pytest.approx(1.0)


def test_colors_are_validated_and_normalised() -> None:
    base = {k: "#000000" for k in ThemeColors.model_fields}
    assert ThemeColors(**{**base, "bg": "#ABCDEF"}).bg == "#abcdef"
    for bad in ("red", "#fff", "#gggggg", "123456"):
        with pytest.raises(ValidationError):
            ThemeColors(**{**base, "bg": bad})


def test_theme_id_must_be_a_slug() -> None:
    colors = BUILTIN_THEMES[0].colors
    ThemeDefinition(id="my-theme-2", name="Mine", colors=colors)
    for bad in ("", "Has Space", "UPPER", "-lead", "a" * 41, "../x"):
        with pytest.raises(ValidationError):
            ThemeDefinition(id=bad, name="x", colors=colors)


def test_unknown_fields_are_ignored() -> None:
    data = BUILTIN_THEMES[0].model_dump(mode="json")
    data["new_field"] = 1
    data["assets"] = [
        {
            "role": "font",
            "content_hash": "a" * 64,
            "relative_path": "themes/aa/a.ttf",
            "size_bytes": 3,
            "future": True,
        }
    ]
    theme = ThemeDefinition.model_validate(data)
    assert theme.assets == [
        ThemeAsset(
            role=ThemeAssetRole.FONT,
            content_hash="a" * 64,
            relative_path="themes/aa/a.ttf",
            size_bytes=3,
        )
    ]


def test_profile_settings_theme_defaults_to_none_and_round_trips() -> None:
    assert ProfileSettings().theme is None
    assert ProfileSettings.model_validate({"max_volume": 30}).theme is None
    settings = ProfileSettings(theme="night")
    assert ProfileSettings.model_validate_json(settings.model_dump_json()).theme == (
        "night"
    )


def test_manifest_theme_is_optional_and_round_trips() -> None:
    base = {
        "device_id": "6f0c9c5e-2d3a-4d0e-9f0a-1a2b3c4d5e6f",
        "profile_id": "0b9a0c5e-2d3a-4d0e-9f0a-1a2b3c4d5e6f",
        "manifest_hash": "h",
    }
    assert SyncManifest.model_validate(base).theme is None
    theme = BUILTIN_THEMES_BY_ID["night"]
    manifest = SyncManifest.model_validate({**base, "theme": theme.model_dump()})
    assert manifest.theme == theme
