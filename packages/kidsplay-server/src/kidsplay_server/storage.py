"""Content-addressed file storage for the KidsPlay media store.

Files are stored at {root}/{subdir}/{hash[:2]}/{hash}{suffix}.
The SHA-256 hash of the file content is the address. Storing a file
whose hash already exists is a silent no-op (idempotent ingest).

Typical usage:
    store = MediaStore(Path("/mnt/media"))
    content_hash, rel_path = store.store(
        source=Path("/tmp/upload/song.mp3"),
        subdir="audio",
        suffix=".mp3",
    )
    # rel_path = "audio/ab/abcd1234...sha256.mp3"
"""

import contextlib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path


class MediaStore:
    """Manages a content-addressed filesystem store.

    All paths returned by methods are either absolute Paths (get_path,
    get_absolute_path) or POSIX-style relative strings using forward
    slashes (store, suitable for storing in the database).

    Args:
        root: Root directory. Created on construction if absent.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def compute_hash(self, source: Path) -> str:
        """Compute the SHA-256 hash of a file.

        Reads in 64 KB chunks to handle large audio/video files without
        loading them fully into memory.

        Args:
            source: Path to the file to hash.

        Returns:
            Lowercase hex SHA-256 digest string.
        """
        h = hashlib.sha256()
        with source.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def store(self, source: Path, subdir: str, suffix: str) -> tuple[str, str]:
        """Store a file in content-addressed storage.

        Hashes the source file, then copies it to
        {root}/{subdir}/{hash[:2]}/{hash}{suffix}. If the destination
        already exists (same hash, same subdir/suffix), the copy is
        skipped and the existing hash/path are returned unchanged.

        Args:
            source: Path to the file to store.
            subdir: Storage subdirectory (e.g. 'audio', 'thumbnails', 'photos').
            suffix: Filename suffix including extension
                (e.g. '.mp3', '_200x200.webp').

        Returns:
            Tuple of ``(content_hash, relative_path)`` where
            ``relative_path`` is a POSIX string relative to the store root,
            suitable for storing in the database.
        """
        content_hash = self.compute_hash(source)
        rel = self._rel(content_hash, subdir, suffix)
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            self._publish(source, dest)
        return content_hash, rel

    def get_path(self, content_hash: str, subdir: str, suffix: str) -> Path:
        """Return the absolute path for a content-addressed file.

        The file may or may not exist; call exists() to check first.

        Args:
            content_hash: SHA-256 hex digest.
            subdir: Storage subdirectory.
            suffix: Filename suffix including extension.

        Returns:
            Absolute Path where the file would be stored.
        """
        return self.root / self._rel(content_hash, subdir, suffix)

    def get_absolute_path(self, relative_path: str) -> Path:
        """Return the absolute path for a relative path from the database.

        Args:
            relative_path: POSIX path string relative to the store root.

        Returns:
            Absolute Path.
        """
        return self.root / relative_path

    def exists(self, relative_path: str) -> bool:
        """Return True if a file exists in the store.

        Args:
            relative_path: POSIX path string relative to the store root.
        """
        return (self.root / relative_path).exists()

    def delete(self, relative_path: str) -> None:
        """Delete a file from the store. No-op if the file does not exist.

        Args:
            relative_path: POSIX path string relative to the store root.
        """
        (self.root / relative_path).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _publish(source: Path, dest: Path) -> None:
        """Copy ``source`` to ``dest`` so ``dest`` never exists half-written.

        The copy goes to a temporary file in the same directory and is then
        linked into place. Linking never replaces an existing file, so two
        concurrent stores of the same content cannot disturb each other or a
        reader that already has the file open (store files are never modified
        in place). Where hard links are unavailable it falls back to a rename.
        """
        fd, tmp_name = tempfile.mkstemp(
            dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp"
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            shutil.copy2(source, tmp)
            try:
                os.link(tmp, dest)
            except FileExistsError:
                pass  # another store of the same content won the race
            except OSError:
                os.replace(tmp, dest)  # no hard links on this filesystem
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def _rel(self, content_hash: str, subdir: str, suffix: str) -> str:
        """Build the relative path string for a content hash."""
        filename = f"{content_hash}{suffix}"
        return f"{subdir}/{content_hash[:2]}/{filename}"
