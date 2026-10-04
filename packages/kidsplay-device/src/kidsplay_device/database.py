"""Device-local SQLite database operations.

All access is synchronous (sqlite3, not aiosqlite) because the device
player is single-threaded for database work. The sync client opens a
connection, performs its batch, commits, and closes.

Schema (matches ARCHITECTURE.md device schema):
  - media_items  — local copy of assigned media metadata
  - play_history — local play tracking
  - sync_state   — key/value pairs (last_manifest_hash, last_sync_at,
                   profile_settings, theme, sync_interval_seconds, last_server_time)
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kidsplay_models import ProfileSettings, ThemeDefinition
from kidsplay_models.sync import SyncMediaEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Row type
# ---------------------------------------------------------------------------


@dataclass
class MediaRow:
    """A single row from the device media_items table.

    Used as the return type for all query functions so callers get typed
    access to every field without relying on sqlite3.Row magic.
    """

    media_id: str
    media_type: str
    playlist_title: str
    title: str
    artist: str | None
    duration_seconds: int | None
    audio_path: str | None
    photo_path: str | None
    thumbnail_small_path: str | None
    thumbnail_medium_path: str | None
    thumbnail_large_path: str | None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_items (
    media_id              TEXT PRIMARY KEY,
    media_type            TEXT NOT NULL,
    playlist_title        TEXT NOT NULL,
    title                 TEXT NOT NULL,
    artist                TEXT,
    duration_seconds      INTEGER,
    audio_path            TEXT,
    photo_path            TEXT,
    thumbnail_small_path  TEXT,
    thumbnail_medium_path TEXT,
    thumbnail_large_path  TEXT
);

CREATE TABLE IF NOT EXISTS play_history (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id                TEXT NOT NULL,
    played_at               TEXT NOT NULL,
    duration_played_seconds INTEGER
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the device SQLite database and apply the schema.

    Sets WAL journal mode and creates all tables if they don't exist.
    The caller is responsible for closing the returned connection.

    Args:
        db_path: Filesystem path to the SQLite file.  Parent directory
            must exist.

    Returns:
        Open ``sqlite3.Connection`` with the schema applied.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Media items
# ---------------------------------------------------------------------------


def upsert_media_item(conn: sqlite3.Connection, entry: SyncMediaEntry) -> None:
    """Insert or replace a media item from a sync manifest entry.

    Maps ``SyncMediaEntry.thumbnail_paths`` dict keys ("60x60", "200x200",
    "480x480") to the corresponding ``thumbnail_*_path`` columns.

    Args:
        conn: Open database connection.
        entry: Media metadata from the server sync manifest.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO media_items (
            media_id, media_type, playlist_title, title, artist,
            duration_seconds, audio_path, photo_path,
            thumbnail_small_path, thumbnail_medium_path, thumbnail_large_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(entry.media_id),
            entry.media_type.value,
            entry.playlist_title,
            entry.title,
            entry.artist,
            entry.duration_seconds,
            entry.audio_path,
            entry.photo_path,
            entry.thumbnail_paths.get("60x60"),
            entry.thumbnail_paths.get("200x200"),
            entry.thumbnail_paths.get("480x480"),
        ),
    )


def delete_media_item(conn: sqlite3.Connection, media_id: str) -> None:
    """Delete a media item from the local database.

    Does not touch the filesystem; callers are responsible for deleting
    the associated files before calling this.

    Args:
        conn: Open database connection.
        media_id: String UUID of the media item to remove.
    """
    conn.execute("DELETE FROM media_items WHERE media_id = ?", (media_id,))


def get_all_media_ids(conn: sqlite3.Connection) -> set[str]:
    """Return the set of all media_id values currently in the local DB.

    Used during sync to diff against the server manifest and find items
    that should be removed.

    Args:
        conn: Open database connection.

    Returns:
        Set of media_id strings (UUIDs).
    """
    cur = conn.execute("SELECT media_id FROM media_items")
    return {row[0] for row in cur.fetchall()}


def get_media_file_paths(conn: sqlite3.Connection, media_id: str) -> list[str]:
    """Return all non-None file paths stored for a media item.

    Used by the sync client to find local files that need to be deleted
    when a media item is removed from the manifest.

    Args:
        conn: Open database connection.
        media_id: String UUID of the media item.

    Returns:
        List of relative path strings (may be empty).
    """
    cur = conn.execute(
        """
        SELECT audio_path, photo_path,
               thumbnail_small_path, thumbnail_medium_path, thumbnail_large_path
        FROM media_items WHERE media_id = ?
        """,
        (media_id,),
    )
    row = cur.fetchone()
    if row is None:
        return []
    return [p for p in row if p is not None]


# ---------------------------------------------------------------------------
# Player UI queries
# ---------------------------------------------------------------------------


def _row_to_media(row: tuple) -> MediaRow:
    return MediaRow(
        media_id=row[0],
        media_type=row[1],
        playlist_title=row[2],
        title=row[3],
        artist=row[4],
        duration_seconds=row[5],
        audio_path=row[6],
        photo_path=row[7],
        thumbnail_small_path=row[8],
        thumbnail_medium_path=row[9],
        thumbnail_large_path=row[10],
    )


def get_groups_with_thumbnails(
    conn: sqlite3.Connection, media_type: str
) -> list[tuple[str, str | None]]:
    """Return all distinct playlist groups with a representative thumbnail.

    The thumbnail is taken from the first item in the group (alphabetically
    by title).  Used for the group-level list view in Music, Audiobooks, and
    Photos.

    Args:
        conn: Open database connection.
        media_type: One of ``'music'``, ``'audiobook'``, ``'photo'``.

    Returns:
        List of ``(playlist_title, thumbnail_path_or_None)`` tuples ordered
        by ``playlist_title``.
    """
    cur = conn.execute(
        """
        SELECT DISTINCT m1.playlist_title,
               (SELECT COALESCE(thumbnail_medium_path, thumbnail_small_path)
                FROM media_items
                WHERE media_type = ? AND playlist_title = m1.playlist_title
                ORDER BY title
                LIMIT 1) AS thumb
        FROM media_items m1
        WHERE m1.media_type = ?
        ORDER BY m1.playlist_title
        """,
        (media_type, media_type),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def get_tracks_by_group(
    conn: sqlite3.Connection, playlist_title: str
) -> list[MediaRow]:
    """Return all music tracks in a given playlist, ordered by title.

    Args:
        conn: Open database connection.
        playlist_title: The playlist/group name to filter on.

    Returns:
        List of ``MediaRow`` objects for media_type='music'.
    """
    cur = conn.execute(
        """
        SELECT media_id, media_type, playlist_title, title, artist,
               duration_seconds, audio_path, photo_path,
               thumbnail_small_path, thumbnail_medium_path, thumbnail_large_path
        FROM media_items
        WHERE media_type = 'music' AND playlist_title = ?
        ORDER BY title
        """,
        (playlist_title,),
    )
    return [_row_to_media(row) for row in cur.fetchall()]


def get_audiobooks(conn: sqlite3.Connection) -> list[MediaRow]:
    """Return all audiobook items ordered by playlist_title then title.

    Args:
        conn: Open database connection.

    Returns:
        List of ``MediaRow`` objects for media_type='audiobook'.
    """
    cur = conn.execute(
        """
        SELECT media_id, media_type, playlist_title, title, artist,
               duration_seconds, audio_path, photo_path,
               thumbnail_small_path, thumbnail_medium_path, thumbnail_large_path
        FROM media_items
        WHERE media_type = 'audiobook'
        ORDER BY playlist_title, title
        """,
    )
    return [_row_to_media(row) for row in cur.fetchall()]


def get_photos(conn: sqlite3.Connection) -> list[MediaRow]:
    """Return all photo items ordered by playlist_title then title.

    Args:
        conn: Open database connection.

    Returns:
        List of ``MediaRow`` objects for media_type='photo'.
    """
    cur = conn.execute(
        """
        SELECT media_id, media_type, playlist_title, title, artist,
               duration_seconds, audio_path, photo_path,
               thumbnail_small_path, thumbnail_medium_path, thumbnail_large_path
        FROM media_items
        WHERE media_type = 'photo'
        ORDER BY playlist_title, title
        """,
    )
    return [_row_to_media(row) for row in cur.fetchall()]


def get_chapters_by_book(
    conn: sqlite3.Connection, playlist_title: str
) -> list[MediaRow]:
    """Return all audiobook chapters in a given book, ordered by title.

    Args:
        conn: Open database connection.
        playlist_title: The book/group name to filter on.

    Returns:
        List of ``MediaRow`` objects for media_type='audiobook'.
    """
    cur = conn.execute(
        """
        SELECT media_id, media_type, playlist_title, title, artist,
               duration_seconds, audio_path, photo_path,
               thumbnail_small_path, thumbnail_medium_path, thumbnail_large_path
        FROM media_items
        WHERE media_type = 'audiobook' AND playlist_title = ?
        ORDER BY title
        """,
        (playlist_title,),
    )
    return [_row_to_media(row) for row in cur.fetchall()]


def get_photos_by_group(
    conn: sqlite3.Connection, playlist_title: str
) -> list[MediaRow]:
    """Return all photos in a given album/group, ordered by title.

    Args:
        conn: Open database connection.
        playlist_title: The album/group name to filter on.

    Returns:
        List of ``MediaRow`` objects for media_type='photo'.
    """
    cur = conn.execute(
        """
        SELECT media_id, media_type, playlist_title, title, artist,
               duration_seconds, audio_path, photo_path,
               thumbnail_small_path, thumbnail_medium_path, thumbnail_large_path
        FROM media_items
        WHERE media_type = 'photo' AND playlist_title = ?
        ORDER BY title
        """,
        (playlist_title,),
    )
    return [_row_to_media(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------


def get_sync_state(conn: sqlite3.Connection, key: str) -> str | None:
    """Retrieve a sync state value by key.

    Args:
        conn: Open database connection.
        key: State key (e.g. ``'last_manifest_hash'``).

    Returns:
        The stored string value, or ``None`` if the key is absent.
    """
    cur = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_sync_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insert or replace a sync state key/value pair.

    Args:
        conn: Open database connection.
        key: State key (e.g. ``'last_manifest_hash'``).
        value: Value to store.
    """
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
        (key, value),
    )


# ---------------------------------------------------------------------------
# Synced settings
# ---------------------------------------------------------------------------

_PROFILE_SETTINGS_KEY = "profile_settings"
_THEME_KEY = "theme"
_SYNC_INTERVAL_KEY = "sync_interval_seconds"
_LAST_SERVER_TIME_KEY = "last_server_time"
_CLOCK_HEARTBEAT_KEY = "clock_heartbeat"


def get_profile_settings(conn: sqlite3.Connection) -> ProfileSettings:
    """Return the profile settings from the last sync.

    Args:
        conn: Open database connection.

    Returns:
        The stored settings; defaults if never synced or unreadable.
    """
    raw = get_sync_state(conn, _PROFILE_SETTINGS_KEY)
    if raw is None:
        return ProfileSettings()
    try:
        return ProfileSettings.model_validate_json(raw)
    except ValueError:
        logger.warning("Stored profile settings unreadable; using defaults")
        return ProfileSettings()


def set_profile_settings(conn: sqlite3.Connection, settings: ProfileSettings) -> None:
    """Persist the profile settings received in a manifest.

    Args:
        conn: Open database connection.
        settings: Settings to store.
    """
    set_sync_state(conn, _PROFILE_SETTINGS_KEY, settings.model_dump_json())


def get_sync_interval(conn: sqlite3.Connection) -> int | None:
    """Return the sync interval the server sent last, in seconds.

    Args:
        conn: Open database connection.

    Returns:
        The interval, or None if the server never sent one.
    """
    raw = get_sync_state(conn, _SYNC_INTERVAL_KEY)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def set_sync_interval(conn: sqlite3.Connection, seconds: int | None) -> None:
    """Persist (or clear, with None) the server-sent sync interval.

    Args:
        conn: Open database connection.
        seconds: Interval in seconds, or None.
    """
    if seconds is None:
        conn.execute("DELETE FROM sync_state WHERE key = ?", (_SYNC_INTERVAL_KEY,))
    else:
        set_sync_state(conn, _SYNC_INTERVAL_KEY, str(seconds))


def get_last_server_time(conn: sqlite3.Connection) -> datetime | None:
    """Return the server's clock as last seen by a sync.

    Args:
        conn: Open database connection.

    Returns:
        An aware datetime, or None if never recorded.
    """
    raw = get_sync_state(conn, _LAST_SERVER_TIME_KEY)
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def set_last_server_time(conn: sqlite3.Connection, server_time: datetime) -> None:
    """Persist the server's clock as seen by a sync.

    Args:
        conn: Open database connection.
        server_time: Aware datetime from the server's ``Date`` header.
    """
    set_sync_state(conn, _LAST_SERVER_TIME_KEY, server_time.isoformat())


# ---------------------------------------------------------------------------
# Wall-clock heartbeat
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockHeartbeat:
    """The system clock as last recorded while the player was running.

    Attributes:
        wall: The system clock reading (aware).
        boot_id: Kernel boot id of the boot that wrote it, or None if the
            platform does not provide one.
        trusted: Whether the player trusted the clock when it wrote this
            (False: it looked like a clock restored at boot and no sync
            has confirmed it since).
    """

    wall: datetime
    boot_id: str | None
    trusted: bool


def get_clock_heartbeat(conn: sqlite3.Connection) -> ClockHeartbeat | None:
    """Return the last recorded wall-clock heartbeat.

    Args:
        conn: Open database connection.

    Returns:
        The heartbeat, or None if never recorded or unreadable.
    """
    raw = get_sync_state(conn, _CLOCK_HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        wall = datetime.fromisoformat(data["wall"])
        boot_id = data.get("boot_id")
        trusted = bool(data["trusted"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if wall.tzinfo is None or not (boot_id is None or isinstance(boot_id, str)):
        return None
    return ClockHeartbeat(wall=wall, boot_id=boot_id, trusted=trusted)


def set_clock_heartbeat(conn: sqlite3.Connection, heartbeat: ClockHeartbeat) -> None:
    """Persist the wall-clock heartbeat (one row, replaced in a transaction).

    The caller commits; SQLite makes the replacement atomic, so a power cut
    leaves either the old or the new heartbeat, never a torn one.

    Args:
        conn: Open database connection.
        heartbeat: The heartbeat to store.
    """
    set_sync_state(
        conn,
        _CLOCK_HEARTBEAT_KEY,
        json.dumps(
            {
                "wall": heartbeat.wall.isoformat(),
                "boot_id": heartbeat.boot_id,
                "trusted": heartbeat.trusted,
            }
        ),
    )


def get_theme_definition(conn: sqlite3.Connection) -> ThemeDefinition | None:
    """Return the profile's theme as stored by the last sync.

    Args:
        conn: Open database connection.

    Returns:
        The stored theme, or None if there is none or it is unreadable (the
        player then shows the built-in theme of that id, or the default).
    """
    raw = get_sync_state(conn, _THEME_KEY)
    if raw is None:
        return None
    try:
        return ThemeDefinition.model_validate_json(raw)
    except ValueError:
        return None


def set_theme_definition(
    conn: sqlite3.Connection, theme: ThemeDefinition | None
) -> None:
    """Store (or clear) the profile's theme delivered by a sync.

    Does not commit.

    Args:
        conn: Open database connection.
        theme: The theme, or None to clear it.
    """
    if theme is None:
        conn.execute("DELETE FROM sync_state WHERE key = ?", (_THEME_KEY,))
    else:
        set_sync_state(conn, _THEME_KEY, theme.model_dump_json())
