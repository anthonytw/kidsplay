"""Tests for the versioned schema migrations in kidsplay_server.database.

The upgrade tests build a database with the exact schema that shipped before
versioning (``_V0_SCHEMA_SQL`` is a frozen copy, not an import, so later schema
edits cannot silently change what "an old database" means) and check that
``init_db`` upgrades it in place without losing rows.
"""

import asyncio
import sqlite3
import uuid
from pathlib import Path

import aiosqlite
import pytest

from kidsplay_models.queue import QueueStatus
from kidsplay_server.database import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    configure_conn,
    get_media_item,
    get_queue_item,
    get_schema_version,
    init_db,
    list_queue_items,
)

# Schema as of the last release without ``PRAGMA user_version`` (version 0).
# Frozen on purpose: do not update this when the schema changes.
_V0_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
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
    updated_at         TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS yt_queue (
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
    completed_at    TEXT
);
"""


_OLD_ROW_ID = "11111111-2222-3333-4444-555555555555"


async def _make_v0_db(path: Path) -> None:
    """Create a version-0 database holding one queue row and one profile."""
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_V0_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO profiles (id, name, created_at) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), "Leo", "2026-01-01T00:00:00"),
        )
        await conn.execute(
            """
            INSERT INTO yt_queue
                (id, url, media_type, playlist_title, profile_ids, status,
                 attempt, max_retries, last_error, log, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _OLD_ROW_ID,
                "https://www.youtube.com/watch?v=abc123",
                "music",
                "Favourites",
                "[]",
                "failed",
                5,
                5,
                "HTTP Error 403",
                "ATTEMPT 1 ...",
                "2026-01-01T00:00:00",
                "2026-01-01T00:05:00",
            ),
        )
        await conn.commit()


async def _table_names(conn: aiosqlite.Connection) -> set[str]:
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
        return {row[0] for row in await cur.fetchall()}


async def test_new_database_is_stamped_with_latest_version(tmp_path: Path) -> None:
    async with aiosqlite.connect(tmp_path / "new.db") as conn:
        await configure_conn(conn)
        await init_db(conn)
        assert await get_schema_version(conn) == SCHEMA_VERSION
        tables = await _table_names(conn)
    assert "import_queue" in tables
    assert "yt_queue" not in tables


async def test_new_database_schema_and_version_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while creating a new database leaves no half-made schema."""
    from kidsplay_server import database

    good_sql = database._CREATE_TABLES_SQL
    monkeypatch.setattr(
        database, "_CREATE_TABLES_SQL", good_sql + "\nSELECT * FROM no_such_table;"
    )
    async with aiosqlite.connect(tmp_path / "new.db") as conn:
        await configure_conn(conn)
        with pytest.raises(sqlite3.OperationalError):
            await init_db(conn)
        assert await _table_names(conn) == set()
        assert await get_schema_version(conn) == 0

        # The next start creates the database normally.
        monkeypatch.setattr(database, "_CREATE_TABLES_SQL", good_sql)
        await init_db(conn)
        assert await get_schema_version(conn) == SCHEMA_VERSION
        assert "import_queue" in await _table_names(conn)


async def test_upgrade_from_v0_keeps_queue_rows(tmp_path: Path) -> None:
    """A database created by the pre-versioning schema upgrades in place."""
    path = tmp_path / "old.db"
    await _make_v0_db(path)

    async with aiosqlite.connect(path) as conn:
        await configure_conn(conn)
        assert await get_schema_version(conn) == 0
        await init_db(conn)
        await conn.commit()

        assert await get_schema_version(conn) == SCHEMA_VERSION
        tables = await _table_names(conn)
        assert "yt_queue" not in tables
        assert "import_queue" in tables

        item = await get_queue_item(conn, uuid.UUID(_OLD_ROW_ID))
        assert item is not None
        assert item.url == "https://www.youtube.com/watch?v=abc123"
        assert item.status == QueueStatus.FAILED
        assert item.attempt == 5
        assert item.last_error == "HTTP Error 403"
        assert item.log == "ATTEMPT 1 ..."
        # Old rows predate the column: the worker picks an importer by URL.
        assert item.importer is None

        async with conn.execute("SELECT COUNT(*) FROM profiles") as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 1


async def test_init_db_is_idempotent_after_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    await _make_v0_db(path)

    for _ in range(2):
        async with aiosqlite.connect(path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            await conn.commit()

    async with aiosqlite.connect(path) as conn:
        await configure_conn(conn)
        assert await get_schema_version(conn) == SCHEMA_VERSION
        # A second run must not recreate an empty legacy table.
        assert "yt_queue" not in await _table_names(conn)
        assert len(await list_queue_items(conn)) == 1


async def test_newer_schema_version_is_refused(tmp_path: Path) -> None:
    async with aiosqlite.connect(tmp_path / "future.db") as conn:
        await configure_conn(conn)
        await init_db(conn)
        await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with pytest.raises(RuntimeError, match="newer than this server"):
            await init_db(conn)


async def test_loudness_columns_added_on_upgrade(tmp_path: Path) -> None:
    """Step 2 adds the loudness columns; existing items read as not normalized."""
    path = tmp_path / "v1.db"
    await _make_v0_db(path)
    media_id = uuid.uuid4()
    with sqlite3.connect(path) as conn:
        # Bring the database to version 1, as the previous release left it.
        conn.executescript(_MIGRATIONS[0])
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO media_items (id, media_type, content_hash, "
            "playlist_title, title, processing_status, created_at, updated_at) "
            "VALUES (?, 'music', 'abc', 'P', 'Song', 'ready', ?, ?)",
            (str(media_id), "2025-01-01T00:00:00", "2025-01-01T00:00:00"),
        )

    async with aiosqlite.connect(path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        assert await get_schema_version(conn) == SCHEMA_VERSION == len(_MIGRATIONS)
        item = await get_media_item(conn, media_id)

    assert item is not None
    assert item.title == "Song"
    assert item.loudness_source_lufs is None
    assert item.loudness_source_true_peak_dbtp is None
    assert item.loudness_gain_db is None
    assert item.loudness_target_lufs is None
    assert item.loudness_target_true_peak_dbtp is None


async def test_theme_tables_appear_on_upgrade_without_a_migration_step(
    tmp_path: Path,
) -> None:
    """Themes are new tables: ``CREATE TABLE IF NOT EXISTS`` covers old databases."""
    path = tmp_path / "before-themes.db"
    async with aiosqlite.connect(path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await conn.execute("DROP TABLE theme_assets")
        await conn.execute("DROP TABLE themes")
        await conn.commit()
        assert "themes" not in await _table_names(conn)

    async with aiosqlite.connect(path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        assert {"themes", "theme_assets"} <= await _table_names(conn)
        assert await get_schema_version(conn) == SCHEMA_VERSION == len(_MIGRATIONS)


async def _init_concurrently(path: Path, n: int) -> None:
    """Open ``n`` connections to ``path`` and run ``init_db`` on all at once.

    Every request runs ``init_db`` (see ``api.deps.get_db``), so the first
    requests to a new or just-upgraded server race on it.
    """
    conns = [await aiosqlite.connect(path, timeout=30) for _ in range(n)]
    try:
        for conn in conns:
            await configure_conn(conn)
        await asyncio.gather(*(init_db(conn) for conn in conns))
    finally:
        for conn in conns:
            await conn.close()


@pytest.mark.parametrize("attempt", range(5))
async def test_concurrent_init_of_a_new_database(tmp_path: Path, attempt: int) -> None:
    """Racing connections must not mistake a just-created schema for version 0."""
    path = tmp_path / f"new-{attempt}.db"
    await _init_concurrently(path, 16)
    async with aiosqlite.connect(path) as conn:
        assert await get_schema_version(conn) == SCHEMA_VERSION
        assert "import_queue" in await _table_names(conn)


@pytest.mark.parametrize("attempt", range(5))
async def test_concurrent_upgrade_applies_each_step_once(
    tmp_path: Path, attempt: int
) -> None:
    """Racing connections upgrade an old database exactly once, keeping rows."""
    path = tmp_path / f"old-{attempt}.db"
    await _make_v0_db(path)
    await _init_concurrently(path, 16)
    async with aiosqlite.connect(path) as conn:
        await configure_conn(conn)
        assert await get_schema_version(conn) == SCHEMA_VERSION
        assert await get_queue_item(conn, uuid.UUID(_OLD_ROW_ID)) is not None


async def _table_info(path: Path) -> dict[str, set[tuple[object, ...]]]:
    """Return every table's columns as (name, type, notnull, default, pk).

    The column position is left out on purpose: ``ALTER TABLE ADD COLUMN``
    appends, so a migrated table may list its columns in another order than a
    freshly created one without differing in any way that matters.
    """
    async with aiosqlite.connect(path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ) as cur:
            names = [row[0] for row in await cur.fetchall()]
        info: dict[str, set[tuple[object, ...]]] = {}
        for name in names:
            # Table names come from sqlite_master, not from user input.
            async with conn.execute(f'PRAGMA table_info("{name}")') as cur:
                info[name] = {tuple(r[1:]) for r in await cur.fetchall()}
    return info


async def test_migrated_schema_equals_fresh_schema(tmp_path: Path) -> None:
    """A database upgraded step by step ends up with the schema of a new one.

    Catches a column added to ``_CREATE_TABLES_SQL`` without a migration step
    (or the reverse), which would leave upgraded servers broken.
    """
    fresh = tmp_path / "fresh.db"
    async with aiosqlite.connect(fresh) as conn:
        await configure_conn(conn)
        await init_db(conn)

    migrated = tmp_path / "migrated.db"
    await _make_v0_db(migrated)
    async with aiosqlite.connect(migrated) as conn:
        await configure_conn(conn)
        await init_db(conn)

    fresh_info = await _table_info(fresh)
    migrated_info = await _table_info(migrated)
    assert set(migrated_info) == set(fresh_info)
    for table, columns in fresh_info.items():
        assert migrated_info[table] == columns, f"table {table} differs"


async def test_loudness_mode_column_added_on_upgrade(tmp_path: Path) -> None:
    """Step 3 adds ``loudness_mode``; existing items read as "mode unknown"."""
    path = tmp_path / "v2.db"
    await _make_v0_db(path)
    media_id = uuid.uuid4()
    with sqlite3.connect(path) as conn:
        # Bring the database to version 2, as the previous release left it.
        conn.executescript(_MIGRATIONS[0])
        conn.executescript(_MIGRATIONS[1])
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            "INSERT INTO media_items (id, media_type, content_hash, "
            "playlist_title, title, processing_status, created_at, updated_at, "
            "loudness_target_lufs, loudness_gain_db) "
            "VALUES (?, 'music', 'abc', 'P', 'Song', 'ready', ?, ?, -16.0, 11.9)",
            (str(media_id), "2025-01-01T00:00:00", "2025-01-01T00:00:00"),
        )

    async with aiosqlite.connect(path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        assert await get_schema_version(conn) == SCHEMA_VERSION == len(_MIGRATIONS)
        item = await get_media_item(conn, media_id)

    assert item is not None
    assert item.loudness_gain_db == 11.9
    assert item.loudness_mode is None
