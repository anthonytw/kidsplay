"""Tests for the hardware keys of the device config."""

import json
from pathlib import Path

import pytest

from kidsplay_device.config import DeviceConfig


def base(tmp_path: Path) -> dict[str, object]:
    return {
        "server_url": "http://s",
        "device_id": "d",
        "api_key": "k",
        "media_root": str(tmp_path / "media"),
        "db_path": str(tmp_path / "db"),
    }


def load(tmp_path: Path, **extra: object) -> DeviceConfig:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**base(tmp_path), **extra}))
    return DeviceConfig.load(path)


def test_an_existing_config_means_the_reference_hardware(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert (cfg.width, cfg.height) == (640, 480)
    assert cfg.input_profile == "gpi2"
    assert cfg.input_overrides == {}


def test_hardware_keys_are_loaded(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        width=1280,
        height=720,
        input_profile="keyboard",
        input_overrides={"keys": {"K_F5": "repeat"}},
    )
    assert (cfg.width, cfg.height) == (1280, 720)
    assert cfg.input_profile == "keyboard"
    assert cfg.input_overrides == {"keys": {"K_F5": "repeat"}}


def test_blank_profile_and_malformed_overrides_fall_back(tmp_path: Path) -> None:
    cfg = load(tmp_path, input_profile="  ", input_overrides=["nope"])
    assert cfg.input_profile == "gpi2"
    assert cfg.input_overrides == {}


def test_save_writes_hardware_keys_only_when_they_differ(tmp_path: Path) -> None:
    plain = load(tmp_path)
    plain.save(tmp_path / "out.json")
    written = json.loads((tmp_path / "out.json").read_text())
    assert not {"width", "height", "input_profile", "input_overrides"} & set(written)

    custom = load(
        tmp_path,
        width=800,
        height=480,
        input_profile="generic-gamepad",
        input_overrides={"joy_buttons": {"7": "playpause"}},
    )
    custom.save(tmp_path / "out2.json")
    reloaded = DeviceConfig.load(tmp_path / "out2.json")
    assert (reloaded.width, reloaded.height) == (800, 480)
    assert reloaded.input_profile == "generic-gamepad"
    assert reloaded.input_overrides == {"joy_buttons": {"7": "playpause"}}


@pytest.mark.parametrize(("width", "height"), [(100, 480), (640, 100), (0, 0)])
def test_too_small_a_screen_is_refused_when_constructed(
    tmp_path: Path, width: int, height: int
) -> None:
    with pytest.raises(ValueError, match="at least 240x180"):
        DeviceConfig(
            server_url="http://s",
            device_id="d",
            api_key="k",
            media_root=tmp_path / "media",
            db_path=tmp_path / "db",
            width=width,
            height=height,
        )


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (100, 480),
        (640, 100),
        (0, 0),
        (-800, -480),
        (8193, 480),
        (800, 8193),
        (10**9, 10**9),
        ("wide", 480),
        (800, "tall"),
        (None, 480),
        (800, None),
        ([800], {"h": 480}),
        (True, 480),
        (float("inf"), 480),
        ("", ""),
    ],
)
def test_bad_screen_size_falls_back_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    width: object,
    height: object,
) -> None:
    """Regression: a bad width/height used to raise and kill the player."""
    with caplog.at_level("WARNING", logger="kidsplay_device.config"):
        cfg = load(tmp_path, width=width, height=height)
    assert (cfg.width, cfg.height) == (640, 480)
    assert "using 640x480" in caplog.text


def test_screen_size_at_the_upper_bound_is_accepted(tmp_path: Path) -> None:
    cfg = load(tmp_path, width=8192, height=8192)
    assert (cfg.width, cfg.height) == (8192, 8192)


def test_bad_screen_size_keeps_the_rest_of_the_config(tmp_path: Path) -> None:
    cfg = load(tmp_path, width="x", input_profile="keyboard", fullscreen=True)
    assert (cfg.width, cfg.height) == (640, 480)
    assert cfg.input_profile == "keyboard"
    assert cfg.fullscreen


def test_numeric_strings_and_floats_are_accepted(tmp_path: Path) -> None:
    cfg = load(tmp_path, width="800", height=480.0)
    assert (cfg.width, cfg.height) == (800, 480)


def test_good_screen_size_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="kidsplay_device.config"):
        load(tmp_path, width=240, height=180)
    assert caplog.text == ""
