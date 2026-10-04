"""Server backup and restore: a SQLite snapshot plus the content-addressed store.

A backup holds everything the server needs to come back after a lost disk:
the whole database (profiles, devices and their API keys, assignments, the
import queue, and any table added later) and, unless ``db_only`` is set, the
processed media store.

Two target kinds are supported:

* **Archive** (``*.tar.zst``): one self-contained, zstd-compressed tarball.
* **Directory** (anything else): ``kidsplay.db`` plus ``media/`` mirroring the
  store. Because the store is content-addressed, a file that is already in the
  directory is never copied again, so repeated backups are incremental.

Consistency while the server runs
---------------------------------
The ingest pipeline writes each media file into the store *before* inserting
the ``processed_files`` row that references it, and commits only after every
file is on disk. Media files are never modified or deleted afterwards. So the
backup:

1. snapshots the database first with the ``sqlite3`` backup API (a consistent,
   committed view, safe alongside live writers); every file that snapshot
   references was complete before the snapshot was taken;
2. then copies the store, checking every file's SHA-256 against the hash in its
   name. A file an ingest is still writing fails that check and is left out; the
   snapshot cannot reference it because its row was not committed yet.

Archives and the directory's ``kidsplay.db`` contain device API keys in
plaintext, so they are created with ``0600`` permissions (``0700`` for a new
backup directory).

Typical usage::

    result = create_backup(db_path, media_root, Path("/backups/kp.tar.zst"))
    restore_backup(Path("/backups/kp.tar.zst"), db_path, media_root)
"""

import contextlib
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import zstandard

from kidsplay_server.database import ensure_private_db_file

logger = logging.getLogger(__name__)

ARCHIVE_SUFFIX = ".tar.zst"
DB_NAME = "kidsplay.db"
MEDIA_DIR = "media"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1

_CHUNK = 1024 * 1024
_SECRET_FILE_MODE = 0o600
_SECRET_DIR_MODE = 0o700

# Store layout: {subdir}/{hash[:2]}/{hash}{suffix}, e.g.
# "thumbnails/ab/ab12…ef_200x200.webp". Anything else in the store (temp files,
# stray files) is not part of the content-addressed library and is not backed up.
_MEDIA_REL_RE = re.compile(
    r"^[A-Za-z0-9_-]+/(?P<prefix>[0-9a-f]{2})/(?P<hash>[0-9a-f]{64})[A-Za-z0-9_.-]*$"
)


class BackupError(Exception):
    """Raised when a backup or restore cannot be carried out."""


@dataclass
class BackupResult:
    """Outcome of ``create_backup``.

    Attributes:
        target: The archive file or directory written.
        media_copied: Media files written to the target by this run.
        media_already_present: Files skipped because an incremental directory
            target already had them.
        media_rejected: Store files left out because their content did not
            match the hash in their name (e.g. still being written by an ingest).
        media_repaired: With ``verify``, files the directory already held whose
            content no longer matched their hash, and that were rewritten from
            the store (also counted in ``media_copied``).
        missing_referenced: Store paths the database snapshot references that
            are not in the backup. Non-empty means the backup is incomplete.
    """

    target: Path
    media_copied: int = 0
    media_already_present: int = 0
    media_rejected: list[str] = field(default_factory=list)
    media_repaired: list[str] = field(default_factory=list)
    missing_referenced: list[str] = field(default_factory=list)


@dataclass
class RestoreResult:
    """Outcome of ``restore_backup``.

    Attributes:
        media_restored: Media files written into the store.
        media_rejected: Backup files not restored because their content did not
            match the hash in their name (a corrupt backup).
        missing_referenced: Store paths the restored database references that
            are not present in the store after the restore.
    """

    media_restored: int = 0
    media_rejected: list[str] = field(default_factory=list)
    missing_referenced: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_archive_path(path: Path) -> bool:
    """Return True if ``path`` names a ``.tar.zst`` archive target.

    Args:
        path: Backup target or restore source.
    """
    return path.name.lower().endswith(ARCHIVE_SUFFIX)


def check_backup_name(path: Path) -> None:
    """Reject archive-looking names that would not be written as archives.

    ``kidsplay-server backup foo.tar.gz`` would otherwise create a *directory*
    called ``foo.tar.gz``. Only ``.tar.zst`` is an archive; a name that looks
    like another archive format (``.tar``, ``.tar.*``, ``.tgz``, ``.zst``) is
    almost certainly a typo.

    Args:
        path: Backup target.

    Raises:
        BackupError: If the name looks like a non-``.tar.zst`` archive.
    """
    name = path.name.lower()
    if name.endswith(ARCHIVE_SUFFIX):
        return
    if name.endswith((".tar", ".tgz", ".zst")) or ".tar." in name:
        raise BackupError(
            f"{path.name!r} looks like an archive name, but only {ARCHIVE_SUFFIX} "
            f"archives are supported. Use a name ending in {ARCHIVE_SUFFIX}, or a "
            "name without an archive extension for a backup directory."
        )


def snapshot_database(db_path: Path, dest: Path) -> None:
    """Write a consistent copy of a live SQLite database to ``dest``.

    Uses the ``sqlite3`` online backup API on the whole database, so it copies
    every table (including ones added in later versions) as of one committed
    state, and is safe while the server is reading and writing.

    Args:
        db_path: The server's database file.
        dest: File to write the snapshot to. Overwritten if it exists.

    Raises:
        BackupError: If ``db_path`` does not exist or the snapshot fails its
            integrity check.
    """
    if not db_path.is_file():
        raise BackupError(f"Database not found: {db_path}")
    with (
        contextlib.closing(sqlite3.connect(db_path, timeout=30)) as src,
        contextlib.closing(sqlite3.connect(dest)) as dst,
    ):
        src.backup(dst)
    _check_integrity(dest, "Database snapshot")


def _check_integrity(db_path: Path, what: str) -> None:
    """Run ``PRAGMA quick_check`` on a database file.

    Args:
        db_path: The database to check.
        what: What it is, for the error message.

    Raises:
        BackupError: If the file is not a healthy SQLite database.
    """
    try:
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            (status,) = conn.execute("PRAGMA quick_check").fetchone()
    except (sqlite3.Error, UnicodeDecodeError) as exc:
        # Damaged text pages can fail to decode before quick_check reports them.
        status = str(exc)
    if status != "ok":
        raise BackupError(f"{what} failed its integrity check: {status}")


def iter_media_files(media_root: Path) -> Iterator[tuple[str, Path]]:
    """Yield the content-addressed files in a media store.

    Files whose path does not follow the store layout are skipped. Names are
    not verified against content here; see ``create_backup``.

    Args:
        media_root: Root of the media store. A missing root yields nothing.

    Yields:
        ``(relative_path, absolute_path)`` pairs, relative paths POSIX-style
        and in sorted order.
    """
    if not media_root.is_dir():
        return
    for path in sorted(media_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(media_root).as_posix()
        if _parse_media_rel(rel) is not None:
            yield rel, path


def is_restore_target_empty(db_path: Path, media_root: Path) -> bool:
    """Return True if restoring into these paths would overwrite nothing.

    The database counts as empty when it is missing or none of its tables has
    a row, so a server that has started once (and created its schema) but holds
    no data can still be restored into without ``force``.

    Args:
        db_path: Target database file.
        media_root: Target media store root.
    """
    if media_root.is_dir() and any(p.is_file() for p in media_root.rglob("*")):
        return False
    if not db_path.exists() or db_path.stat().st_size == 0:
        return True
    with contextlib.closing(sqlite3.connect(db_path, timeout=30)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            if conn.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                return False
    return True


def create_backup(
    db_path: Path,
    media_root: Path,
    dest: Path,
    *,
    db_only: bool = False,
    verify: bool = False,
) -> BackupResult:
    """Back up the database and (optionally) the media store.

    ``dest`` ending in ``.tar.zst`` writes a compressed archive; any other path
    is treated as a directory target that is updated incrementally.

    Args:
        db_path: The server's database file.
        media_root: Root of the media store.
        dest: Archive file or backup directory.
        db_only: Back up only the database. Media can be re-imported, but
            device keys and assignments cannot.
        verify: For a directory target, re-hash the files it already holds
            instead of trusting them, and rewrite any that no longer match
            (bit rot). Slower: reads the whole existing backup. An archive is
            always written from scratch, so this has no effect on it.

    Returns:
        A ``BackupResult`` describing what was written.

    Raises:
        BackupError: If the database is missing, the target is unusable or its
            name looks like an unsupported archive format.
    """
    check_backup_name(dest)
    if is_archive_path(dest):
        return _backup_to_archive(db_path, media_root, dest, db_only=db_only)
    return _backup_to_directory(
        db_path, media_root, dest, db_only=db_only, verify=verify
    )


def restore_backup(
    source: Path, db_path: Path, media_root: Path, *, force: bool = False
) -> RestoreResult:
    """Restore a backup made by ``create_backup``.

    Media files are restored first and the database last, so the restored
    database never references a file that has not been written yet. Every
    media file is checked against the hash in its name before it is placed.
    Media is staged until the whole source has been read and validated, so a
    truncated or malformed backup leaves the target as it was.

    Stop the server (or at least make sure nothing is ingesting) before
    restoring over a live installation.

    Args:
        source: A ``.tar.zst`` archive or a backup directory.
        db_path: Database file to restore into.
        media_root: Media store root to restore into.
        force: Restore even if the target already holds data. Existing media
            files are kept; the database is replaced.

    Returns:
        A ``RestoreResult`` describing what was restored.

    Raises:
        BackupError: If the source is missing or malformed, or the target is
            not empty and ``force`` is False.
    """
    if not source.exists():
        raise BackupError(f"Backup not found: {source}")
    if not force and not is_restore_target_empty(db_path, media_root):
        raise BackupError(
            "Restore target is not empty "
            f"(database {db_path}, media store {media_root}); "
            "use --force to overwrite it."
        )
    if source.is_dir():
        return _restore_from_directory(source, db_path, media_root)
    return _restore_from_archive(source, db_path, media_root)


# ---------------------------------------------------------------------------
# Backup internals
# ---------------------------------------------------------------------------


def _backup_to_archive(
    db_path: Path, media_root: Path, dest: Path, *, db_only: bool
) -> BackupResult:
    """Write a ``.tar.zst`` archive, atomically and with ``0600`` permissions."""
    result = BackupResult(target=dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".kidsplay-") as tmp:
        tmp_dir = Path(tmp)
        os.chmod(tmp_dir, _SECRET_DIR_MODE)
        snapshot = tmp_dir / DB_NAME
        # Snapshot first: everything it references is already complete on disk.
        snapshot_database(db_path, snapshot)
        referenced = _referenced_paths(snapshot)

        partial = tmp_dir / dest.name
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _SECRET_FILE_MODE)
        os.fchmod(fd, _SECRET_FILE_MODE)
        included: set[str] = set()
        with (
            os.fdopen(fd, "wb") as raw,
            zstandard.ZstdCompressor(level=10).stream_writer(raw) as zst,
            tarfile.open(fileobj=zst, mode="w|") as tar,
        ):
            if not db_only:
                for rel, path in iter_media_files(media_root):
                    if _add_verified_media(tar, rel, path):
                        included.add(rel)
                        result.media_copied += 1
                    else:
                        result.media_rejected.append(rel)
            tar.add(snapshot, arcname=DB_NAME, filter=_secret_member)
            _add_bytes(tar, MANIFEST_NAME, _manifest(db_only, result))
        os.replace(partial, dest)

    if not db_only:
        result.missing_referenced = sorted(referenced - included)
    _log_backup(result)
    return result


def _backup_to_directory(
    db_path: Path, media_root: Path, dest: Path, *, db_only: bool, verify: bool
) -> BackupResult:
    """Update a directory backup, copying only media it does not already have."""
    result = BackupResult(target=dest)
    if dest.exists() and not dest.is_dir():
        raise BackupError(
            f"Backup target {dest} exists and is not a directory "
            f"(archive targets must end in {ARCHIVE_SUFFIX})."
        )
    if not dest.exists():
        dest.mkdir(parents=True, mode=_SECRET_DIR_MODE)
        os.chmod(dest, _SECRET_DIR_MODE)

    fd, tmp_name = tempfile.mkstemp(dir=dest, prefix=f".{DB_NAME}.", suffix=".tmp")
    os.close(fd)
    snapshot = Path(tmp_name)
    try:
        snapshot_database(db_path, snapshot)
        os.chmod(snapshot, _SECRET_FILE_MODE)
        referenced = _referenced_paths(snapshot)

        present: set[str] = set()
        if not db_only:
            dest_media = dest / MEDIA_DIR
            for rel, path in iter_media_files(media_root):
                target = dest_media / rel
                was_corrupt = False
                if target.is_file():
                    if not verify or _file_matches(target, _expected_hash(rel)):
                        result.media_already_present += 1
                        present.add(rel)
                        continue
                    logger.warning("Backup copy of %s is corrupt; rewriting it", rel)
                    was_corrupt = True
                with path.open("rb") as src:
                    ok = _write_verified(src, target, _expected_hash(rel))
                if ok:
                    result.media_copied += 1
                    present.add(rel)
                    if was_corrupt:
                        result.media_repaired.append(rel)
                else:
                    result.media_rejected.append(rel)
            result.missing_referenced = sorted(referenced - present)

        # Publish the database only after the media it references is in place.
        os.replace(snapshot, dest / DB_NAME)
    finally:
        snapshot.unlink(missing_ok=True)
    _atomic_write_bytes(dest / MANIFEST_NAME, _manifest(db_only, result))
    _log_backup(result)
    return result


def _add_verified_media(tar: tarfile.TarFile, rel: str, path: Path) -> bool:
    """Add a store file to ``tar`` if its content matches its name.

    The file is hashed first and then streamed into the archive. A complete
    store file never changes, so a file that verifies is safe to re-read.

    Returns:
        True if the file was added, False if it was rejected.
    """
    expected = _expected_hash(rel)
    with path.open("rb") as f:
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
        if digest.hexdigest() != expected:
            logger.info("Skipping %s: content does not match its hash", rel)
            return False
        f.seek(0)
        info = tarfile.TarInfo(f"{MEDIA_DIR}/{rel}")
        info.size = size
        info.mtime = int(path.stat().st_mtime)
        info.mode = 0o644
        tar.addfile(info, f)
    return True


def _secret_member(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Tar filter for the database member: owner-only mode, no user names."""
    info.mode = _SECRET_FILE_MODE
    info.uname = info.gname = ""
    info.uid = info.gid = 0
    return info


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add an in-memory file to ``tar``."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(datetime.now(UTC).timestamp())
    info.mode = _SECRET_FILE_MODE
    tar.addfile(info, io.BytesIO(data))


def _manifest(db_only: bool, result: BackupResult) -> bytes:
    """Serialise the backup manifest."""
    return json.dumps(
        {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "db_only": db_only,
            "media_files": result.media_copied + result.media_already_present,
        },
        indent=2,
    ).encode()


def _log_backup(result: BackupResult) -> None:
    """Log a summary of a finished backup."""
    logger.info(
        "Backup to %s: %d media copied (%d repaired), %d already present, %d rejected",
        result.target,
        result.media_copied,
        len(result.media_repaired),
        result.media_already_present,
        len(result.media_rejected),
    )
    for rel in result.missing_referenced:
        logger.warning("Referenced media missing from backup: %s", rel)


# ---------------------------------------------------------------------------
# Restore internals
# ---------------------------------------------------------------------------


@contextmanager
def _staging_dir(parent: Path) -> Iterator[Path]:
    """Yield an owner-only temporary directory inside ``parent``.

    Staging next to the destination keeps the final move a same-filesystem
    rename, and avoids /tmp, which is often small in a container. The name
    starts with a dot, so it never matches the store layout and a leftover
    (after a hard kill) is never mistaken for media or backed up.

    If ``parent`` did not exist and the restore fails, the directories created
    for it are removed again, so a rejected backup leaves no empty ``media/``.
    """
    created: list[Path] = []
    for missing in (parent, *parent.parents):
        if missing.exists():
            break
        created.append(missing)
    parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            dir=parent, prefix=".kidsplay-restore-"
        ) as tmp:
            os.chmod(tmp, _SECRET_DIR_MODE)
            yield Path(tmp)
    except BaseException:
        for directory in created:  # deepest first
            with contextlib.suppress(OSError):  # not empty: something else is there
                directory.rmdir()
        raise


def _restore_from_archive(
    source: Path, db_path: Path, media_root: Path
) -> RestoreResult:
    """Stream an archive into staging, then place its media and database."""
    result = RestoreResult()
    manifest_seen = False
    with _staging_dir(db_path.parent) as tmp, _staging_dir(media_root) as staged:
        snapshot = tmp / DB_NAME
        try:
            with (
                source.open("rb") as raw,
                zstandard.ZstdDecompressor().stream_reader(raw) as zst,
                tarfile.open(fileobj=zst, mode="r|") as tar,
            ):
                for member in tar:
                    fobj = tar.extractfile(member) if member.isfile() else None
                    if fobj is None:
                        raise BackupError(f"Unexpected archive entry: {member.name}")
                    if member.name == DB_NAME:
                        if snapshot.exists():
                            raise BackupError(
                                f"Archive {source} contains more than one database."
                            )
                        _copy_stream(fobj, snapshot)
                    elif member.name == MANIFEST_NAME:
                        _check_manifest(fobj.read())
                        manifest_seen = True
                    else:
                        _restore_media_member(member.name, fobj, staged, result)
        except (tarfile.TarError, zstandard.ZstdError) as exc:
            raise BackupError(f"Cannot read archive {source}: {exc}") from exc
        if not manifest_seen or not snapshot.is_file():
            raise BackupError(f"Archive {source} is incomplete or not a backup.")
        _place_staged(staged, media_root, snapshot, db_path)
    result.missing_referenced = _missing_in_store(db_path, media_root)
    return result


def _restore_from_directory(
    source: Path, db_path: Path, media_root: Path
) -> RestoreResult:
    """Restore a directory backup: media first, database last."""
    snapshot = source / DB_NAME
    manifest = source / MANIFEST_NAME
    if not snapshot.is_file() or not manifest.is_file():
        raise BackupError(f"{source} is not a KidsPlay backup directory.")
    _check_manifest(manifest.read_bytes())
    result = RestoreResult()
    with _staging_dir(media_root) as staged:
        for rel, path in iter_media_files(source / MEDIA_DIR):
            with path.open("rb") as f:
                _restore_media_member(f"{MEDIA_DIR}/{rel}", f, staged, result)
        _place_staged(staged, media_root, snapshot, db_path)
    result.missing_referenced = _missing_in_store(db_path, media_root)
    return result


def _place_staged(
    staged: Path, media_root: Path, snapshot: Path, db_path: Path
) -> None:
    """Move validated media from staging into the store, then the database.

    The snapshot is integrity-checked first, so a damaged database aborts the
    restore before anything in the target has been touched.
    """
    _check_integrity(snapshot, "The backup's database")
    for rel, path in iter_media_files(staged):
        dest = media_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, dest)
    try:
        _restore_database(snapshot, db_path)
    except sqlite3.Error as exc:
        raise BackupError(
            f"Media was restored but the database could not be written ({exc}). "
            "Stop the server and re-run the restore with --force."
        ) from exc


def _restore_media_member(
    name: str, stream: IO[bytes], media_root: Path, result: RestoreResult
) -> None:
    """Validate a ``media/…`` entry's name and write it into the store."""
    rel = name.removeprefix(f"{MEDIA_DIR}/")
    if rel == name or _parse_media_rel(rel) is None:
        raise BackupError(f"Unexpected archive entry: {name}")
    if _write_verified(stream, media_root / rel, _expected_hash(rel)):
        result.media_restored += 1
    else:
        logger.warning("Not restoring %s: content does not match its hash", rel)
        result.media_rejected.append(rel)


def _restore_database(snapshot: Path, db_path: Path) -> None:
    """Replace the target database's contents with ``snapshot``.

    Uses the backup API in the other direction, so an existing database (and
    any open connection to it) sees a clean, atomic switch to the new content.
    The database is left owner-only (``0600``): it holds device API keys.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_db_file(db_path)
    with (
        contextlib.closing(sqlite3.connect(snapshot)) as src,
        contextlib.closing(sqlite3.connect(db_path, timeout=30)) as dst,
    ):
        src.backup(dst)


def _check_manifest(data: bytes) -> None:
    """Reject a manifest from an unknown (newer) backup format."""
    try:
        version = json.loads(data)["format_version"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"Malformed backup manifest: {exc}") from exc
    if version != FORMAT_VERSION:
        raise BackupError(f"Unsupported backup format version: {version}")


def _missing_in_store(db_path: Path, media_root: Path) -> list[str]:
    """Return store paths the database references that are not on disk."""
    return sorted(
        p for p in _referenced_paths(db_path) if not (media_root / p).is_file()
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_media_rel(rel: str) -> re.Match[str] | None:
    """Match a store-relative path against the content-addressed layout."""
    match = _MEDIA_REL_RE.match(rel)
    if match is None or not match["hash"].startswith(match["prefix"]):
        return None
    return match


def _expected_hash(rel: str) -> str:
    """Return the SHA-256 encoded in a valid store-relative path."""
    match = _parse_media_rel(rel)
    if match is None:
        raise BackupError(f"Not a content-addressed store path: {rel}")
    return match["hash"]


# Tables whose rows point at store files: (table, column).
_REFERENCING_COLUMNS = (
    ("processed_files", "relative_path"),
    ("theme_assets", "relative_path"),
)


def _referenced_paths(db_path: Path) -> set[str]:
    """Return the store paths the database's file-referencing tables point at.

    This is the one place the backup looks at specific tables, and only to
    check completeness; the database itself is always copied whole. Add a new
    table here when it starts referencing store files.
    """
    paths: set[str] = set()
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        for table, column in _REFERENCING_COLUMNS:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if has_table:
                # Names come from the constant above, never from input.
                rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
                paths.update(row[0] for row in rows)
    return paths


def _file_matches(path: Path, expected_hash: str) -> bool:
    """Return True if ``path`` is readable and its SHA-256 is ``expected_hash``."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected_hash


def _write_verified(stream: IO[bytes], dest: Path, expected_hash: str) -> bool:
    """Copy ``stream`` to ``dest`` if its SHA-256 equals ``expected_hash``.

    Writes to a temporary file next to ``dest`` and renames it into place, so
    ``dest`` never exists half-written.

    Returns:
        True if ``dest`` was written, False if the content did not match.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as out:
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected_hash:
            return False
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _copy_stream(stream: IO[bytes], dest: Path) -> None:
    """Copy a stream to a new owner-only file."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _SECRET_FILE_MODE)
    with os.fdopen(fd, "wb") as out:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            out.write(chunk)


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` via a temp file and rename, mode ``0600``."""
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp_name, dest)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
