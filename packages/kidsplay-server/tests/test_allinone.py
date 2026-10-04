"""Tests for ``kidsplay-allinone`` (kidsplay_server.allinone)."""

import asyncio
import json
import os
import re
import stat
from pathlib import Path

import aiosqlite
import pytest
from click.testing import CliRunner, Result

from kidsplay_device.config import DeviceConfig
from kidsplay_server import allinone
from kidsplay_server.auth import check_admin_password, init_auth_db
from kidsplay_server.database import configure_conn, list_devices, list_profiles

PASSWORD = "correct horse battery"


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        "data": tmp_path / "data",
        "config": tmp_path / "home" / "config.json",
        "media": tmp_path / "home" / "media",
        "player_db": tmp_path / "home" / "db.sqlite",
        "units": tmp_path / "units",
    }


def run(dirs: dict[str, Path], *extra: str, password: str | None = PASSWORD) -> Result:
    args = [
        "--data-dir",
        str(dirs["data"]),
        "--config-path",
        str(dirs["config"]),
        "--media-root",
        str(dirs["media"]),
        "--player-db",
        str(dirs["player_db"]),
        "--systemd-dir",
        str(dirs["units"]),
        *extra,
    ]
    if password is not None:
        args += ["--admin-password", password]
    return CliRunner().invoke(allinone.main, args)


@pytest.fixture(autouse=True)
def _no_systemctl(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Never touch the real systemd; record what would have been run."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(allinone, "_systemctl", lambda *a: calls.append(a))
    monkeypatch.delenv("KIDSPLAY_ADMIN_PASSWORD", raising=False)
    # These tests may themselves run as root (containers); the check has its own.
    monkeypatch.setattr(allinone, "_running_as_root", lambda: False)
    return calls


class TestSetup:
    def test_creates_everything(self, dirs: dict[str, Path]) -> None:
        dirs["units"].mkdir()
        result = run(dirs)
        assert result.exit_code == 0, result.output
        assert (dirs["data"] / "db.sqlite").exists()
        assert (dirs["data"] / "media").is_dir()
        assert dirs["media"].is_dir()
        assert (dirs["units"] / allinone.UNIT_NAME).exists()

    def test_admin_profile_and_device_are_registered(
        self, dirs: dict[str, Path]
    ) -> None:
        dirs["units"].mkdir()
        result = run(dirs, "--profile-name", "Leo", "--device-name", "Leo's GB")
        assert result.exit_code == 0, result.output

        async def read() -> tuple[bool, list, list]:
            async with aiosqlite.connect(dirs["data"] / "db.sqlite") as conn:
                await configure_conn(conn)
                await init_auth_db(conn)
                return (
                    await check_admin_password(conn, PASSWORD),
                    await list_profiles(conn),
                    await list_devices(conn),
                )

        password_ok, profiles, devices = asyncio.run(read())
        assert password_ok
        assert [p.name for p in profiles] == ["Leo"]
        assert [d.name for d in devices] == ["Leo's GB"]
        assert devices[0].profile_id == profiles[0].id

    def test_config_is_a_valid_local_player_config(self, dirs: dict[str, Path]) -> None:
        dirs["units"].mkdir()
        assert run(dirs, "--port", "8123").exit_code == 0

        config = DeviceConfig.load(dirs["config"])  # the device's own parser
        assert config.is_local
        assert config.server_url == "http://127.0.0.1:8123"
        assert config.server_media_store == (dirs["data"] / "media").resolve()
        assert config.media_root == dirs["media"].resolve()
        # The config holds the device's API key.
        assert stat.S_IMODE(os.stat(dirs["config"]).st_mode) == 0o600

    def test_player_starts_fullscreen(self, dirs: dict[str, Path]) -> None:
        """All-in-one runs on the handheld itself: a windowed player is wrong."""
        dirs["units"].mkdir()
        assert run(dirs).exit_code == 0
        assert json.loads(dirs["config"].read_text())["fullscreen"] is True
        assert DeviceConfig.load(dirs["config"]).fullscreen

    def test_rerun_keeps_a_chosen_screen_mode(self, dirs: dict[str, Path]) -> None:
        dirs["units"].mkdir()
        assert run(dirs).exit_code == 0
        config = json.loads(dirs["config"].read_text())
        config["fullscreen"] = False
        dirs["config"].write_text(json.dumps(config))
        assert run(dirs, password=None).exit_code == 0
        assert json.loads(dirs["config"].read_text())["fullscreen"] is False

    def test_rerun_reuses_the_device(self, dirs: dict[str, Path]) -> None:
        dirs["units"].mkdir()
        assert run(dirs).exit_code == 0
        first = json.loads(dirs["config"].read_text())
        result = run(dirs, password=None)  # admin already set: no prompt
        assert result.exit_code == 0, result.output
        assert json.loads(dirs["config"].read_text()) == first
        assert "already registered" in result.output

    def test_rerun_never_changes_the_admin_password(
        self, dirs: dict[str, Path]
    ) -> None:
        dirs["units"].mkdir()
        assert run(dirs).exit_code == 0
        assert run(dirs, password="another password!").exit_code == 0

        async def check() -> bool:
            async with aiosqlite.connect(dirs["data"] / "db.sqlite") as conn:
                await configure_conn(conn)
                return await check_admin_password(conn, PASSWORD)

        assert asyncio.run(check())

    def test_short_password_is_rejected(self, dirs: dict[str, Path]) -> None:
        result = run(dirs, password="short")
        assert result.exit_code != 0
        assert "at least" in result.output
        assert not dirs["data"].exists()

    def test_prompts_for_the_password_when_none_is_set(
        self, dirs: dict[str, Path]
    ) -> None:
        dirs["units"].mkdir()
        result = CliRunner().invoke(
            allinone.main,
            [
                "--data-dir", str(dirs["data"]),
                "--config-path", str(dirs["config"]),
                "--media-root", str(dirs["media"]),
                "--player-db", str(dirs["player_db"]),
                "--systemd-dir", str(dirs["units"]),
            ],
            input=f"{PASSWORD}\n{PASSWORD}\n",
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        assert "Admin password set" in result.output


class TestRoot:
    def test_refuses_to_run_as_root(
        self, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(allinone, "_running_as_root", lambda: True)
        result = run(dirs)
        assert result.exit_code != 0
        assert "not as root" in result.output
        assert not dirs["data"].exists()
        assert not dirs["config"].exists()


class TestPlayerConfigSafety:
    def test_does_not_replace_a_config_for_another_server(
        self, dirs: dict[str, Path]
    ) -> None:
        dirs["config"].parent.mkdir(parents=True)
        original = json.dumps({"server_url": "http://nas.local:8000"})
        dirs["config"].write_text(original)
        result = run(dirs, "--no-systemd")
        assert result.exit_code != 0
        assert "nas.local" in result.output
        assert dirs["config"].read_text() == original

    def test_force_replaces_it(self, dirs: dict[str, Path]) -> None:
        dirs["config"].parent.mkdir(parents=True)
        dirs["config"].write_text(json.dumps({"server_url": "http://nas.local:8000"}))
        assert run(dirs, "--no-systemd", "--force").exit_code == 0
        assert json.loads(dirs["config"].read_text())["sync_transport"] == "local"

    @pytest.mark.parametrize("media_root", ["data/media", "data/media/player", "data"])
    def test_media_root_overlapping_the_store_is_refused(
        self, dirs: dict[str, Path], tmp_path: Path, media_root: str
    ) -> None:
        dirs["media"] = tmp_path / media_root
        result = run(dirs, "--no-systemd")
        assert result.exit_code != 0
        assert "separate directories" in result.output
        assert not dirs["config"].exists()

    def test_warns_when_files_would_be_copied(
        self, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(allinone, "_same_filesystem", lambda a, b: False)
        result = run(dirs, "--no-systemd")
        assert result.exit_code == 0
        assert "different filesystems" in result.output


class TestSystemd:
    def test_installs_enables_and_starts_the_unit(
        self, dirs: dict[str, Path], _no_systemctl: list[tuple[str, ...]]
    ) -> None:
        dirs["units"].mkdir()
        assert run(dirs).exit_code == 0
        assert _no_systemctl == [
            ("daemon-reload",),
            ("enable", allinone.UNIT_NAME),
            ("restart", allinone.UNIT_NAME),  # a re-run may change port or paths
        ]

    def test_no_systemd_prints_the_unit_and_installs_nothing(
        self, dirs: dict[str, Path], _no_systemctl: list[tuple[str, ...]]
    ) -> None:
        result = run(dirs, "--no-systemd")
        assert result.exit_code == 0
        assert "[Service]" in result.output
        assert not dirs["units"].exists()
        assert _no_systemctl == []


def unit(**overrides: object) -> str:
    fields: dict[str, object] = {
        "user": "pi",
        "python": "/home/pi/kidsplay/.venv/bin/python",
        "host": "127.0.0.1",
        "port": 8000,
        "db_path": Path("/home/pi/.local/share/kidsplay/db.sqlite"),
        "media_store": Path("/home/pi/.local/share/kidsplay/media"),
        "log_file": Path("/home/pi/.local/share/kidsplay/server.log"),
    }
    fields.update(overrides)
    return allinone.render_server_unit(**fields)


class TestUnit:
    def test_binds_to_localhost_by_default(self) -> None:
        text = unit()
        assert "--host 127.0.0.1 --port 8000" in text
        assert "0.0.0.0" not in text

    def test_lan_binds_to_all_interfaces(self) -> None:
        assert "--host 0.0.0.0 --port 8080" in unit(host="0.0.0.0", port=8080)

    def test_runs_the_app_factory_from_the_given_python(self) -> None:
        text = unit()
        assert re.search(
            r"^ExecStart=/home/pi/kidsplay/.venv/bin/python -m uvicorn "
            r"kidsplay_server.api.app:create_app_from_env --factory ",
            text,
            re.MULTILINE,
        )
        assert "User=pi" in text

    def test_uvicorn_leaves_forwarded_headers_to_kidsplay(self) -> None:
        """Only KIDSPLAY_TRUSTED_PROXIES decides whom to believe."""
        assert "--no-proxy-headers" in unit()

    def test_resource_budget(self) -> None:
        text = unit()
        assert f"Nice={allinone.SERVER_NICE}" in text
        assert "IOSchedulingClass=best-effort" in text
        assert f'"KIDSPLAY_PROCESSING_NICE={allinone.FFMPEG_NICE}"' in text
        assert '"KIDSPLAY_PROCESSING_JOBS=1"' in text

    def test_data_paths_are_pinned(self) -> None:
        text = unit()
        assert '"KIDSPLAY_DB_PATH=/home/pi/.local/share/kidsplay/db.sqlite"' in text
        assert '"KIDSPLAY_MEDIA_STORE=/home/pi/.local/share/kidsplay/media"' in text

    def test_starts_at_boot_and_restarts(self) -> None:
        text = unit()
        assert "WantedBy=multi-user.target" in text
        assert "Restart=on-failure" in text

    def test_percent_signs_are_escaped_for_systemd(self) -> None:
        text = unit(media_store=Path("/data/100%/media"))
        assert '"KIDSPLAY_MEDIA_STORE=/data/100%%/media"' in text

    def test_no_secrets_in_the_unit(self) -> None:
        assert "PASSWORD" not in unit()
