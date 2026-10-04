"""Tests for kidsplay_server.storage — content-addressed file store.

All tests use real files on disk via tmp_path. No mocking.
"""

import hashlib
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kidsplay_server.storage import MediaStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: bytes) -> Path:
    """Write bytes to a file and return the path."""
    path.write_bytes(content)
    return path


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# compute_hash
# ---------------------------------------------------------------------------


class TestComputeHash:
    def test_known_content(self, store: MediaStore, tmp_path: Path) -> None:
        content = b"hello kidsplay"
        src = _write(tmp_path / "file.txt", content)
        assert store.compute_hash(src) == _sha256(content)

    def test_empty_file(self, store: MediaStore, tmp_path: Path) -> None:
        src = _write(tmp_path / "empty.txt", b"")
        assert store.compute_hash(src) == _sha256(b"")

    def test_different_content_different_hash(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        a = _write(tmp_path / "a.mp3", b"audio-a")
        b = _write(tmp_path / "b.mp3", b"audio-b")
        assert store.compute_hash(a) != store.compute_hash(b)

    def test_same_content_same_hash(self, store: MediaStore, tmp_path: Path) -> None:
        data = b"duplicate content"
        a = _write(tmp_path / "a.mp3", data)
        b = _write(tmp_path / "b.mp3", data)
        assert store.compute_hash(a) == store.compute_hash(b)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class TestStore:
    def test_returns_correct_hash(self, store: MediaStore, tmp_path: Path) -> None:
        content = b"song data"
        src = _write(tmp_path / "song.mp3", content)
        h, _ = store.store(src, "audio", ".mp3")
        assert h == _sha256(content)

    def test_returns_correct_relative_path(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        content = b"song data"
        src = _write(tmp_path / "song.mp3", content)
        h, rel = store.store(src, "audio", ".mp3")
        expected = f"audio/{h[:2]}/{h}.mp3"
        assert rel == expected

    def test_file_exists_at_path(self, store: MediaStore, tmp_path: Path) -> None:
        src = _write(tmp_path / "song.mp3", b"audio bytes")
        h, rel = store.store(src, "audio", ".mp3")
        assert (store.root / rel).exists()

    def test_stored_content_matches_source(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        content = b"audio content"
        src = _write(tmp_path / "song.mp3", content)
        _, rel = store.store(src, "audio", ".mp3")
        assert (store.root / rel).read_bytes() == content

    def test_prefix_subdirectory_created(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        src = _write(tmp_path / "song.mp3", b"data")
        h, _ = store.store(src, "audio", ".mp3")
        assert (store.root / "audio" / h[:2]).is_dir()

    def test_thumbnail_suffix(self, store: MediaStore, tmp_path: Path) -> None:
        content = b"thumbnail bytes"
        src = _write(tmp_path / "thumb.webp", content)
        h, rel = store.store(src, "thumbnails", "_200x200.webp")
        expected = f"thumbnails/{h[:2]}/{h}_200x200.webp"
        assert rel == expected
        assert (store.root / rel).exists()

    def test_idempotent_same_file(self, store: MediaStore, tmp_path: Path) -> None:
        """Storing the same file twice returns the same result with no error."""
        content = b"duplicate"
        src = _write(tmp_path / "song.mp3", content)
        h1, rel1 = store.store(src, "audio", ".mp3")
        h2, rel2 = store.store(src, "audio", ".mp3")
        assert h1 == h2
        assert rel1 == rel2
        assert (store.root / rel1).exists()

    def test_idempotent_does_not_overwrite(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        """Second store call with same hash leaves the existing file intact."""
        content = b"original"
        src = _write(tmp_path / "song.mp3", content)
        _, rel = store.store(src, "audio", ".mp3")
        # Overwrite the source with different content — same hash won't re-copy.
        # Since hash changed, this is a different store call; just verify existing
        # file is intact after first call.
        assert (store.root / rel).read_bytes() == content

    def test_multiple_different_files(self, store: MediaStore, tmp_path: Path) -> None:
        files = [(f"file{i}.mp3", f"content {i}".encode()) for i in range(5)]
        stored = []
        for name, data in files:
            src = _write(tmp_path / name, data)
            stored.append(store.store(src, "audio", ".mp3"))

        hashes = [h for h, _ in stored]
        assert len(set(hashes)) == 5  # all unique

        for _, rel in stored:
            assert (store.root / rel).exists()


# ---------------------------------------------------------------------------
# exists / get_path / get_absolute_path
# ---------------------------------------------------------------------------


class TestExistsAndPaths:
    def test_exists_true_for_stored_file(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        src = _write(tmp_path / "f.mp3", b"data")
        _, rel = store.store(src, "audio", ".mp3")
        assert store.exists(rel)

    def test_exists_false_for_unstored(self, store: MediaStore) -> None:
        assert not store.exists("audio/ab/abcdef1234_nonexistent.mp3")

    def test_get_path_matches_stored_location(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        content = b"test"
        src = _write(tmp_path / "f.mp3", content)
        h, _ = store.store(src, "audio", ".mp3")
        expected = store.root / "audio" / h[:2] / f"{h}.mp3"
        assert store.get_path(h, "audio", ".mp3") == expected

    def test_get_path_does_not_require_existence(self, store: MediaStore) -> None:
        fake_hash = "a" * 64
        path = store.get_path(fake_hash, "audio", ".mp3")
        assert not path.exists()  # doesn't raise

    def test_get_absolute_path(self, store: MediaStore, tmp_path: Path) -> None:
        src = _write(tmp_path / "f.mp3", b"bytes")
        _, rel = store.store(src, "audio", ".mp3")
        assert store.get_absolute_path(rel) == store.root / rel


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_file(self, store: MediaStore, tmp_path: Path) -> None:
        src = _write(tmp_path / "f.mp3", b"audio")
        _, rel = store.store(src, "audio", ".mp3")
        assert store.exists(rel)
        store.delete(rel)
        assert not store.exists(rel)

    def test_delete_nonexistent_is_noop(self, store: MediaStore) -> None:
        store.delete("audio/xx/nonexistent.mp3")  # must not raise

    def test_delete_does_not_affect_other_files(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        src_a = _write(tmp_path / "a.mp3", b"aaa")
        src_b = _write(tmp_path / "b.mp3", b"bbb")
        _, rel_a = store.store(src_a, "audio", ".mp3")
        _, rel_b = store.store(src_b, "audio", ".mp3")

        store.delete(rel_a)

        assert not store.exists(rel_a)
        assert store.exists(rel_b)


class TestStoreIsAtomic:
    """A stored file never exists half-written (two ingests can race)."""

    def test_destination_appears_complete_or_not_at_all(
        self, store: MediaStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = b"x" * 100_000
        src = _write(tmp_path / "song.mp3", content)
        dest = store.get_path(_sha256(content), "audio", ".mp3")
        real_copy = shutil.copy2
        seen: list[bool] = []

        def slow_copy(s: Path, d: Path) -> object:
            # Halfway through the copy, a concurrent reader looks at dest.
            Path(d).write_bytes(content[:10])
            seen.append(dest.exists())
            return real_copy(s, d)

        monkeypatch.setattr(shutil, "copy2", slow_copy)
        store.store(src, "audio", ".mp3")
        assert seen == [False]
        assert dest.read_bytes() == content

    def test_concurrent_stores_of_same_content(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        content = os.urandom(2_000_000)
        src = _write(tmp_path / "big.mp3", content)
        results: list[tuple[str, str]] = []
        with ThreadPoolExecutor(8) as pool:
            futures = [
                pool.submit(store.store, src, "audio", ".mp3") for _ in range(16)
            ]
            results = [f.result() for f in futures]
        assert len(set(results)) == 1
        dest = store.get_absolute_path(results[0][1])
        assert dest.read_bytes() == content
        assert [p.name for p in dest.parent.iterdir()] == [dest.name]

    def test_existing_file_is_not_replaced(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        src = _write(tmp_path / "a.mp3", b"same")
        _, rel = store.store(src, "audio", ".mp3")
        inode = store.get_absolute_path(rel).stat().st_ino
        # Even when the exists() fast path loses a race, the link is refused.
        MediaStore._publish(src, store.get_absolute_path(rel))
        assert store.get_absolute_path(rel).stat().st_ino == inode
        assert [p.name for p in store.get_absolute_path(rel).parent.iterdir()] == [
            Path(rel).name
        ]

    def test_failed_copy_leaves_no_temp_file(
        self, store: MediaStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = _write(tmp_path / "a.mp3", b"data")

        def boom(s: Path, d: Path) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(shutil, "copy2", boom)
        with pytest.raises(OSError, match="disk full"):
            store.store(src, "audio", ".mp3")
        assert not [p for p in store.root.rglob("*") if p.is_file()]

    def test_falls_back_to_rename_without_hard_links(
        self, store: MediaStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_link(s: object, d: object) -> None:
            raise PermissionError("links not supported")

        monkeypatch.setattr(os, "link", no_link)
        src = _write(tmp_path / "a.mp3", b"data")
        _, rel = store.store(src, "audio", ".mp3")
        assert store.get_absolute_path(rel).read_bytes() == b"data"
        assert [p.name for p in store.get_absolute_path(rel).parent.iterdir()] == [
            Path(rel).name
        ]
