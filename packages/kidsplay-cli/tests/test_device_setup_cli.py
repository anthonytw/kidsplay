"""``kidsplay device setup``: the config file it writes for the player."""

import json

from click.testing import CliRunner

from kidsplay_cli.main import cli


def setup(*extra: str) -> dict[str, object]:
    result = CliRunner().invoke(
        cli,
        [
            "--server",
            "http://s",
            "device",
            "setup",
            "--device-id",
            "d",
            "--api-key",
            "k",
            *extra,
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    config: dict[str, object] = json.loads(result.stdout)
    return config


def test_default_size_leaves_the_config_as_it_was() -> None:
    config = setup()
    assert "width" not in config and "height" not in config
    assert config["device_id"] == "d"


def test_another_size_is_written_for_the_player() -> None:
    config = setup("--width", "800", "--height", "480")
    assert (config["width"], config["height"]) == (800, 480)


def test_one_changed_dimension_writes_both() -> None:
    config = setup("--height", "720")
    assert (config["width"], config["height"]) == (640, 720)
