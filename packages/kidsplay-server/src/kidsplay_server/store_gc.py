"""Garbage collection for the content-addressed media store.

Nothing in the server deletes a store file: the backup relies on stored files
never changing or vanishing under it. That leaves files no database row points
to any more, mostly the previous normalized MP3 of an item that was
re-normalized (a new loudness target), but also the files of deleted items.
``collect_garbage`` removes them, carefully.

A file is deleted only if it has been **unreferenced for a whole grace period**
(``DEFAULT_GRACE``, seven days), as observed over several runs:

1. The first run that finds a file unreferenced records it in
   ``store_gc_pending`` with the time. It deletes nothing.
2. A run that finds it referenced again forgets it.
3. A run that finds it still unreferenced once the grace period has passed
   deletes it, after checking again, under the database write lock, that no
   row points to it.

Why this is safe next to ``kidsplay-server backup`` (``docs/BACKUP.md``): a
backup copies the database first, then the files that copy references. A file
in that copy became unreferenced no earlier than the backup started, so it is
not deleted before the backup started plus the grace period. Any backup that
finishes within the grace period finds all its files. A file being written by
an ingest (not yet referenced) is protected the same way: it is only ever
deleted after a full grace period without a row.

The one remaining hazard is an ingest that stores a file *identical to a
pending one* (a re-import of a song whose item was deleted) during the moments
between two GC passes over the same file; run it while no import is running
(``docs/LOUDNESS.md``).

Only ``audio/``, ``thumbnails/`` and ``photos/`` are scanned: the directories
of the media pipeline. Theme assets, and anything else in the store root, are
never touched.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from kidsplay_server.database import configure_conn, init_db

logger = logging.getLogger(__name__)

DEFAULT_GRACE = timedelta(days=7)
"""How long a file must stay unreferenced before it is deleted."""

SCANNED_SUBDIRS: tuple[str, ...] = ("audio", "thumbnails", "photos")
"""Store subdirectories that hold pipeline output, and so are collected."""


@dataclass
class GcResult:
    """What one ``collect_garbage`` run found and did.

    Attributes:
        scanned: Files found in the scanned directories.
        referenced: Of those, files a database row points to.
        newly_unreferenced: Unreferenced files seen for the first time. They
            start their grace period now.
        in_grace: Unreferenced files still inside their grace period.
        deleted: Files deleted (or, in a dry run, that would be).
        freed_bytes: Their total size.
        deleted_paths: Relative paths of the deleted files.
    """

    scanned: int = 0
    referenced: int = 0
    newly_unreferenced: int = 0
    in_grace: int = 0
    deleted: int = 0
    freed_bytes: int = 0
    deleted_paths: list[str] = field(default_factory=list)


def _scan(root: Path) -> dict[str, Path]:
    """Map the relative POSIX path of every scanned store file to its path."""
    found: dict[str, Path] = {}
    for subdir in SCANNED_SUBDIRS:
        for directory, _dirs, names in os.walk(root / subdir):
            for name in names:
                path = Path(directory) / name
                if path.is_file() and not path.is_symlink():
                    found[path.relative_to(root).as_posix()] = path
    return found


async def _referenced_paths(conn: aiosqlite.Connection) -> set[str]:
    """Every store path some row points to."""
    paths: set[str] = set()
    for table in ("processed_files", "theme_assets"):
        async with conn.execute(f"SELECT relative_path FROM {table}") as cur:
            paths.update(row[0] for row in await cur.fetchall())
    return paths


async def collect_garbage(
    db_path: Path,
    store_root: Path,
    *,
    grace: timedelta = DEFAULT_GRACE,
    dry_run: bool = False,
    now: datetime | None = None,
) -> GcResult:
    """Delete store files that have been unreferenced for the grace period.

    Safe to run while the server is up. See the module docstring for the
    rules and why they keep backups intact.

    Args:
        db_path: The server database.
        store_root: Root of the media store.
        grace: How long a file must stay unreferenced before it is deleted.
        dry_run: Report what would happen without deleting anything or
            recording first sightings.
        now: The current time (tests set it).

    Returns:
        What was found and deleted.
    """
    now = now or datetime.now()
    result = GcResult()
    files = _scan(store_root)
    result.scanned = len(files)

    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        # The write lock for the whole pass: no ingest commits a row between
        # our last look at the references and the deletes.
        await conn.execute("BEGIN IMMEDIATE")
        try:
            referenced = await _referenced_paths(conn)
            async with conn.execute(
                "SELECT relative_path, first_seen FROM store_gc_pending"
            ) as cur:
                pending = {
                    row[0]: datetime.fromisoformat(row[1])
                    for row in await cur.fetchall()
                }

            unreferenced = {rel for rel in files if rel not in referenced}
            result.referenced = len(files) - len(unreferenced)
            # Referenced again, or gone from disk: nothing to track.
            forget = [rel for rel in pending if rel not in unreferenced]
            new = sorted(rel for rel in unreferenced if rel not in pending)
            result.newly_unreferenced = len(new)

            for rel in sorted(unreferenced & pending.keys()):
                if now - pending[rel] < grace:
                    result.in_grace += 1
                    continue
                path = files[rel]
                try:
                    size = path.stat().st_size
                    if not dry_run:
                        path.unlink()
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", rel, exc)
                    continue
                result.deleted += 1
                result.freed_bytes += size
                result.deleted_paths.append(rel)
                forget.append(rel)

            if not dry_run:
                await conn.executemany(
                    "DELETE FROM store_gc_pending WHERE relative_path = ?",
                    [(rel,) for rel in forget],
                )
                await conn.executemany(
                    "INSERT OR IGNORE INTO store_gc_pending "
                    "(relative_path, first_seen) VALUES (?, ?)",
                    [(rel, now.isoformat()) for rel in new],
                )
                await conn.commit()
            else:
                await conn.rollback()
        except BaseException:
            if conn.in_transaction:
                await conn.rollback()
            raise
    logger.info(
        "Store GC%s: %d scanned, %d deleted (%d bytes), %d waiting, %d new",
        " (dry run)" if dry_run else "",
        result.scanned,
        result.deleted,
        result.freed_bytes,
        result.in_grace,
        result.newly_unreferenced,
    )
    return result
