"""Tests for kidsplay_cli.credentials (local admin token storage)."""

import stat
from pathlib import Path

import pytest

from kidsplay_cli.credentials import (
    StoredToken,
    credentials_path,
    delete_token,
    load_token,
    save_token,
)


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


class TestCredentials:
    def test_path_honours_xdg(self, config_home: Path) -> None:
        assert credentials_path() == config_home / "kidsplay" / "credentials.json"

    def test_path_defaults_to_dot_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert credentials_path() == (
            Path.home() / ".config" / "kidsplay" / "credentials.json"
        )

    def test_missing_file_means_no_token(self, config_home: Path) -> None:
        assert load_token("http://a:8000") is None

    def test_round_trip_per_server(self, config_home: Path) -> None:
        a = StoredToken(token="kpa_a", token_id="1")
        b = StoredToken(token="kpa_b", token_id="2")
        save_token("http://a:8000/", a)
        save_token("http://b:8000", b)
        assert load_token("http://a:8000") == a
        assert load_token("http://b:8000/") == b

    def test_file_and_dir_permissions(self, config_home: Path) -> None:
        path = save_token("http://a:8000", StoredToken(token="kpa_a", token_id="1"))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    @pytest.mark.parametrize("mode", [0o755, 0o750, 0o777, 0o701])
    def test_looser_existing_directory_is_tightened(
        self, config_home: Path, mode: int
    ) -> None:
        directory = config_home / "kidsplay"
        directory.mkdir(parents=True)
        directory.chmod(mode)
        path = save_token("http://a:8000", StoredToken(token="kpa_a", token_id="1"))
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_looser_existing_file_is_replaced_with_0600(
        self, config_home: Path
    ) -> None:
        directory = config_home / "kidsplay"
        directory.mkdir(parents=True)
        old = directory / "credentials.json"
        old.write_text('{"servers": {}}')
        old.chmod(0o644)
        path = save_token("http://a:8000", StoredToken(token="kpa_a", token_id="1"))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_deleting_a_token_tightens_too(self, config_home: Path) -> None:
        save_token("http://a:8000", StoredToken(token="kpa_a", token_id="1"))
        save_token("http://b:8000", StoredToken(token="kpa_b", token_id="2"))
        directory = credentials_path().parent
        directory.chmod(0o755)
        assert delete_token("http://a:8000")
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_directory_that_cannot_be_changed_is_not_fatal(
        self, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = config_home / "kidsplay"
        directory.mkdir(parents=True)
        directory.chmod(0o755)

        def refuse(self: Path, mode: int) -> None:
            raise PermissionError("not yours")

        monkeypatch.setattr(Path, "chmod", refuse)
        path = save_token("http://a:8000", StoredToken(token="kpa_a", token_id="1"))
        assert load_token("http://a:8000") == StoredToken(token="kpa_a", token_id="1")
        assert path.exists()

    def test_overwrite_keeps_0600(self, config_home: Path) -> None:
        save_token("http://a:8000", StoredToken(token="kpa_a", token_id="1"))
        path = save_token("http://a:8000", StoredToken(token="kpa_c", token_id="3"))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert load_token("http://a:8000") == StoredToken(token="kpa_c", token_id="3")

    def test_delete(self, config_home: Path) -> None:
        save_token("http://a:8000", StoredToken(token="kpa_a", token_id="1"))
        save_token("http://b:8000", StoredToken(token="kpa_b", token_id="2"))
        assert delete_token("http://a:8000")
        assert not delete_token("http://a:8000")
        assert load_token("http://a:8000") is None
        assert load_token("http://b:8000") is not None

    def test_corrupt_file_raises(self, config_home: Path) -> None:
        path = config_home / "kidsplay" / "credentials.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        with pytest.raises(RuntimeError, match="Cannot read credentials"):
            load_token("http://a:8000")
