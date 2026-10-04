"""``kidsplay-player`` with and without a ``config.json``: only a *missing*
config starts pairing; an existing one, even a broken one, never does."""

import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from kidsplay_device import app
from kidsplay_device.config import DeviceConfig


@pytest.fixture
def player(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    """Stand in for the player and the pairing screen; return the player."""
    monkeypatch.setattr(app, "DEFAULT_CONFIG_PATH", tmp_path / "config.json")
    instance = MagicMock()
    monkeypatch.setattr(app, "MusicPlayerApp", MagicMock(return_value=instance))
    return instance


def write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "server_url": "http://s:8000",
                "device_id": "d",
                "api_key": "k",
                "media_root": str(path.parent / "media"),
                "db_path": str(path.parent / "db.sqlite"),
            }
        )
    )


def test_existing_config_never_pairs(
    player: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path / "config.json")
    pairing = MagicMock()
    monkeypatch.setattr("kidsplay_device.pairing_app.run_pairing", pairing)
    app.main([])
    pairing.assert_not_called()
    player.initialize.assert_called_once()
    player.run.assert_called_once()


def test_missing_config_pairs_then_runs(
    player: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_pairing(path: Path, **_: object) -> DeviceConfig:
        assert path == tmp_path / "config.json"
        write_config(path)
        return DeviceConfig.load(path)

    monkeypatch.setattr("kidsplay_device.pairing_app.run_pairing", fake_pairing)
    app.main([])
    player.initialize.assert_called_once()
    player.run.assert_called_once()


@pytest.mark.usefixtures("no_boot_preset")
def test_command_line_hardware_options_reach_pairing_and_the_paired_config(
    player: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config yet: --width/--height/--input-profile size the pairing screen
    and are then applied to the config it writes, like for an existing one."""
    seen: dict[str, object] = {}

    def fake_pairing(path: Path, **options: object) -> DeviceConfig:
        seen.update(options)
        write_config(path)
        return DeviceConfig.load(path)

    monkeypatch.setattr("kidsplay_device.pairing_app.run_pairing", fake_pairing)
    app.main(["--width", "800", "--height", "480", "--input-profile", "keyboard"])
    assert seen == {
        "width": 800,
        "height": 480,
        "input_profile": "keyboard",
        "start_url": None,
        "fullscreen": None,
    }
    config = cast("MagicMock", app.MusicPlayerApp).call_args.args[0]
    assert (config.width, config.height, config.input_profile) == (
        800,
        480,
        "keyboard",
    )


@pytest.mark.usefixtures("no_boot_preset")
def test_pairing_without_options_uses_the_defaults(
    player: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_pairing(path: Path, **options: object) -> DeviceConfig:
        seen.update(options)
        write_config(path)
        return DeviceConfig.load(path)

    monkeypatch.setattr("kidsplay_device.pairing_app.run_pairing", fake_pairing)
    app.main([])
    assert seen == {
        "width": None,
        "height": None,
        "input_profile": None,
        "start_url": None,
        "fullscreen": None,
    }


@pytest.mark.usefixtures("no_boot_preset")
def test_fullscreen_reaches_pairing_and_the_paired_config(
    player: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kiosk passes --fullscreen: the pairing screen fills the screen and
    so does the player started from the config pairing just wrote (which has no
    ``fullscreen`` key of its own)."""
    seen: dict[str, object] = {}

    def fake_pairing(path: Path, **options: object) -> DeviceConfig:
        seen.update(options)
        write_config(path)
        return DeviceConfig.load(path)

    monkeypatch.setattr("kidsplay_device.pairing_app.run_pairing", fake_pairing)
    app.main(["--fullscreen"])
    assert seen["fullscreen"] is True
    config = cast("MagicMock", app.MusicPlayerApp).call_args.args[0]
    assert config.fullscreen is True


def test_fullscreen_overrides_an_existing_config(
    player: MagicMock, tmp_path: Path
) -> None:
    write_config(tmp_path / "config.json")
    app.main(["--fullscreen"])
    assert cast("MagicMock", app.MusicPlayerApp).call_args.args[0].fullscreen is True


@pytest.mark.parametrize(("fullscreen", "expected"), [(True, True), (None, False)])
def test_the_pairing_window_is_fullscreen_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, fullscreen: bool | None, expected: bool
) -> None:
    import pygame

    from kidsplay_device import pairing_app

    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    flags: list[int] = []

    class Stop(Exception):
        pass

    def fake_set_mode(size: tuple[int, int], flag: int = 0) -> None:
        flags.append(flag)
        raise Stop

    monkeypatch.setattr(pygame.display, "set_mode", fake_set_mode)
    with pytest.raises(Stop):
        pairing_app.run_pairing(fullscreen=fullscreen, discover=False)
    assert bool(flags[0] & pygame.FULLSCREEN) is expected


def test_without_the_flag_the_config_decides(player: MagicMock, tmp_path: Path) -> None:
    write_config(tmp_path / "config.json")
    app.main([])
    assert cast("MagicMock", app.MusicPlayerApp).call_args.args[0].fullscreen is False


def test_closing_the_pairing_screen_exits_quietly(
    player: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kidsplay_device.pairing_app.run_pairing", lambda path, **_: None
    )
    app.main([])
    player.initialize.assert_not_called()


def test_broken_config_is_not_replaced(
    player: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.json").write_text("{ not json")
    pairing = MagicMock()
    monkeypatch.setattr("kidsplay_device.pairing_app.run_pairing", pairing)
    with pytest.raises(ValueError):
        app.main([])
    pairing.assert_not_called()
    assert (tmp_path / "config.json").read_text() == "{ not json"


def _capture_pairing(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the pairing screen; record what it was started with."""
    seen: dict[str, object] = {}

    def fake_pairing(path: Path, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr("kidsplay_device.pairing_app.run_pairing", fake_pairing)
    return seen


@pytest.fixture
def no_boot_preset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the boot-partition preset at a test file (absent unless written)."""
    boot = tmp_path / "boot-kidsplay-server.txt"
    monkeypatch.setattr("kidsplay_device.pairing.BOOT_PAIR_SERVER_FILE", boot)
    return boot


@pytest.mark.usefixtures("player")
def test_no_preset_shows_the_picker(
    monkeypatch: pytest.MonkeyPatch, no_boot_preset: Path
) -> None:
    seen = _capture_pairing(monkeypatch)
    app.main([])
    assert seen["start_url"] is None


@pytest.mark.usefixtures("player")
def test_preset_file_next_to_the_config_starts_pairing_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_boot_preset: Path
) -> None:
    (tmp_path / "pair-server.txt").write_text("https://kidsplay.example.net\n")
    seen = _capture_pairing(monkeypatch)
    app.main([])
    assert seen["start_url"] == "https://kidsplay.example.net"


@pytest.mark.usefixtures("player")
def test_boot_partition_preset_is_used(
    monkeypatch: pytest.MonkeyPatch, no_boot_preset: Path
) -> None:
    no_boot_preset.write_text("https://boot.example.net\n")
    seen = _capture_pairing(monkeypatch)
    app.main([])
    assert seen["start_url"] == "https://boot.example.net"


@pytest.mark.usefixtures("player")
def test_command_line_wins_over_a_preset_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_boot_preset: Path
) -> None:
    (tmp_path / "pair-server.txt").write_text("https://file.example.net\n")
    seen = _capture_pairing(monkeypatch)
    app.main(["--pair-server", "192.168.1.20"])
    assert seen["start_url"] == "http://192.168.1.20:8000"


def test_bad_pair_server_argument_is_refused() -> None:
    with pytest.raises(SystemExit):
        app.parse_args(["--pair-server", "not an address"])
