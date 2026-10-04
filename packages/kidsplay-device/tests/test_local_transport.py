"""Unit tests for ``local_transport.fetch_from_store`` and the config it needs."""

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from kidsplay_device.config import DeviceConfig
from kidsplay_device.local_transport import LocalFetchError, fetch_from_store

CONTENT = b"processed media bytes"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
REL = f"audio/{DIGEST[:2]}/{DIGEST}.mp3"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    (root / REL).parent.mkdir(parents=True)
    (root / REL).write_bytes(CONTENT)
    return root


class TestFetchFromStore:
    def test_links_when_possible(self, store: Path, tmp_path: Path) -> None:
        dest = tmp_path / "media" / REL
        how = fetch_from_store(store, REL, DIGEST, len(CONTENT), dest)
        assert how == "link"
        assert dest.stat().st_ino == (store / REL).stat().st_ino
        assert dest.stat().st_nlink == 2

    def test_copies_when_linking_fails(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EXDEV, "cross-device")

        monkeypatch.setattr(os, "link", refuse)
        dest = tmp_path / "media" / REL
        how = fetch_from_store(store, REL, DIGEST, len(CONTENT), dest)
        assert how == "copy"
        assert dest.read_bytes() == CONTENT
        assert dest.stat().st_ino != (store / REL).stat().st_ino
        assert not list(dest.parent.glob("*.part"))

    def test_copy_with_wrong_hash_is_rejected_and_leaves_nothing(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EPERM, "no hard links here")

        monkeypatch.setattr(os, "link", refuse)
        dest = tmp_path / "media" / REL
        with pytest.raises(LocalFetchError, match="hash mismatch"):
            fetch_from_store(store, REL, "0" * 64, len(CONTENT), dest)
        assert not dest.exists()
        assert not list(dest.parent.glob("*"))

    def test_wrong_size_is_rejected(self, store: Path, tmp_path: Path) -> None:
        dest = tmp_path / "media" / REL
        with pytest.raises(LocalFetchError, match="bytes"):
            fetch_from_store(store, REL, DIGEST, len(CONTENT) + 1, dest)
        assert not dest.exists()

    def test_missing_source_is_rejected(self, store: Path, tmp_path: Path) -> None:
        with pytest.raises(LocalFetchError, match="not readable"):
            fetch_from_store(store, "audio/no/such.mp3", DIGEST, 1, tmp_path / "d")

    @pytest.mark.parametrize("bad", ["../outside.mp3", "/etc/passwd", "audio/../.."])
    def test_paths_outside_the_store_are_rejected(
        self, store: Path, tmp_path: Path, bad: str
    ) -> None:
        (tmp_path / "outside.mp3").write_bytes(CONTENT)
        with pytest.raises(LocalFetchError, match="outside the media store"):
            fetch_from_store(store, bad, DIGEST, len(CONTENT), tmp_path / "d")

    def test_existing_destination_is_never_overwritten(
        self, store: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "media" / REL
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"other")
        with pytest.raises(LocalFetchError, match="already exists"):
            fetch_from_store(store, REL, DIGEST, len(CONTENT), dest)
        assert dest.read_bytes() == b"other"
        assert (store / REL).read_bytes() == CONTENT


def _config(tmp_path: Path, **overrides: object) -> DeviceConfig:
    fields: dict[str, object] = {
        "server_url": "http://localhost:8000",
        "device_id": "d",
        "api_key": "k",
        "media_root": tmp_path / "media",
        "db_path": tmp_path / "db.sqlite",
    }
    fields.update(overrides)
    return DeviceConfig(**fields)  # ty: ignore[invalid-argument-type] # kwargs built from a dict[str, object] for brevity


class TestConfig:
    def test_http_is_the_default(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        assert cfg.sync_transport == "http"
        assert not cfg.is_local

    def test_http_config_file_is_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _config(tmp_path).save(path)
        assert set(json.loads(path.read_text())) == {
            "server_url",
            "device_id",
            "api_key",
            "media_root",
            "db_path",
            "sync_interval_seconds",
            "fullscreen",
        }

    def test_local_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _config(
            tmp_path,
            sync_transport="local",
            server_media_store=tmp_path / "store",
        ).save(path)
        loaded = DeviceConfig.load(path)
        assert loaded.is_local
        assert loaded.server_media_store == tmp_path / "store"

    def test_unknown_transport_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="sync_transport"):
            _config(tmp_path, sync_transport="rsync")

    def test_local_needs_a_store(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="server_media_store"):
            _config(tmp_path, sync_transport="local")

    @pytest.mark.parametrize(
        "media_root",
        ["store", "store/inside", "."],
        ids=["same", "inside-store", "contains-store"],
    )
    def test_overlapping_directories_are_rejected(
        self, tmp_path: Path, media_root: str
    ) -> None:
        """The device prunes media_root, so an overlap could delete the
        server's files."""
        with pytest.raises(ValueError, match="separate directories"):
            _config(
                tmp_path,
                media_root=tmp_path / media_root,
                sync_transport="local",
                server_media_store=tmp_path / "store",
            )
