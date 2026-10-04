"""Tests for the ``just demo`` entry point (``demo/__main__.py``).

The server, seeding, sync and player are all mocked; ``test_seed.py`` covers
the real seeding against the app.
"""

import contextlib
import signal
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import demo.__main__ as demo_main
from demo.seed import DemoDevice


def test_parse_args_defaults() -> None:
    args = demo_main.parse_args([])
    assert args.port is None
    assert args.keep is False
    assert args.no_player is False


def test_parse_args_options() -> None:
    args = demo_main.parse_args(["--port", "9000", "--keep", "--no-player"])
    assert (args.port, args.keep, args.no_player) == (9000, True, True)


def test_player_command_runs_the_player_entry_point() -> None:
    cmd = demo_main.player_command()
    assert "kidsplay_device.app" in cmd[-1]


@dataclass
class Mocks:
    """The mocked collaborators of ``run`` and the temp dir it will use."""

    data_dir: Path
    seed: MagicMock
    sync: MagicMock
    run: MagicMock
    ports: list[int | None] = field(default_factory=list)


@pytest.fixture
def mocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mocks:
    """Mock the server, seeding, sync and child processes; data in tmp_path."""
    mocks = Mocks(
        data_dir=tmp_path / "demo-data",
        seed=MagicMock(return_value=DemoDevice("dev", "key", {"Ada": "p"})),
        sync=MagicMock(return_value=52),
        run=MagicMock(return_value=MagicMock(returncode=0)),
    )
    mocks.data_dir.mkdir()

    @contextlib.contextmanager
    def fake_server(_: Path, port: int | None = None) -> Iterator[str]:
        mocks.ports.append(port)
        yield "http://127.0.0.1:1234"

    monkeypatch.setattr(
        demo_main.tempfile, "mkdtemp", lambda prefix: str(mocks.data_dir)
    )
    monkeypatch.setattr(demo_main, "running_server", fake_server)
    monkeypatch.setattr(demo_main, "login", MagicMock())
    monkeypatch.setattr(demo_main, "seed", mocks.seed)
    monkeypatch.setattr(demo_main, "sync_device", mocks.sync)
    monkeypatch.setattr(demo_main.subprocess, "run", mocks.run)
    monkeypatch.setattr(demo_main.signal, "signal", MagicMock())
    monkeypatch.setattr(demo_main.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    return mocks


def test_run_launches_player_with_demo_home(mocked: Mocks) -> None:
    data_dir = mocked.data_dir
    code = demo_main.run(demo_main.parse_args(["--port", "8001"]))

    assert code == 0
    assert mocked.ports == [8001]
    mocked.seed.assert_called_once()
    cmd = mocked.run.call_args.args[0]
    env = mocked.run.call_args.kwargs["env"]
    assert cmd == demo_main.player_command()
    assert env["HOME"] == str(data_dir / "device-home")
    # The device config was written where the player will look for it...
    assert not data_dir.exists()  # ...and everything was cleaned up after.


def test_run_keep_leaves_data(mocked: Mocks) -> None:
    data_dir = mocked.data_dir
    demo_main.run(demo_main.parse_args(["--keep"]))
    assert (data_dir / "device-home" / ".kidsplay" / "config.json").exists()


def test_run_returns_player_exit_code(mocked: Mocks) -> None:
    mocked.run.return_value = MagicMock(returncode=3)
    assert demo_main.run(demo_main.parse_args([])) == 3


def test_run_no_player_waits_for_ctrl_c(
    mocked: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        demo_main.time, "sleep", MagicMock(side_effect=KeyboardInterrupt)
    )
    assert demo_main.run(demo_main.parse_args(["--no-player"])) == 0
    mocked.run.assert_not_called()
    # A second Ctrl-C (``just`` forwards SIGINT too) can't interrupt cleanup.
    set_handler = demo_main.signal.signal
    assert isinstance(set_handler, MagicMock)  # patched by the fixture
    set_handler.assert_any_call(signal.SIGINT, signal.SIG_IGN)
    assert not mocked.data_dir.exists()


def test_run_warns_without_ffmpeg(
    mocked: Mocks,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(demo_main.shutil, "which", lambda _: None)
    demo_main.run(demo_main.parse_args([]))
    assert "ffmpeg is not on PATH" in capsys.readouterr().err


def test_main_exits_with_run_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo_main, "run", lambda _: 7)
    with pytest.raises(SystemExit) as exc:
        demo_main.main([])
    assert exc.value.code == 7
