"""Local sync transport: take files from a media store on this machine.

Used in all-in-one mode (``sync_transport = "local"``), where the server runs
on the same device. Only the "fetch file" step of a sync differs from HTTP:
the manifest is still diffed and applied exactly as before.

A file is **hard-linked** from the server's media store into ``media_root``,
so it takes no extra space. If the link fails (a different filesystem, or one
without hard links, such as FAT) the file is **copied** instead, verified
against its SHA-256, and moved into place atomically.

Read this before changing anything here: a hard link *shares its inode with
the server's file*, and the server's backups rely on media-store files never
being modified. The device therefore

- never opens a store file or a linked file for writing (links are created
  with ``os.link``; copies are written to a new file and renamed into place);
- only ever removes files with ``unlink``, which drops the device's name for
  the inode and leaves the server's file and content untouched.
"""

import contextlib
import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


class LocalFetchError(Exception):
    """A file could not be taken from the server's media store."""


def _source_path(store_root: Path, relative_path: str) -> Path:
    """Resolve a manifest path inside the media store.

    Args:
        store_root: The server's media store directory.
        relative_path: ``SyncFileEntry.relative_path`` from the manifest.

    Returns:
        The absolute path of the file in the store.

    Raises:
        LocalFetchError: If the path is absolute or escapes the store.
    """
    root = store_root.expanduser().resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise LocalFetchError(f"path {relative_path!r} is outside the media store")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_from_store(
    store_root: Path,
    relative_path: str,
    content_hash: str,
    size_bytes: int,
    dest: Path,
) -> Literal["link", "copy"]:
    """Put a store file at ``dest``: hard link if possible, else a copy.

    ``dest`` must not exist. A link is not re-hashed (the store is
    content-addressed and immutable; reading every file again would cost a
    full pass over the SD card), but its size must match the manifest, which
    catches a file that is missing or still being written. A copy is hashed.

    Args:
        store_root: The server's media store directory.
        relative_path: Path of the file below ``store_root`` (same as below
            the device's ``media_root``).
        content_hash: Expected SHA-256 hex digest.
        size_bytes: Expected size in bytes.
        dest: Where the file goes on the device.

    Returns:
        ``"link"`` or ``"copy"``, whichever was used.

    Raises:
        LocalFetchError: If the source is missing, has the wrong size or
            hash, or cannot be linked or copied.
    """
    src = _source_path(store_root, relative_path)
    try:
        stat = src.stat()
    except OSError as exc:
        raise LocalFetchError(f"{src} is not readable: {exc}") from exc
    if not src.is_file():
        raise LocalFetchError(f"{src} is not a regular file")
    if stat.st_size != size_bytes:
        raise LocalFetchError(
            f"{src} is {stat.st_size} bytes, the manifest says {size_bytes}"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except FileExistsError:
        raise LocalFetchError(f"{dest} already exists") from None
    except OSError as exc:
        logger.info("Cannot hard-link %s (%s); copying instead", relative_path, exc)
    else:
        return "link"

    # Not the same filesystem (or no hard links there): copy, verify, rename.
    part = dest.with_name(dest.name + ".part")
    try:
        shutil.copyfile(src, part)
        actual = _sha256(part)
        if actual != content_hash:
            raise LocalFetchError(
                f"hash mismatch for {relative_path}: got {actual}, "
                f"expected {content_hash}"
            )
        os.replace(part, dest)
    except OSError as exc:
        raise LocalFetchError(f"cannot copy {src} to {dest}: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
    return "copy"
