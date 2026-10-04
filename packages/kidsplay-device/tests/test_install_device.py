"""``install-device.sh``: the one command that sets up a handheld.

It runs for real, in bash, with every command it calls (uv, the kiosk
installer, ``kidsplay-allinone``) replaced by a stub that logs its arguments
through the ``KIDSPLAY_*`` overrides, and HOME in a temp dir. Nothing needs
sudo or systemd, so these run on Linux and macOS CI alike. One test also runs
the real ``install-kiosk.sh`` (with ``sudo`` stubbed) to prove the preset
lands in ``pair-server.txt``.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
INSTALLER = DEPLOY / "install-device.sh"
KIOSK = DEPLOY / "install-kiosk.sh"

SERVER = "http://kidsplay.local:8000"
CONFIG = {
    "server_url": SERVER,
    "device_id": "dev-1",
    "api_key": "secret",
    "media_root": "/tmp/m",
    "db_path": "/tmp/d.sqlite",
}

# Logs "<name> <cwd-basename> <args...>" so a test can see how it was called.
_LOGGER = '#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\n'


def _script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    """Stub commands, a sandboxed HOME and a (fake) checkout."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    _script(bin_dir / "id", 'case "$1" in -u) echo 1000;; *) echo tester;; esac\n')
    _script(bin_dir / "ffmpeg", "exit 0\n")
    return {
        "bin": bin_dir,
        "repo": repo,
        "home": home,
        "log": tmp_path / "stub.log",
        "uv": _script(bin_dir / "uv", _LOGGER.format(name="uv")),
        "kiosk": _script(tmp_path / "kiosk.sh", _LOGGER.format(name="kiosk")),
        "allinone": _script(tmp_path / "allinone", _LOGGER.format(name="allinone")),
        "config": home / ".kidsplay" / "config.json",
        "tmp": tmp_path,
    }


def run(
    box: dict[str, Path], *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installer against the stubs in ``box``."""
    full = {
        "PATH": f"{box['bin']}:{os.environ['PATH']}",
        "HOME": str(box["home"]),
        "STUB_LOG": str(box["log"]),
        "KIDSPLAY_REPO": str(box["repo"]),
        "KIDSPLAY_UV": str(box["uv"]),
        "KIDSPLAY_KIOSK_INSTALLER": str(box["kiosk"]),
        "KIDSPLAY_ALLINONE": str(box["allinone"]),
        # The test interpreter has the player installed: the address parser.
        "KIDSPLAY_PYTHON": sys.executable,
        "KIDSPLAY_APP_CMD": str(box["repo"] / ".venv" / "bin" / "kidsplay-player"),
        "KIDSPLAY_CONFIG": str(box["config"]),
        **(env or {}),
    }
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        env=full,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def calls(box: dict[str, Path]) -> list[str]:
    return (
        [line.rstrip() for line in box["log"].read_text().splitlines()]
        if box["log"].exists()
        else []
    )


def write_config(box: dict[str, Path], **overrides: str) -> None:
    box["config"].parent.mkdir(parents=True, exist_ok=True)
    box["config"].write_text(json.dumps({**CONFIG, **overrides}))


def untouched(box: dict[str, Path]) -> bool:
    """Nothing ran and nothing was written."""
    return not calls(box) and not (box["home"] / ".kidsplay").exists()


def test_installer_parses() -> None:
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


class TestChoosingAMode:
    def test_no_mode_is_an_error_with_usage(self, box: dict[str, Path]) -> None:
        result = run(box)
        assert result.returncode != 0
        assert "--server URL" in result.stderr and "--standalone" in result.stderr
        assert "choose a mode" in result.stderr
        assert untouched(box)

    def test_both_modes_is_an_error(self, box: dict[str, Path]) -> None:
        result = run(box, "--server", SERVER, "--standalone", "--profile-name", "A")
        assert result.returncode != 0
        assert "choose one" in result.stderr
        assert untouched(box)

    def test_help_exits_zero(self, box: dict[str, Path]) -> None:
        result = run(box, "--help")
        assert result.returncode == 0
        assert "--standalone" in result.stdout

    def test_standalone_options_are_refused_with_server(
        self, box: dict[str, Path]
    ) -> None:
        result = run(box, "--server", SERVER, "--lan")
        assert result.returncode != 0
        assert untouched(box)

    def test_admin_password_is_never_a_flag(self, box: dict[str, Path]) -> None:
        result = run(
            box, "--standalone", "--profile-name", "A", "--admin-password", "hunter22"
        )
        assert result.returncode != 0
        assert "KIDSPLAY_ADMIN_PASSWORD" in result.stderr
        assert untouched(box)


class TestServerPairing:
    def test_syncs_only_the_device_package_and_presets_the_server(
        self, box: dict[str, Path]
    ) -> None:
        result = run(box, "--server", "kidsplay.local", "--timezone", "UTC")
        assert result.returncode == 0, result.stderr
        log = calls(box)
        assert log[0] == "uv sync --package kidsplay-device --locked"
        assert not any("--all-packages" in line for line in log)
        # The address was normalized (default port) and handed to the kiosk
        # installer, which owns the pair-server.txt preset.
        assert log[1] == f"kiosk --timezone UTC --server {SERVER}"
        assert not box["config"].exists()
        assert "pairing code" in result.stdout

    def test_uv_runs_in_the_checkout(self, box: dict[str, Path]) -> None:
        _script(box["uv"], 'pwd >> "$STUB_LOG"\n')
        assert run(box, "--server", SERVER).returncode == 0
        assert Path(calls(box)[0]).resolve() == box["repo"].resolve()

    def test_real_kiosk_installer_writes_pair_server_txt(
        self, box: dict[str, Path]
    ) -> None:
        """The preset is written by install-kiosk.sh: one implementation."""
        _script(box["bin"] / "sudo", 'echo "sudo $*" >> "$STUB_LOG"; cat >/dev/null\n')
        _script(box["bin"] / "labwc", "exit 0\n")
        _script(
            box["bin"] / "timedatectl", "case $1 in show) echo Europe/Berlin;; esac\n"
        )
        app = box["repo"] / ".venv" / "bin" / "kidsplay-player"
        _script(app, "#!/bin/sh\n")
        # install-kiosk.sh checks the address with `$(dirname APP)/python`.
        # (A wrapper, not a symlink: a venv's python needs its own location.)
        _script(
            box["repo"] / ".venv" / "bin" / "python",
            f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
        )
        result = run(
            box,
            "--server",
            "kidsplay.local",
            env={"KIDSPLAY_KIOSK_INSTALLER": str(KIOSK)},
        )
        assert result.returncode == 0, result.stderr + result.stdout
        preset = box["home"] / ".kidsplay" / "pair-server.txt"
        assert preset.read_text() == f"{SERVER}\n"

    @pytest.mark.parametrize("bad", ["not a url", "http://a/b", ""])
    def test_bad_address_is_refused_before_anything_changes(
        self, box: dict[str, Path], bad: str
    ) -> None:
        result = run(box, "--server", bad)
        assert result.returncode != 0
        assert untouched(box)

    def test_bad_address_is_refused_before_uv_runs(self, box: dict[str, Path]) -> None:
        """With the player already installed the check precedes any install."""
        result = run(box, "--server", "http://a/b")
        assert "not a server address" in result.stderr
        assert calls(box) == []

    def test_bad_timezone_is_refused_before_anything_changes(
        self, box: dict[str, Path]
    ) -> None:
        result = run(box, "--server", SERVER, "--timezone", "Mars/Olympus")
        assert result.returncode != 0
        assert "timezone" in result.stderr
        assert untouched(box)

    def test_existing_config_for_another_server_is_refused(
        self, box: dict[str, Path]
    ) -> None:
        write_config(box, server_url="http://other.local:8000")
        before = box["config"].read_text()
        result = run(box, "--server", SERVER)
        assert result.returncode != 0
        assert "--force" in result.stderr
        assert box["config"].read_text() == before
        assert calls(box) == []

    def test_force_sets_the_old_config_aside_so_the_device_pairs(
        self, box: dict[str, Path]
    ) -> None:
        write_config(box, server_url="http://other.local:8000")
        old = box["config"].read_text()
        assert run(box, "--server", SERVER, "--force").returncode == 0
        assert not box["config"].exists()
        assert Path(f"{box['config']}.old").read_text() == old
        assert f"--server {SERVER}" in calls(box)[-1]

    def test_existing_config_for_the_same_server_is_kept(
        self, box: dict[str, Path]
    ) -> None:
        write_config(box)
        before = box["config"].read_text()
        assert run(box, "--server", "kidsplay.local").returncode == 0
        assert box["config"].read_text() == before

    def test_failing_install_step_stops_the_run(self, box: dict[str, Path]) -> None:
        _script(box["uv"], "exit 3\n")
        result = run(box, "--server", SERVER)
        assert result.returncode != 0
        assert calls(box) == []  # the kiosk installer never ran


class TestServerWithConfig:
    @pytest.fixture
    def config_file(self, box: dict[str, Path]) -> Path:
        path = box["tmp"] / "from-setup.json"
        path.write_text(json.dumps(CONFIG))
        return path

    def test_installs_the_config_securely_without_a_preset(
        self, box: dict[str, Path], config_file: Path
    ) -> None:
        result = run(box, "--server", SERVER, "--config", str(config_file))
        assert result.returncode == 0, result.stderr
        assert json.loads(box["config"].read_text()) == CONFIG
        assert stat.S_IMODE(box["config"].stat().st_mode) == 0o600
        assert stat.S_IMODE(box["config"].parent.stat().st_mode) == 0o700
        assert not list(box["config"].parent.glob("*.tmp*"))
        log = calls(box)
        assert log[0] == "uv sync --package kidsplay-device --locked"
        assert log[1] == "kiosk"  # no --server: nothing to pair
        assert not (box["config"].parent / "pair-server.txt").exists()

    def test_mismatched_server_is_refused(
        self, box: dict[str, Path], config_file: Path
    ) -> None:
        result = run(
            box, "--server", "http://elsewhere.local:8000", "--config", str(config_file)
        )
        assert result.returncode != 0
        assert SERVER in result.stderr and "--force" in result.stderr
        assert untouched(box)

    def test_force_accepts_a_mismatch(
        self, box: dict[str, Path], config_file: Path
    ) -> None:
        result = run(
            box,
            "--server",
            "http://elsewhere.local:8000",
            "--config",
            str(config_file),
            "--force",
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(box["config"].read_text()) == CONFIG

    @pytest.mark.parametrize(
        "content", ["not json", "[]", json.dumps({"server_url": SERVER})]
    )
    def test_invalid_config_is_refused(
        self, box: dict[str, Path], content: str
    ) -> None:
        bad = box["tmp"] / "bad.json"
        bad.write_text(content)
        result = run(box, "--server", SERVER, "--config", str(bad))
        assert result.returncode != 0
        assert "not a device config" in result.stderr
        assert untouched(box)

    def test_missing_config_file_is_refused(self, box: dict[str, Path]) -> None:
        result = run(box, "--server", SERVER, "--config", str(box["tmp"] / "nope"))
        assert result.returncode != 0
        assert untouched(box)

    def test_replaces_a_config_for_another_server_only_with_force(
        self, box: dict[str, Path], config_file: Path
    ) -> None:
        write_config(box, server_url="http://other.local:8000")
        old = box["config"].read_text()
        refused = run(box, "--server", SERVER, "--config", str(config_file))
        assert refused.returncode != 0
        assert box["config"].read_text() == old
        assert (
            run(
                box, "--server", SERVER, "--config", str(config_file), "--force"
            ).returncode
            == 0
        )
        assert json.loads(box["config"].read_text()) == CONFIG
        assert Path(f"{box['config']}.old").read_text() == old


class TestStandalone:
    def test_needs_a_profile_name(self, box: dict[str, Path]) -> None:
        result = run(box, "--standalone")
        assert result.returncode != 0
        assert "--profile-name" in result.stderr
        assert untouched(box)

    def test_syncs_everything_then_runs_allinone_then_the_kiosk(
        self, box: dict[str, Path]
    ) -> None:
        result = run(
            box,
            "--standalone",
            "--profile-name",
            "Alice",
            "--device-name",
            "Alice's GB",
            "--lan",
            "--port",
            "8123",
            "--timezone",
            "UTC",
        )
        assert result.returncode == 0, result.stderr
        assert calls(box) == [
            "uv sync --all-packages --locked",
            "allinone --profile-name Alice --config-path "
            f"{box['config']} --device-name Alice's GB --port 8123 --lan",
            "kiosk --timezone UTC",
        ]

    def test_missing_ffmpeg_stops_with_the_apt_command(
        self, box: dict[str, Path]
    ) -> None:
        result = run(
            box,
            "--standalone",
            "--profile-name",
            "A",
            env={"KIDSPLAY_FFMPEG": "ffmpeg-not-installed"},
        )
        assert result.returncode != 0
        assert "sudo apt-get install -y ffmpeg" in result.stderr
        assert untouched(box)

    def test_password_is_passed_by_environment_only(self, box: dict[str, Path]) -> None:
        _script(
            box["allinone"],
            'echo "pw=$KIDSPLAY_ADMIN_PASSWORD args=$*" >> "$STUB_LOG"\n',
        )
        result = run(
            box,
            "--standalone",
            "--profile-name",
            "A",
            env={"KIDSPLAY_ADMIN_PASSWORD": "correct horse"},
        )
        assert result.returncode == 0, result.stderr
        line = next(c for c in calls(box) if c.startswith("pw="))
        assert line.startswith("pw=correct horse ")
        assert "correct horse" not in line.split("args=", 1)[1]

    def test_existing_config_for_another_server_is_refused(
        self, box: dict[str, Path]
    ) -> None:
        write_config(box, server_url="http://other.local:8000")
        result = run(box, "--standalone", "--profile-name", "A")
        assert result.returncode != 0
        assert "--force" in result.stderr
        assert calls(box) == []

    def test_force_is_passed_on_and_the_old_config_kept(
        self, box: dict[str, Path]
    ) -> None:
        write_config(box, server_url="http://other.local:8000")
        result = run(box, "--standalone", "--profile-name", "A", "--force")
        assert result.returncode == 0, result.stderr
        assert any(
            c.startswith("allinone") and c.endswith("--force") for c in calls(box)
        )

    def test_rerun_over_an_all_in_one_config_is_fine(
        self, box: dict[str, Path]
    ) -> None:
        write_config(box, server_url="http://127.0.0.1:8000", sync_transport="local")
        assert run(box, "--standalone", "--profile-name", "A").returncode == 0


def test_finds_uv_in_local_bin_when_it_is_not_on_path(box: dict[str, Path]) -> None:
    """uv's installer puts it in ~/.local/bin, which a non-login shell (ssh host
    cmd) has no PATH entry for; on the reference handheld that stopped the
    installer with "uv not found"."""
    box["uv"].unlink()
    local_bin = box["home"] / ".local" / "bin"
    local_bin.mkdir(parents=True)
    _script(local_bin / "uv", _LOGGER.format(name="uv"))
    result = run(
        box,
        "--server",
        SERVER,
        env={"KIDSPLAY_UV": "", "PATH": f"{box['bin']}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("uv ") for line in calls(box)), calls(box)


def test_already_paired_device_is_not_told_to_expect_a_code(
    box: dict[str, Path],
) -> None:
    """Re-running on a device already paired with this server keeps its config,
    so the closing message must not promise a pairing code."""
    box["config"].parent.mkdir(parents=True)
    box["config"].write_text(json.dumps(CONFIG))
    result = run(box, "--server", SERVER)
    assert result.returncode == 0, result.stderr
    assert "syncs with" in result.stdout
    assert "pairing code" not in result.stdout
