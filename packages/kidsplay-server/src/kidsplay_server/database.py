"""Server-side SQLite database layer.

All database access uses raw SQL with parameter binding. No ORM.
Functions accept an ``aiosqlite.Connection`` — callers own the
connection lifecycle.

Typical usage:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await create_profile(conn, Profile(name="Leo"))
        await conn.commit()
        profile = await get_profile(conn, some_uuid)

Write functions do **not** call ``conn.commit()`` themselves.
The caller commits after each logical unit of work.  This lets the
processing pipeline batch multiple inserts into one transaction.

The sole exception is ``delete_media_item``, which commits internally
so the three-table cascade delete is always atomic.
"""

import contextlib
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from kidsplay_models import (
    Device,
    MediaItem,
    ProcessedFile,
    Profile,
    ProfileMediaAssignment,
    ProfileSettings,
    QueueItem,
    ThemeAsset,
    ThemeAssetRole,
    ThemeColors,
    ThemeDefinition,
)
from kidsplay_models.media import MediaType
from kidsplay_models.queue import LOUDNESS_JOB, QueueStatus

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_settings (
    profile_id  TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    settings    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Custom themes (#6). Built-in themes are code (kidsplay_models.themes) and
-- have no row. An asset row points at a file in the media store under
-- themes/, which, like all store files, is never modified or deleted.
CREATE TABLE IF NOT EXISTS themes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    colors      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS theme_assets (
    theme_id       TEXT NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    relative_path  TEXT NOT NULL,
    mime_type      TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    PRIMARY KEY (theme_id, role)
);

CREATE INDEX IF NOT EXISTS idx_theme_assets_hash ON theme_assets(content_hash);

CREATE TABLE IF NOT EXISTS server_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    profile_id              TEXT NOT NULL REFERENCES profiles(id),
    display_width           INTEGER NOT NULL DEFAULT 640,
    display_height          INTEGER NOT NULL DEFAULT 480,
    last_sync_at            TEXT,
    last_sync_manifest_hash TEXT,
    api_key                 TEXT NOT NULL UNIQUE,
    created_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_items (
    id                 TEXT PRIMARY KEY,
    media_type         TEXT NOT NULL,
    content_hash       TEXT UNIQUE NOT NULL,
    playlist_title     TEXT NOT NULL,
    title              TEXT NOT NULL,
    artist             TEXT,
    duration_seconds   INTEGER,
    processing_status  TEXT NOT NULL DEFAULT 'pending',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    loudness_source_lufs            REAL,
    loudness_source_true_peak_dbtp  REAL,
    loudness_gain_db                REAL,
    loudness_target_lufs            REAL,
    loudness_target_true_peak_dbtp  REAL,
    loudness_mode                   TEXT
);

CREATE TABLE IF NOT EXISTS processed_files (
    id            TEXT PRIMARY KEY,
    media_id      TEXT NOT NULL REFERENCES media_items(id),
    content_hash  TEXT NOT NULL,
    file_type     TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    mime_type     TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_media (
    profile_id  TEXT NOT NULL REFERENCES profiles(id),
    media_id    TEXT NOT NULL REFERENCES media_items(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, media_id)
);

CREATE TABLE IF NOT EXISTS import_queue (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    media_type      TEXT NOT NULL,
    playlist_title  TEXT NOT NULL,
    profile_ids     TEXT NOT NULL DEFAULT '[]',
    title_override  TEXT,
    artist_override TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempt         INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 5,
    last_error      TEXT,
    log             TEXT NOT NULL DEFAULT '',
    media_id        TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,
    importer        TEXT
);

CREATE TABLE IF NOT EXISTS store_gc_pending (
    relative_path   TEXT PRIMARY KEY,
    first_seen      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pairing_requests (
    code            TEXT PRIMARY KEY,
    secret_hash     TEXT NOT NULL,
    status          TEXT NOT NULL,
    device_name     TEXT NOT NULL,
    display_width   INTEGER NOT NULL,
    display_height  INTEGER NOT NULL,
    client          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    device_id       TEXT
);

CREATE TABLE IF NOT EXISTS server_identity (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    server_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

# Ordered schema migrations. ``PRAGMA user_version`` records how many have been
# applied, so step N runs exactly once, on a database at version N-1.
#
# ``_CREATE_TABLES_SQL`` always describes the *latest* schema. A brand-new
# database is created from it directly and stamped with the latest version; the
# steps below only ever run against a database created by an older release, so
# each one may assume the schema of the version before it. To change the
# schema: update ``_CREATE_TABLES_SQL`` and append a step here. Adding a new
# table needs no step, because ``CREATE TABLE IF NOT EXISTS`` also runs on
# upgraded databases.
_MIGRATIONS: tuple[str, ...] = (
    # 1: the YouTube-only ``yt_queue`` became the generic import queue. Rename
    # keeps every row; ``importer`` stays NULL on old rows, so the worker picks
    # an importer by URL for them.
    """
    ALTER TABLE yt_queue RENAME TO import_queue;
    ALTER TABLE import_queue ADD COLUMN importer TEXT;
    """,
    # 2: loudness normalization (#8). All NULL on existing rows, which reads
    # as "not normalized yet", so ``kidsplay media normalize --all`` picks
    # them up.
    """
    ALTER TABLE media_items ADD COLUMN loudness_source_lufs REAL;
    ALTER TABLE media_items ADD COLUMN loudness_source_true_peak_dbtp REAL;
    ALTER TABLE media_items ADD COLUMN loudness_gain_db REAL;
    ALTER TABLE media_items ADD COLUMN loudness_target_lufs REAL;
    ALTER TABLE media_items ADD COLUMN loudness_target_true_peak_dbtp REAL;
    """,
    # 3: how loudnorm applied the gain (linear, dynamic or capped). NULL on
    # existing rows: normalized before the mode was recorded.
    """
    ALTER TABLE media_items ADD COLUMN loudness_mode TEXT;
    """,
)

SCHEMA_VERSION = len(_MIGRATIONS)
"""Schema version of a database after ``init_db``."""


# ---------------------------------------------------------------------------
# Connection setup
# ---------------------------------------------------------------------------


def ensure_private_db_file(db_path: Path) -> None:
    """Make the database file readable by its owner only (``0600``).

    The database holds device API keys in plaintext. A missing file is created
    empty first, so SQLite never creates it with umask permissions, not even
    briefly; an existing file (and its WAL/journal sidecars) is chmod-ed.
    Call before the first connection to ``db_path``.

    Args:
        db_path: The database file. Its directory must exist.
    """
    if not db_path.exists():
        with contextlib.suppress(FileExistsError):
            os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    for path in (
        db_path,
        *(db_path.with_name(db_path.name + s) for s in ("-wal", "-shm", "-journal")),
    ):
        with contextlib.suppress(FileNotFoundError):
            os.chmod(path, 0o600)


async def configure_conn(conn: aiosqlite.Connection) -> None:
    """Configure per-connection SQLite settings.

    Must be called immediately after every ``aiosqlite.connect()`` call.
    Enables foreign-key constraint enforcement and sets the row factory so
    that column values can be accessed by name.

    Args:
        conn: Open aiosqlite connection.
    """
    await conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row


async def get_schema_version(conn: aiosqlite.Connection) -> int:
    """Return the database's schema version (``PRAGMA user_version``).

    Args:
        conn: Open aiosqlite connection.

    Returns:
        Number of migrations applied; ``0`` for a database that predates
        versioning or has never been initialised.
    """
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ) as cur:
        return await cur.fetchone() is not None


async def init_db(conn: aiosqlite.Connection) -> None:
    """Create or upgrade the schema to ``SCHEMA_VERSION``.

    A new database gets the latest schema directly. An existing one has each
    pending step of ``_MIGRATIONS`` applied in order, keeping its rows. Tables
    missing from either are then created.

    Idempotent. Safe to call on an already-initialised database.
    Call ``configure_conn`` on the same connection before this.

    Args:
        conn: Open, configured aiosqlite connection.

    Raises:
        RuntimeError: If the database was written by a newer release (its
            version is above ``SCHEMA_VERSION``).
    """
    # Fast path: the schema is already current. init_db runs on every request
    # (see api.deps.get_db), so this is the common case. The version is only
    # ever stamped together with the tables, so a current version implies the
    # schema exists.
    if await get_schema_version(conn) == SCHEMA_VERSION:
        # Tables added since this database was created need no migration step.
        await conn.executescript(_CREATE_TABLES_SQL)
        return
    # Slow path: take the write lock first, then decide from what we read
    # under it. Deciding from an unlocked read races: a connection that read
    # version 0 while another was creating the schema would find the new
    # tables and try to "upgrade" them from version 0.
    await conn.execute("BEGIN IMMEDIATE")
    try:
        version = await get_schema_version(conn)
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {version} is newer than this server "
                f"supports ({SCHEMA_VERSION}); upgrade KidsPlay."
            )
        # ``profiles`` has existed since the first release, so its absence
        # means a new database rather than one to upgrade.
        if not await _table_exists(conn, "profiles"):
            await _run_statements(conn, _CREATE_TABLES_SQL)
        else:
            for step in _MIGRATIONS[version:]:
                await _run_statements(conn, step)
        # PRAGMA takes no bound parameters; SCHEMA_VERSION is our constant.
        # Stamped in the same transaction as the schema changes, so a crash
        # can never leave tables without the version that describes them.
        await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await conn.commit()
    except BaseException:
        if conn.in_transaction:
            await conn.rollback()
        raise
    # Tables added since this database was created need no migration step.
    await conn.executescript(_CREATE_TABLES_SQL)


async def _run_statements(conn: aiosqlite.Connection, script: str) -> None:
    """Run each statement of ``script`` inside the caller's transaction.

    ``executescript`` would commit the open transaction first, releasing the
    write lock init_db depends on, so statements run one at a time.
    """
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            await conn.execute(buffer)
            buffer = ""
    if buffer.strip():
        await conn.execute(buffer)


# ---------------------------------------------------------------------------
# Row → model helpers
# ---------------------------------------------------------------------------


def _media_item(row: sqlite3.Row) -> MediaItem:
    return MediaItem(
        id=uuid.UUID(row["id"]),
        media_type=MediaType(row["media_type"]),
        content_hash=row["content_hash"],
        playlist_title=row["playlist_title"],
        title=row["title"],
        artist=row["artist"],
        duration_seconds=row["duration_seconds"],
        processing_status=row["processing_status"],
        loudness_source_lufs=row["loudness_source_lufs"],
        loudness_source_true_peak_dbtp=row["loudness_source_true_peak_dbtp"],
        loudness_gain_db=row["loudness_gain_db"],
        loudness_target_lufs=row["loudness_target_lufs"],
        loudness_target_true_peak_dbtp=row["loudness_target_true_peak_dbtp"],
        loudness_mode=row["loudness_mode"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _processed_file(row: sqlite3.Row) -> ProcessedFile:
    return ProcessedFile(
        id=uuid.UUID(row["id"]),
        media_id=uuid.UUID(row["media_id"]),
        content_hash=row["content_hash"],
        file_type=row["file_type"],
        relative_path=row["relative_path"],
        size_bytes=row["size_bytes"],
        mime_type=row["mime_type"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _profile(row: sqlite3.Row) -> Profile:
    return Profile(
        id=uuid.UUID(row["id"]),
        name=row["name"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _device(row: sqlite3.Row) -> Device:
    return Device(
        id=uuid.UUID(row["id"]),
        name=row["name"],
        profile_id=uuid.UUID(row["profile_id"]),
        display_width=row["display_width"],
        display_height=row["display_height"],
        last_sync_at=(
            datetime.fromisoformat(row["last_sync_at"]) if row["last_sync_at"] else None
        ),
        last_sync_manifest_hash=row["last_sync_manifest_hash"],
        api_key=row["api_key"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# media_items
# ---------------------------------------------------------------------------


async def create_media_item(conn: aiosqlite.Connection, item: MediaItem) -> None:
    """Insert a media item row.

    Args:
        conn: Open, configured connection.
        item: MediaItem to persist. The ``id`` and ``content_hash`` must be
            unique; ``content_hash`` violations raise ``aiosqlite.IntegrityError``.
    """
    await conn.execute(
        """
        INSERT INTO media_items
            (id, media_type, content_hash, playlist_title, title, artist,
             duration_seconds, processing_status, created_at, updated_at,
             loudness_source_lufs, loudness_source_true_peak_dbtp,
             loudness_gain_db, loudness_target_lufs,
             loudness_target_true_peak_dbtp, loudness_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(item.id),
            item.media_type.value,
            item.content_hash,
            item.playlist_title,
            item.title,
            item.artist,
            item.duration_seconds,
            item.processing_status,
            item.created_at.isoformat(),
            item.updated_at.isoformat(),
            item.loudness_source_lufs,
            item.loudness_source_true_peak_dbtp,
            item.loudness_gain_db,
            item.loudness_target_lufs,
            item.loudness_target_true_peak_dbtp,
            item.loudness_mode,
        ),
    )


async def get_media_item(
    conn: aiosqlite.Connection, media_id: uuid.UUID
) -> MediaItem | None:
    """Fetch a single media item by primary key.

    Args:
        conn: Open, configured connection.
        media_id: UUID of the media item.

    Returns:
        The MediaItem, or None if not found.
    """
    async with conn.execute(
        "SELECT * FROM media_items WHERE id = ?", (str(media_id),)
    ) as cur:
        row = await cur.fetchone()
        return _media_item(row) if row else None


async def get_media_item_by_hash(
    conn: aiosqlite.Connection, content_hash: str
) -> MediaItem | None:
    """Fetch a media item by its content hash (for deduplication checks).

    Args:
        conn: Open, configured connection.
        content_hash: SHA-256 hex digest of the source file.

    Returns:
        The MediaItem, or None if not found.
    """
    async with conn.execute(
        "SELECT * FROM media_items WHERE content_hash = ?", (content_hash,)
    ) as cur:
        row = await cur.fetchone()
        return _media_item(row) if row else None


async def list_media_items(
    conn: aiosqlite.Connection,
    *,
    media_type: MediaType | None = None,
    profile_id: uuid.UUID | None = None,
    playlist_title: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[MediaItem]:
    """List media items with optional filtering.

    All filters are ANDed together. Results are ordered by
    ``playlist_title, title`` to match the two-level hierarchy.

    Args:
        conn: Open, configured connection.
        media_type: Restrict to one media type.
        profile_id: Only items assigned to this profile.
        playlist_title: Exact match on playlist_title.
        status: Exact match on processing_status.
        q: Substring search across title, artist, and playlist_title.
        limit: Maximum rows to return (default 1000).
        offset: Pagination offset (default 0).

    Returns:
        List of matching MediaItem instances.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if profile_id is not None:
        base = (
            "SELECT m.* FROM media_items m JOIN profile_media pm ON m.id = pm.media_id"
        )
        conditions.append("pm.profile_id = ?")
        params.append(str(profile_id))
    else:
        base = "SELECT m.* FROM media_items m"

    if media_type is not None:
        conditions.append("m.media_type = ?")
        params.append(media_type.value)

    if playlist_title is not None:
        conditions.append("m.playlist_title = ?")
        params.append(playlist_title)

    if status is not None:
        conditions.append("m.processing_status = ?")
        params.append(status)

    if q is not None:
        conditions.append(
            "(m.title LIKE ? OR m.artist LIKE ? OR m.playlist_title LIKE ?)"
        )
        like = f"%{q}%"
        params += [like, like, like]

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"{base}{where} ORDER BY m.playlist_title, m.title LIMIT ? OFFSET ?"
    params += [limit, offset]

    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
        return [_media_item(r) for r in rows]


async def update_media_item_status(
    conn: aiosqlite.Connection,
    media_id: uuid.UUID,
    status: str,
    updated_at: datetime,
) -> None:
    """Update the processing_status of a media item.

    Args:
        conn: Open, configured connection.
        media_id: UUID of the media item to update.
        status: New processing status string.
        updated_at: Timestamp to write to the updated_at column.
    """
    await conn.execute(
        "UPDATE media_items SET processing_status = ?, updated_at = ? WHERE id = ?",
        (status, updated_at.isoformat(), str(media_id)),
    )


async def update_media_item_metadata(
    conn: aiosqlite.Connection,
    media_id: uuid.UUID,
    *,
    title: str,
    artist: str | None,
    playlist_title: str,
    updated_at: datetime,
    media_type: str | None = None,
) -> None:
    """Update the user-visible metadata fields of a media item.

    Args:
        conn: Open, configured connection.
        media_id: UUID of the media item to update.
        title: New title.
        artist: New artist (may be None for photos).
        playlist_title: New playlist/group title.
        updated_at: Timestamp to write to the updated_at column.
        media_type: Optional new media type (music/audiobook/photo).
    """
    if media_type is not None:
        await conn.execute(
            """
            UPDATE media_items
            SET title = ?, artist = ?, playlist_title = ?, media_type = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                artist,
                playlist_title,
                media_type,
                updated_at.isoformat(),
                str(media_id),
            ),
        )
    else:
        await conn.execute(
            """
            UPDATE media_items
            SET title = ?, artist = ?, playlist_title = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, artist, playlist_title, updated_at.isoformat(), str(media_id)),
        )


async def update_media_item_loudness(
    conn: aiosqlite.Connection,
    media_id: uuid.UUID,
    *,
    source_lufs: float | None,
    source_true_peak_dbtp: float | None,
    gain_db: float | None,
    mode: str | None = None,
    target_lufs: float,
    target_true_peak_dbtp: float,
    updated_at: datetime,
) -> None:
    """Record the outcome of loudness normalization on a media item.

    Args:
        conn: Open, configured connection.
        media_id: UUID of the media item to update.
        source_lufs: Measured integrated loudness of the original, or
            ``None`` if it was too quiet to measure.
        source_true_peak_dbtp: Measured true peak of the original.
        gain_db: Loudness change applied, or ``None`` if the audio was kept
            unchanged.
        mode: How the gain was applied (``linear``, ``dynamic`` or
            ``capped``), or ``None`` if the audio was kept unchanged.
        target_lufs: Target the item was processed for.
        target_true_peak_dbtp: Ceiling the item was processed for.
        updated_at: Timestamp to write to the updated_at column.
    """
    await conn.execute(
        """
        UPDATE media_items
        SET loudness_source_lufs = ?, loudness_source_true_peak_dbtp = ?,
            loudness_gain_db = ?, loudness_mode = ?, loudness_target_lufs = ?,
            loudness_target_true_peak_dbtp = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            source_lufs,
            source_true_peak_dbtp,
            gain_db,
            mode,
            target_lufs,
            target_true_peak_dbtp,
            updated_at.isoformat(),
            str(media_id),
        ),
    )


async def list_distinct_artists(conn: aiosqlite.Connection) -> list[str]:
    """Return all distinct non-null artist values ordered alphabetically.

    Args:
        conn: Open, configured connection.

    Returns:
        Sorted list of artist strings.
    """
    async with conn.execute(
        "SELECT DISTINCT artist FROM media_items "
        "WHERE artist IS NOT NULL AND artist != '' "
        "ORDER BY artist"
    ) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def list_distinct_playlist_titles(conn: aiosqlite.Connection) -> list[str]:
    """Return all distinct playlist_title values ordered alphabetically.

    Args:
        conn: Open, configured connection.

    Returns:
        Sorted list of playlist title strings.
    """
    async with conn.execute(
        "SELECT DISTINCT playlist_title FROM media_items "
        "WHERE playlist_title IS NOT NULL AND playlist_title != '' "
        "ORDER BY playlist_title"
    ) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def delete_media_item(conn: aiosqlite.Connection, media_id: uuid.UUID) -> None:
    """Delete a media item and all dependent rows.

    Cascade-deletes ``profile_media`` and ``processed_files`` rows first,
    then removes the ``media_items`` row. The three deletes are wrapped in
    a single transaction and committed internally so the operation is
    always atomic.

    Args:
        conn: Open, configured connection.
        media_id: UUID of the media item to delete.
    """
    mid = str(media_id)
    await conn.execute("DELETE FROM profile_media WHERE media_id = ?", (mid,))
    await conn.execute("DELETE FROM processed_files WHERE media_id = ?", (mid,))
    await conn.execute("DELETE FROM media_items WHERE id = ?", (mid,))
    await conn.commit()


# ---------------------------------------------------------------------------
# processed_files
# ---------------------------------------------------------------------------


async def create_processed_file(conn: aiosqlite.Connection, pf: ProcessedFile) -> None:
    """Insert a processed-file row.

    Args:
        conn: Open, configured connection.
        pf: ProcessedFile to persist.
    """
    await conn.execute(
        """
        INSERT INTO processed_files
            (id, media_id, content_hash, file_type, relative_path,
             size_bytes, mime_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(pf.id),
            str(pf.media_id),
            pf.content_hash,
            pf.file_type,
            pf.relative_path,
            pf.size_bytes,
            pf.mime_type,
            pf.created_at.isoformat(),
        ),
    )


async def list_processed_files(
    conn: aiosqlite.Connection, media_id: uuid.UUID
) -> list[ProcessedFile]:
    """List all processed files for a media item.

    Args:
        conn: Open, configured connection.
        media_id: UUID of the parent media item.

    Returns:
        List of ProcessedFile instances, ordered by file_type.
    """
    async with conn.execute(
        "SELECT * FROM processed_files WHERE media_id = ? ORDER BY file_type",
        (str(media_id),),
    ) as cur:
        rows = await cur.fetchall()
        return [_processed_file(r) for r in rows]


async def update_processed_file(conn: aiosqlite.Connection, pf: ProcessedFile) -> None:
    """Point an existing processed-file row at different stored content.

    Rewrites every column except ``id`` and ``media_id``. Used to repoint a
    row at a newly written store file; the old file is left in the store.

    Args:
        conn: Open, configured connection.
        pf: The row's new values; ``pf.id`` selects the row.
    """
    await conn.execute(
        """
        UPDATE processed_files
        SET content_hash = ?, file_type = ?, relative_path = ?, size_bytes = ?,
            mime_type = ?, created_at = ?
        WHERE id = ?
        """,
        (
            pf.content_hash,
            pf.file_type,
            pf.relative_path,
            pf.size_bytes,
            pf.mime_type,
            pf.created_at.isoformat(),
            str(pf.id),
        ),
    )


async def get_processed_file_by_hash(
    conn: aiosqlite.Connection, content_hash: str
) -> ProcessedFile | None:
    """Fetch a processed file by its content hash.

    Args:
        conn: Open, configured connection.
        content_hash: SHA-256 hex digest of the processed file.

    Returns:
        The ProcessedFile, or None if not found.
    """
    async with conn.execute(
        "SELECT * FROM processed_files WHERE content_hash = ?", (content_hash,)
    ) as cur:
        row = await cur.fetchone()
        return _processed_file(row) if row else None


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


async def create_profile(conn: aiosqlite.Connection, profile: Profile) -> None:
    """Insert a profile row.

    Args:
        conn: Open, configured connection.
        profile: Profile to persist.
    """
    await conn.execute(
        "INSERT INTO profiles (id, name, created_at) VALUES (?, ?, ?)",
        (str(profile.id), profile.name, profile.created_at.isoformat()),
    )


async def get_profile(
    conn: aiosqlite.Connection, profile_id: uuid.UUID
) -> Profile | None:
    """Fetch a profile by primary key.

    Args:
        conn: Open, configured connection.
        profile_id: UUID of the profile.

    Returns:
        The Profile, or None if not found.
    """
    async with conn.execute(
        "SELECT * FROM profiles WHERE id = ?", (str(profile_id),)
    ) as cur:
        row = await cur.fetchone()
        return _profile(row) if row else None


async def list_profiles(conn: aiosqlite.Connection) -> list[Profile]:
    """List all profiles ordered by name.

    Args:
        conn: Open, configured connection.

    Returns:
        List of all Profile instances.
    """
    async with conn.execute("SELECT * FROM profiles ORDER BY name") as cur:
        rows = await cur.fetchall()
        return [_profile(r) for r in rows]


async def delete_profile(conn: aiosqlite.Connection, profile_id: uuid.UUID) -> None:
    """Delete a profile.

    Raises ``aiosqlite.IntegrityError`` (FK violation) if any devices
    are still linked to this profile. Unlink or delete those devices first.

    Args:
        conn: Open, configured connection.
        profile_id: UUID of the profile to delete.
    """
    await conn.execute("DELETE FROM profiles WHERE id = ?", (str(profile_id),))


# ---------------------------------------------------------------------------
# profile settings
# ---------------------------------------------------------------------------


async def get_profile_settings(
    conn: aiosqlite.Connection, profile_id: uuid.UUID
) -> ProfileSettings:
    """Fetch a profile's settings.

    Args:
        conn: Open, configured connection.
        profile_id: UUID of the profile.

    Returns:
        The stored settings, or defaults if none were ever saved. Fields
        stored by a newer release that this one does not know are dropped.
    """
    async with conn.execute(
        "SELECT settings FROM profile_settings WHERE profile_id = ?",
        (str(profile_id),),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return ProfileSettings()
    return ProfileSettings.model_validate_json(row["settings"])


async def set_profile_settings(
    conn: aiosqlite.Connection,
    profile_id: uuid.UUID,
    settings: ProfileSettings,
    updated_at: datetime,
) -> None:
    """Insert or replace a profile's settings.

    Raises ``aiosqlite.IntegrityError`` if the profile does not exist.

    Args:
        conn: Open, configured connection.
        profile_id: UUID of the profile.
        settings: Complete settings to store.
        updated_at: Timestamp of the change.
    """
    await conn.execute(
        "INSERT OR REPLACE INTO profile_settings (profile_id, settings, updated_at)"
        " VALUES (?, ?, ?)",
        (str(profile_id), settings.model_dump_json(), updated_at.isoformat()),
    )


# ---------------------------------------------------------------------------
# custom themes
# ---------------------------------------------------------------------------


def _theme_definition(row: sqlite3.Row, assets: list[sqlite3.Row]) -> ThemeDefinition:
    return ThemeDefinition(
        id=row["id"],
        name=row["name"],
        colors=ThemeColors.model_validate_json(row["colors"]),
        assets=[
            ThemeAsset(
                role=ThemeAssetRole(a["role"]),
                content_hash=a["content_hash"],
                relative_path=a["relative_path"],
                size_bytes=a["size_bytes"],
            )
            for a in assets
        ],
    )


async def _theme_asset_rows(
    conn: aiosqlite.Connection, theme_id: str
) -> list[sqlite3.Row]:
    async with conn.execute(
        "SELECT * FROM theme_assets WHERE theme_id = ? ORDER BY role", (theme_id,)
    ) as cur:
        return list(await cur.fetchall())


async def list_custom_themes(conn: aiosqlite.Connection) -> list[ThemeDefinition]:
    """List the custom themes, ordered by name.

    Args:
        conn: Open, configured connection.

    Returns:
        Every custom theme with its assets. Built-in themes are not stored.
    """
    async with conn.execute("SELECT * FROM themes ORDER BY name, id") as cur:
        rows = list(await cur.fetchall())
    return [_theme_definition(r, await _theme_asset_rows(conn, r["id"])) for r in rows]


async def get_custom_theme(
    conn: aiosqlite.Connection, theme_id: str
) -> ThemeDefinition | None:
    """Fetch one custom theme.

    Args:
        conn: Open, configured connection.
        theme_id: The theme's id.

    Returns:
        The theme with its assets, or None if there is no such custom theme.
    """
    async with conn.execute("SELECT * FROM themes WHERE id = ?", (theme_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return _theme_definition(row, await _theme_asset_rows(conn, theme_id))


async def upsert_custom_theme(
    conn: aiosqlite.Connection,
    theme_id: str,
    name: str,
    colors: ThemeColors,
    now: datetime,
) -> None:
    """Create a custom theme, or replace its name and palette (assets stay).

    Args:
        conn: Open, configured connection.
        theme_id: The theme's id.
        name: Display name.
        colors: The palette.
        now: Timestamp of the change.
    """
    await conn.execute(
        "INSERT INTO themes (id, name, colors, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET"
        " name = excluded.name, colors = excluded.colors,"
        " updated_at = excluded.updated_at",
        (theme_id, name, colors.model_dump_json(), now.isoformat(), now.isoformat()),
    )


async def delete_custom_theme(conn: aiosqlite.Connection, theme_id: str) -> bool:
    """Delete a custom theme and its asset rows (the store files stay).

    Args:
        conn: Open, configured connection.
        theme_id: The theme's id.

    Returns:
        True if a theme was deleted.
    """
    cur = await conn.execute("DELETE FROM themes WHERE id = ?", (theme_id,))
    return cur.rowcount > 0


async def set_theme_asset(
    conn: aiosqlite.Connection,
    theme_id: str,
    asset: ThemeAsset,
    mime_type: str,
) -> None:
    """Attach an asset to a theme, replacing any asset of the same role.

    Raises ``aiosqlite.IntegrityError`` if the theme does not exist.

    Args:
        conn: Open, configured connection.
        theme_id: The theme's id.
        asset: The stored file.
        mime_type: Its MIME type, sent when a device downloads it.
    """
    await conn.execute(
        "INSERT OR REPLACE INTO theme_assets"
        " (theme_id, role, content_hash, relative_path, mime_type, size_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            theme_id,
            asset.role.value,
            asset.content_hash,
            asset.relative_path,
            mime_type,
            asset.size_bytes,
        ),
    )


async def delete_theme_asset(
    conn: aiosqlite.Connection, theme_id: str, role: ThemeAssetRole
) -> bool:
    """Detach an asset from a theme (the store file stays).

    Args:
        conn: Open, configured connection.
        theme_id: The theme's id.
        role: Which asset.

    Returns:
        True if the theme had an asset of that role.
    """
    cur = await conn.execute(
        "DELETE FROM theme_assets WHERE theme_id = ? AND role = ?",
        (theme_id, role.value),
    )
    return cur.rowcount > 0


async def get_theme_asset_file(
    conn: aiosqlite.Connection, content_hash: str
) -> tuple[str, str] | None:
    """Find a theme asset by content hash, for the device download endpoint.

    Args:
        conn: Open, configured connection.
        content_hash: SHA-256 hex digest.

    Returns:
        ``(relative_path, mime_type)``, or None if no theme has that file.
    """
    async with conn.execute(
        "SELECT relative_path, mime_type FROM theme_assets"
        " WHERE content_hash = ? LIMIT 1",
        (content_hash,),
    ) as cur:
        row = await cur.fetchone()
    return (row["relative_path"], row["mime_type"]) if row else None


# ---------------------------------------------------------------------------
# server settings
# ---------------------------------------------------------------------------


async def get_or_create_server_id(conn: aiosqlite.Connection) -> str:
    """Return this server's stable identity, creating it on first use.

    Devices pin the id they paired with and refuse a server that answers with
    another (see ``kidsplay_server.pairing``). It lives in the database, so a
    backup restores it and a fresh database gets a new one. Commits only if it
    had to create the id.

    Args:
        conn: Open, configured connection.

    Returns:
        The id, a random UUID string.
    """
    async with conn.execute(
        "SELECT server_id FROM server_identity WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        return str(row["server_id"])
    await conn.execute(
        "INSERT OR IGNORE INTO server_identity (id, server_id, created_at) "
        "VALUES (1, ?, ?)",
        (str(uuid.uuid4()), datetime.now(UTC).isoformat()),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT server_id FROM server_identity WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    if row is None:  # cannot happen: we inserted it, or another writer did
        raise RuntimeError("could not create the server identity")
    return str(row["server_id"])


async def get_server_setting_values(conn: aiosqlite.Connection) -> dict[str, str]:
    """Return every server setting saved from the web UI, as raw strings.

    Validation and environment overrides live in
    ``kidsplay_server.server_settings``.

    Args:
        conn: Open, configured connection.

    Returns:
        Map of setting key to stored value.
    """
    async with conn.execute("SELECT key, value FROM server_settings") as cur:
        rows = await cur.fetchall()
    return {row["key"]: row["value"] for row in rows}


async def set_server_setting_value(
    conn: aiosqlite.Connection, key: str, value: str, updated_at: datetime
) -> None:
    """Insert or replace one stored server setting.

    Args:
        conn: Open, configured connection.
        key: Setting key.
        value: Raw string value.
        updated_at: Timestamp of the change.
    """
    await conn.execute(
        "INSERT OR REPLACE INTO server_settings (key, value, updated_at)"
        " VALUES (?, ?, ?)",
        (key, value, updated_at.isoformat()),
    )


async def delete_server_setting_value(conn: aiosqlite.Connection, key: str) -> None:
    """Remove a stored server setting, reverting it to its default.

    Args:
        conn: Open, configured connection.
        key: Setting key.
    """
    await conn.execute("DELETE FROM server_settings WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------


async def create_device(conn: aiosqlite.Connection, device: Device) -> None:
    """Insert a device row.

    Args:
        conn: Open, configured connection.
        device: Device to persist. ``api_key`` must be unique.
    """
    await conn.execute(
        """
        INSERT INTO devices
            (id, name, profile_id, display_width, display_height,
             last_sync_at, last_sync_manifest_hash, api_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(device.id),
            device.name,
            str(device.profile_id),
            device.display_width,
            device.display_height,
            device.last_sync_at.isoformat() if device.last_sync_at else None,
            device.last_sync_manifest_hash,
            device.api_key,
            device.created_at.isoformat(),
        ),
    )


async def get_device(conn: aiosqlite.Connection, device_id: uuid.UUID) -> Device | None:
    """Fetch a device by primary key.

    Args:
        conn: Open, configured connection.
        device_id: UUID of the device.

    Returns:
        The Device, or None if not found.
    """
    async with conn.execute(
        "SELECT * FROM devices WHERE id = ?", (str(device_id),)
    ) as cur:
        row = await cur.fetchone()
        return _device(row) if row else None


async def get_device_by_api_key(
    conn: aiosqlite.Connection, api_key: str
) -> Device | None:
    """Fetch a device by its API key (used for request authentication).

    The ``api_key`` column has a UNIQUE index, so this query is fast.

    Args:
        conn: Open, configured connection.
        api_key: Bearer token from the Authorization header.

    Returns:
        The Device, or None if the key is not recognised.
    """
    async with conn.execute(
        "SELECT * FROM devices WHERE api_key = ?", (api_key,)
    ) as cur:
        row = await cur.fetchone()
        return _device(row) if row else None


async def list_devices(conn: aiosqlite.Connection) -> list[Device]:
    """List all devices ordered by name.

    Args:
        conn: Open, configured connection.

    Returns:
        List of all Device instances.
    """
    async with conn.execute("SELECT * FROM devices ORDER BY name") as cur:
        rows = await cur.fetchall()
        return [_device(r) for r in rows]


async def update_device(
    conn: aiosqlite.Connection,
    device_id: uuid.UUID,
    *,
    name: str,
    profile_id: uuid.UUID,
    display_width: int,
    display_height: int,
) -> None:
    """Update the mutable identity fields of a device.

    Use ``update_device_sync`` to record sync state separately.

    Args:
        conn: Open, configured connection.
        device_id: UUID of the device to update.
        name: New display name.
        profile_id: New linked profile UUID.
        display_width: Screen width in pixels.
        display_height: Screen height in pixels.
    """
    await conn.execute(
        """
        UPDATE devices
        SET name = ?, profile_id = ?, display_width = ?, display_height = ?
        WHERE id = ?
        """,
        (name, str(profile_id), display_width, display_height, str(device_id)),
    )


async def update_device_sync(
    conn: aiosqlite.Connection,
    device_id: uuid.UUID,
    last_sync_at: datetime,
    last_sync_manifest_hash: str,
) -> None:
    """Record the result of a successful sync for a device.

    Args:
        conn: Open, configured connection.
        device_id: UUID of the device.
        last_sync_at: When the sync completed.
        last_sync_manifest_hash: Hash of the manifest that was applied.
    """
    await conn.execute(
        """
        UPDATE devices
        SET last_sync_at = ?, last_sync_manifest_hash = ?
        WHERE id = ?
        """,
        (last_sync_at.isoformat(), last_sync_manifest_hash, str(device_id)),
    )


async def delete_device(conn: aiosqlite.Connection, device_id: uuid.UUID) -> None:
    """Delete a device.

    Args:
        conn: Open, configured connection.
        device_id: UUID of the device to delete.
    """
    await conn.execute("DELETE FROM devices WHERE id = ?", (str(device_id),))


# ---------------------------------------------------------------------------
# profile_media
# ---------------------------------------------------------------------------


async def assign_media_to_profile(
    conn: aiosqlite.Connection,
    profile_id: uuid.UUID,
    media_id: uuid.UUID,
    assigned_at: datetime,
) -> ProfileMediaAssignment:
    """Assign a media item to a profile.

    Idempotent: if the assignment already exists the INSERT is silently
    ignored and the original assignment is returned.

    Args:
        conn: Open, configured connection.
        profile_id: UUID of the profile.
        media_id: UUID of the media item.
        assigned_at: Timestamp to record for the assignment.

    Returns:
        A ProfileMediaAssignment representing the (possibly pre-existing)
        assignment.
    """
    await conn.execute(
        """
        INSERT OR IGNORE INTO profile_media (profile_id, media_id, assigned_at)
        VALUES (?, ?, ?)
        """,
        (str(profile_id), str(media_id), assigned_at.isoformat()),
    )
    return ProfileMediaAssignment(
        profile_id=profile_id,
        media_id=media_id,
        assigned_at=assigned_at,
    )


async def unassign_media_from_profile(
    conn: aiosqlite.Connection,
    profile_id: uuid.UUID,
    media_id: uuid.UUID,
) -> None:
    """Remove a media-to-profile assignment.

    No-op if the assignment does not exist.

    Args:
        conn: Open, configured connection.
        profile_id: UUID of the profile.
        media_id: UUID of the media item.
    """
    await conn.execute(
        "DELETE FROM profile_media WHERE profile_id = ? AND media_id = ?",
        (str(profile_id), str(media_id)),
    )


async def list_media_for_profile(
    conn: aiosqlite.Connection, profile_id: uuid.UUID
) -> list[MediaItem]:
    """List all media items assigned to a profile.

    Args:
        conn: Open, configured connection.
        profile_id: UUID of the profile.

    Returns:
        Media items ordered by playlist_title, title.
    """
    async with conn.execute(
        """
        SELECT m.*
        FROM media_items m
        JOIN profile_media pm ON m.id = pm.media_id
        WHERE pm.profile_id = ?
        ORDER BY m.playlist_title, m.title
        """,
        (str(profile_id),),
    ) as cur:
        rows = await cur.fetchall()
        return [_media_item(r) for r in rows]


async def list_profiles_for_media(
    conn: aiosqlite.Connection, media_id: uuid.UUID
) -> list[Profile]:
    """List all profiles a media item is assigned to.

    Args:
        conn: Open, configured connection.
        media_id: UUID of the media item.

    Returns:
        Profiles ordered by name, then id (names are not unique).
    """
    async with conn.execute(
        """
        SELECT p.*
        FROM profiles p
        JOIN profile_media pm ON p.id = pm.profile_id
        WHERE pm.media_id = ?
        ORDER BY p.name, p.id
        """,
        (str(media_id),),
    ) as cur:
        rows = await cur.fetchall()
        return [_profile(r) for r in rows]


# ---------------------------------------------------------------------------
# import_queue
# ---------------------------------------------------------------------------


def _queue_item(row: sqlite3.Row) -> QueueItem:
    import json as _json

    return QueueItem(
        id=uuid.UUID(row["id"]),
        url=row["url"],
        importer=row["importer"],
        media_type=MediaType(row["media_type"]),
        playlist_title=row["playlist_title"],
        profile_ids=[uuid.UUID(p) for p in _json.loads(row["profile_ids"])],
        title_override=row["title_override"],
        artist_override=row["artist_override"],
        status=QueueStatus(row["status"]),
        attempt=row["attempt"],
        max_retries=row["max_retries"],
        last_error=row["last_error"],
        log=row["log"],
        media_id=uuid.UUID(row["media_id"]) if row["media_id"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        ),
    )


async def create_queue_item(conn: aiosqlite.Connection, item: QueueItem) -> None:
    """Insert an import queue item.

    Args:
        conn: Open, configured connection.
        item: QueueItem to persist.
    """
    import json as _json

    await conn.execute(
        """
        INSERT INTO import_queue
            (id, url, importer, media_type, playlist_title, profile_ids,
             title_override, artist_override, status, attempt, max_retries,
             last_error, log, media_id, created_at, updated_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(item.id),
            item.url,
            item.importer,
            item.media_type.value,
            item.playlist_title,
            _json.dumps([str(p) for p in item.profile_ids]),
            item.title_override,
            item.artist_override,
            item.status.value,
            item.attempt,
            item.max_retries,
            item.last_error,
            item.log,
            str(item.media_id) if item.media_id else None,
            item.created_at.isoformat(),
            item.updated_at.isoformat(),
            item.completed_at.isoformat() if item.completed_at else None,
        ),
    )


async def get_queue_item(
    conn: aiosqlite.Connection, item_id: uuid.UUID
) -> QueueItem | None:
    """Fetch a single import queue item by ID.

    Args:
        conn: Open, configured connection.
        item_id: UUID of the queue item.

    Returns:
        The QueueItem, or None if not found.
    """
    async with conn.execute(
        "SELECT * FROM import_queue WHERE id = ?", (str(item_id),)
    ) as cur:
        row = await cur.fetchone()
        return _queue_item(row) if row else None


async def list_queue_items(
    conn: aiosqlite.Connection,
    *,
    status: QueueStatus | None = None,
    limit: int = 100,
    offset: int = 0,
    include_loudness: bool = False,
) -> list[QueueItem]:
    """List import queue items with optional status filter.

    Args:
        conn: Open, configured connection.
        status: Optional filter by queue status.
        limit: Maximum rows to return.
        offset: Pagination offset.
        include_loudness: Also list loudness-normalization jobs, which share
            the queue but are progress of the audio processing, not imports.

    Returns:
        List of QueueItem instances ordered by created_at descending.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if status is not None:
        conditions.append("status = ?")
        params.append(status.value)
    if not include_loudness:
        conditions.append("(importer IS NULL OR importer != ?)")
        params.append(LOUDNESS_JOB)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM import_queue{where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
        return [_queue_item(r) for r in rows]


async def claim_next_queue_item(
    conn: aiosqlite.Connection,
) -> QueueItem | None:
    """Atomically claim the next pending queue item for processing.

    Imports go before loudness normalization, so a link a parent just added
    does not wait behind a library-wide backfill. Otherwise oldest first.
    Sets status to RUNNING and increments the attempt counter.

    Args:
        conn: Open, configured connection.

    Returns:
        The claimed QueueItem, or None if queue is empty.
    """
    now = datetime.now().isoformat()
    async with conn.execute(
        """
        SELECT * FROM import_queue
        WHERE status = 'pending'
        ORDER BY (importer = ?) ASC, created_at ASC, rowid ASC
        LIMIT 1
        """,
        (LOUDNESS_JOB,),
    ) as cur:
        row = await cur.fetchone()
        if not row:
            return None
        item = _queue_item(row)

    item.status = QueueStatus.RUNNING
    item.attempt += 1
    item.updated_at = datetime.now()

    await conn.execute(
        """
        UPDATE import_queue
        SET status = ?, attempt = ?, updated_at = ?
        WHERE id = ?
        """,
        (item.status.value, item.attempt, now, str(item.id)),
    )
    await conn.commit()
    return item


async def update_queue_item(
    conn: aiosqlite.Connection,
    item_id: uuid.UUID,
    *,
    status: QueueStatus | None = None,
    attempt: int | None = None,
    last_error: str | None = None,
    log: str | None = None,
    media_id: uuid.UUID | None = None,
    completed_at: datetime | None = None,
) -> None:
    """Update fields on an import queue item.

    Only provided (non-None) fields are updated. Always updates
    ``updated_at``.

    Args:
        conn: Open, configured connection.
        item_id: UUID of the queue item to update.
        status: New status.
        attempt: New attempt count.
        last_error: Error message from latest attempt.
        log: Full accumulated log text.
        media_id: Media item ID on success.
        completed_at: Completion timestamp.
    """
    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [datetime.now().isoformat()]

    if status is not None:
        sets.append("status = ?")
        params.append(status.value)
    if attempt is not None:
        sets.append("attempt = ?")
        params.append(attempt)
    if last_error is not None:
        sets.append("last_error = ?")
        params.append(last_error)
    if log is not None:
        sets.append("log = ?")
        params.append(log)
    if media_id is not None:
        sets.append("media_id = ?")
        params.append(str(media_id))
    if completed_at is not None:
        sets.append("completed_at = ?")
        params.append(completed_at.isoformat())

    sql = f"UPDATE import_queue SET {', '.join(sets)} WHERE id = ?"
    params.append(str(item_id))
    await conn.execute(sql, params)


async def delete_queue_item(conn: aiosqlite.Connection, item_id: uuid.UUID) -> None:
    """Delete an import queue item.

    Args:
        conn: Open, configured connection.
        item_id: UUID of the queue item to delete.
    """
    await conn.execute("DELETE FROM import_queue WHERE id = ?", (str(item_id),))


# ---------------------------------------------------------------------------
# Loudness-normalization jobs (rows of import_queue, importer = LOUDNESS_JOB)
# ---------------------------------------------------------------------------

_ACTIVE = (QueueStatus.PENDING.value, QueueStatus.RUNNING.value)


async def create_loudness_job(
    conn: aiosqlite.Connection,
    media_id: uuid.UUID,
    media_type: MediaType,
    *,
    max_retries: int = 2,
    created_at: datetime | None = None,
) -> QueueItem | None:
    """Queue loudness normalization of one media item.

    Not committed: the caller commits, so ingest queues the job in the same
    transaction that creates the item.

    Args:
        conn: Open, configured connection.
        media_id: The item to normalize. It need not exist (yet); the worker
            skips an item that is gone.
        media_type: The item's type, kept on the row for reference.
        max_retries: Attempts before the job is marked failed.
        created_at: Creation time. A library-wide request gives all its jobs
            the same time, which is how their progress is grouped.

    Returns:
        The queued job, or ``None`` if the item already has a pending or
        running job.
    """
    async with conn.execute(
        "SELECT 1 FROM import_queue WHERE importer = ? AND media_id = ? "
        "AND status IN (?, ?)",
        (LOUDNESS_JOB, str(media_id), *_ACTIVE),
    ) as cur:
        if await cur.fetchone() is not None:
            return None
    now = created_at or datetime.now()
    item = QueueItem(
        url=f"loudness:{media_id}",
        importer=LOUDNESS_JOB,
        media_type=media_type,
        playlist_title="",
        media_id=media_id,
        max_retries=max_retries,
        created_at=now,
        updated_at=now,
    )
    await create_queue_item(conn, item)
    return item


async def list_loudness_jobs(
    conn: aiosqlite.Connection, since: datetime
) -> list[QueueItem]:
    """List the loudness jobs created at or after ``since``, oldest first.

    Args:
        conn: Open, configured connection.
        since: Start of the window.

    Returns:
        The jobs, whatever their status.
    """
    async with conn.execute(
        "SELECT * FROM import_queue WHERE importer = ? AND created_at >= ? "
        "ORDER BY created_at, rowid",
        (LOUDNESS_JOB, since.isoformat()),
    ) as cur:
        return [_queue_item(r) for r in await cur.fetchall()]


async def list_normalizing_media_ids(conn: aiosqlite.Connection) -> set[uuid.UUID]:
    """Return the media items with a pending or running loudness job.

    Args:
        conn: Open, configured connection.

    Returns:
        Media IDs, for the "normalizing…" state in the web UI.
    """
    async with conn.execute(
        "SELECT media_id FROM import_queue WHERE importer = ? AND status IN (?, ?) "
        "AND media_id IS NOT NULL",
        (LOUDNESS_JOB, *_ACTIVE),
    ) as cur:
        return {uuid.UUID(r[0]) for r in await cur.fetchall()}


async def prune_loudness_jobs(conn: aiosqlite.Connection, before: datetime) -> int:
    """Delete finished loudness jobs that completed before ``before``.

    Every audio ingest leaves a finished job behind; this keeps the table
    from growing without bound. Not committed.

    Args:
        conn: Open, configured connection.
        before: Cut-off completion time.

    Returns:
        How many jobs were deleted.
    """
    cur = await conn.execute(
        "DELETE FROM import_queue WHERE importer = ? "
        "AND status NOT IN (?, ?) AND COALESCE(completed_at, updated_at) < ?",
        (LOUDNESS_JOB, *_ACTIVE, before.isoformat()),
    )
    return cur.rowcount
