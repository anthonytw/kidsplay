"""The kiosk installer and the clock bootstrap in all-in-one mode.

The server is the same machine, so its ``Date`` header is the device's own
clock and ``kidsplay-timeset`` is meaningless. These tests run the real
installer with ``sudo``, ``systemctl`` and friends replaced by stubs that log
their arguments, so nothing on the test machine is changed.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "deploy" / "install-kiosk.sh"

STUBS = {
    # `sudo cmd args...`: log it, run nothing, swallow any heredoc on stdin.
    "sudo": 'echo "sudo $*" >> "$STUB_LOG"; cat >/dev/null\n',
    "labwc": "exit 0\n",
    # The installer refuses root; report an ordinary user whoever runs the test.
    "id": 'case "$1" in -u) echo 1000;; -un) echo tester;; *) echo uid=1000;; esac\n',
    "timedatectl": 'case "$1" in show) echo Europe/Berlin;; esac\n',
}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A sandboxed HOME, stubbed system commands and the log they write."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    app = tmp_path / "kidsplay-player"
    app.write_text("#!/bin/sh\n")
    app.chmod(0o755)
    home = tmp_path / "home"
    (home / ".kidsplay").mkdir(parents=True)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "STUB_LOG": str(tmp_path / "stub.log"),
        "KIDSPLAY_APP_CMD": str(app),
    }


def install(env: dict[str, str], config: dict[str, object] | None, *args: str) -> str:
    """Run the installer with ``config`` as config.json; return the sudo log."""
    config_path = Path(env["HOME"]) / ".kidsplay" / "config.json"
    if config is not None:
        config_path.write_text(json.dumps(config))
    result = subprocess.run(
        ["bash", str(INSTALLER), *args],
        env={**env, "KIDSPLAY_CONFIG": str(config_path)},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    log = Path(env["STUB_LOG"])
    return log.read_text() if log.exists() else ""


HTTP_CONFIG: dict[str, object] = {
    "server_url": "http://nas.local:8000",
    "sync_transport": "http",
}
LOCAL_CONFIG: dict[str, object] = {
    "server_url": "http://127.0.0.1:8000",
    "sync_transport": "local",
}


def test_local_config_skips_the_clock_bootstrap(env: dict[str, str]) -> None:
    log = install(env, LOCAL_CONFIG)
    assert "install -m755 /dev/stdin /usr/local/sbin/kidsplay-timeset.sh" not in log
    assert "enable --now kidsplay-timeset.timer" not in log
    # ...but the kiosk itself is installed as usual.
    assert "set-default multi-user.target" in log
    assert (Path(env["HOME"]) / ".local/bin/kidsplay-kiosk.sh").exists()


def test_local_config_removes_a_bootstrap_left_by_an_earlier_install(
    env: dict[str, str],
) -> None:
    log = install(env, LOCAL_CONFIG)
    assert "disable --now kidsplay-timeset.timer kidsplay-timeset.service" in log
    assert "rm -f /etc/systemd/system/kidsplay-timeset.service" in log


def test_http_config_still_installs_the_clock_bootstrap(env: dict[str, str]) -> None:
    log = install(env, HTTP_CONFIG)
    assert "install -m755 /dev/stdin /usr/local/sbin/kidsplay-timeset.sh" in log
    assert "enable --now kidsplay-timeset.timer" in log


def test_config_without_a_transport_is_http(env: dict[str, str]) -> None:
    log = install(env, {"server_url": "http://nas.local:8000"})
    assert "enable --now kidsplay-timeset.timer" in log


def test_missing_config_is_http(env: dict[str, str]) -> None:
    assert "enable --now kidsplay-timeset.timer" in install(env, None)


def test_no_timeset_flag(env: dict[str, str]) -> None:
    log = install(env, HTTP_CONFIG, "--no-timeset")
    assert "enable --now kidsplay-timeset.timer" not in log
    assert "install -m755 /dev/stdin /usr/local/sbin/kidsplay-timeset.sh" not in log


def _timeset_script() -> str:
    text = INSTALLER.read_text()
    match = re.search(r"<<'TIMESET'\n(.*?)\nTIMESET\n", text, re.DOTALL)
    assert match
    return match.group(1)


def test_timeset_script_does_nothing_for_a_local_config(tmp_path: Path) -> None:
    """Already-installed timeset units become no-ops once the config is local."""
    script = tmp_path / "timeset.sh"
    script.write_text(_timeset_script())
    config = tmp_path / "config.json"
    config.write_text(json.dumps(LOCAL_CONFIG))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\necho "curl called" >> "$STUB_LOG"; exit 1\n')
    curl.chmod(0o755)
    log = tmp_path / "log"

    result = subprocess.run(
        ["sh", str(script), str(config)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "nothing to set" in result.stdout
    assert not log.exists()  # never asked the server for the time
