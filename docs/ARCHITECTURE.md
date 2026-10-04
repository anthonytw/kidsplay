# Architecture

## System Overview

```
┌──────────────────────────────────────────────────────────────┐
│  Home server / NAS (or the device itself)                   │
│  ┌─────────────────────────────────────┐                     │
│  │  kidsplay-server (Docker)           │                     │
│  │  ┌───────────┐  ┌────────────────┐  │  ┌──────────────┐   │
│  │  │ FastAPI   │  │ Media          │  │  │ Media Store  │   │
│  │  │ REST API  │  │ Processing     │  │  │ (filesystem) │   │
│  │  │ + Web UI  │  │ Pipeline       │  │  │              │   │
│  │  └─────┬─────┘  └───────┬────────┘  │  └──────────────┘   │
│  │        │                │           │                     │
│  │  ┌─────┴────────────────┴────────┐  │                     │
│  │  │  SQLite database              │  │                     │
│  │  └───────────────────────────────┘  │                     │
│  └─────────────────────────────────────┘                     │
└────────────────────┬─────────────────────────────────────────┘
                     │ HTTP (LAN only)
        ┌────────────┼────────────┐
        │            │            │
   ┌────┴─────┐  ┌───┴──────┐  ┌──┴────┐
   │ Device 1 │  │ Device 2 │  │  CLI  │
   │ (RPi CM4)│  │ (RPi CM4)│  │ (Mac) │
   │ Leo      │  │ Sofia    │  │       │
   └──────────┘  └──────────┘  └───────┘
```

## Data Flow

### Ingest → Process → Store

1. Admin triggers ingest via CLI or web UI, providing a directory, media type, and playlist title
2. Server walks directory, computes SHA-256 hash of each file
3. For each new file (hash not already in DB):
   a. Extract basic metadata (mutagen for audio, filesystem fallbacks)
   b. For audio: store original file unchanged, extract embedded artwork, and
      queue a background job that later stores a loudness-normalized MP3 for
      devices (EBU R128, see [LOUDNESS.md](LOUDNESS.md)); the item plays
      un-normalized until then
   c. Generate thumbnails at 60×60, 200×200, 480×480 (WebP, from artwork or photo)
   d. For photos: resize to fit 640×480 (WebP, aspect-ratio preserving, no upscale)
   e. Store all processed files in content-addressed filesystem
   f. Create DB record with playlist_title and derived title
4. If profile_ids provided, create assignment records

### Assign → Sync → Play

1. Admin assigns media to a profile via CLI or web UI
2. Device calls `GET /devices/{id}/manifest` (on startup + every 15 min)
3. Server builds manifest: all files needed for this device's profile
4. Device diffs manifest against local state
5. Downloads new files, deletes removed files
6. Updates local SQLite with metadata from manifest
7. Player reads from local SQLite + local files. No network needed.

## Content-Addressed Storage

All processed files are stored by SHA-256 hash:

```
{MEDIA_STORE}/
  audio/{hash[0:2]}/{hash}.mp3
  thumbnails/{hash[0:2]}/{hash}_{width}x{height}.webp
  photos/{hash[0:2]}/{hash}_{width}x{height}.webp
```

Benefits:
- **Deduplication:** Same song assigned to both kids stores the audio once
- **Cache-friendly:** Hash-based paths never change, safe to cache forever
- **Sync simplicity:** "Does this hash exist locally?" is the only question

The two-character prefix subdirectory prevents any single directory from
accumulating thousands of files (filesystem performance degrades around 10k entries).

## Database Design

### Server (SQLite via aiosqlite)

```sql
-- Core media catalog
CREATE TABLE media_items (
    id TEXT PRIMARY KEY,           -- UUID
    media_type TEXT NOT NULL,      -- 'music', 'audiobook', 'photo'
    content_hash TEXT UNIQUE NOT NULL,
    playlist_title TEXT NOT NULL,  -- Group/playlist name (top-level nav)
    title TEXT NOT NULL,           -- Individual item title
    artist TEXT,                   -- Optional artist/author display
    duration_seconds INTEGER,     -- Audio duration (NULL for photos)
    processing_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    loudness_source_lufs REAL,          -- measured on the original
    loudness_source_true_peak_dbtp REAL,
    loudness_gain_db REAL,              -- NULL: stored unchanged
    loudness_target_lufs REAL,          -- NULL: not normalized yet
    loudness_target_true_peak_dbtp REAL,
    loudness_mode TEXT                  -- linear, dynamic or capped; NULL: unknown
);

-- Processed output files
CREATE TABLE processed_files (
    id TEXT PRIMARY KEY,
    media_id TEXT NOT NULL REFERENCES media_items(id),
    content_hash TEXT NOT NULL,
    file_type TEXT NOT NULL,        -- 'audio', 'audio_source' (server-only original), 'thumbnail_small', etc.
    relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Children
CREATE TABLE profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Physical devices
CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    profile_id TEXT NOT NULL REFERENCES profiles(id),
    display_width INTEGER NOT NULL DEFAULT 640,
    display_height INTEGER NOT NULL DEFAULT 480,
    last_sync_at TEXT,
    last_sync_manifest_hash TEXT,
    api_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- Media-to-profile assignments
CREATE TABLE profile_media (
    profile_id TEXT NOT NULL REFERENCES profiles(id),
    media_id TEXT NOT NULL REFERENCES media_items(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, media_id)
);
```

### Device (SQLite via sqlite3)

```sql
-- Local copy of assigned media metadata
CREATE TABLE media_items (
    media_id TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    playlist_title TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT,
    duration_seconds INTEGER,
    audio_path TEXT,
    photo_path TEXT,
    thumbnail_small_path TEXT,
    thumbnail_medium_path TEXT,
    thumbnail_large_path TEXT
);

-- Play tracking (local only, synced up to server later)
CREATE TABLE play_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT NOT NULL,
    played_at TEXT NOT NULL,
    duration_played_seconds INTEGER
);

-- Sync state
CREATE TABLE sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Keys: 'last_manifest_hash', 'last_sync_at', 'profile_settings',
--       'sync_interval_seconds', 'last_server_time' (see docs/SETTINGS.md)
```

### Schema versions and downgrading

The server database records its schema version in `PRAGMA user_version`, and
`init_db` upgrades an older database step by step (`_MIGRATIONS` in
`kidsplay_server/database.py`). A server refuses to start on a database written
by a *newer* release.

**Do not run an older release against a database a newer one has upgraded.**
Releases from before versioning (the ones that had no import queue, then the
`yt_queue` table) do not know the version stamp. They recreate the tables they
expect next to the upgraded ones, and rows written while downgraded (queued
jobs, in particular) land in a table the newer release never reads. Upgrading
again finds the database already at the current version, so those rows stay
orphaned. To go back a release, restore a backup taken before the upgrade
(`docs/BACKUP.md`).

## Sync Protocol Detail

### Happy Path

```
Device                              Server
  │                                    │
  │  GET /devices/{id}/manifest        │
  │  If-None-Match: {last_hash}        │
  │───────────────────────────────────>│
  │                                    │
  │  200 OK                            │
  │  ETag: {new_hash}                  │
  │  Body: SyncManifest JSON           │
  │<───────────────────────────────────│
  │                                    │
  │  [diff local vs manifest]          │
  │                                    │
  │  GET /sync/file/{hash1}            │
  │───────────────────────────────────>│
  │  200 OK (file bytes)               │
  │<───────────────────────────────────│
  │                                    │
  │  GET /sync/file/{hash2}            │
  │───────────────────────────────────>│
  │  200 OK (file bytes)               │
  │<───────────────────────────────────│
  │                                    │
  │  [delete removed files]            │
  │  [update local SQLite]             │
  │  [store new manifest hash]         │
  │                                    │
```

### No Changes

```
Device                              Server
  │                                    │
  │  GET /devices/{id}/manifest        │
  │  If-None-Match: {current_hash}     │
  │───────────────────────────────────>│
  │                                    │
  │  304 Not Modified                  │
  │<───────────────────────────────────│
  │                                    │
  │  [skip sync, done]                 │
```

### Error Handling

- **Server unreachable:** Log warning, continue with local content, retry at next interval
- **Partial download failure:** Retry individual files up to 3 times, then skip and log
- **Disk full:** Stop sync, log error, continue with existing content
- **Corrupt download:** Verify SHA-256 after download, re-download on mismatch

### Local Transport (all-in-one)

With `"sync_transport": "local"` in the device config, only step 4 of the
protocol changes: instead of `GET /api/v1/sync/file/{content_hash}` the device
hard-links `{server_media_store}/{relative_path}` into its `media_root` (or
copies and verifies it across filesystems). The manifest is still fetched and
diffed over HTTP, and the device never modifies a linked file, since it is the
server's file. A local sync also does not count as a bedtime clock anchor: the
server's `Date` header is the device's own clock. See
[ALL_IN_ONE.md](ALL_IN_ONE.md).

## Server Configuration

The server reads its two required paths from environment variables at startup
via `kidsplay_server.config.Settings`.  Both paths are created automatically
if they don't exist.

| Variable | Default | Description |
|---|---|---|
| `KIDSPLAY_DB_PATH` | `~/.local/share/kidsplay/db.sqlite` | SQLite database file |
| `KIDSPLAY_MEDIA_STORE` | `~/.local/share/kidsplay/media` | Content-addressed media root |

Loudness normalization's deployment switches are `KIDSPLAY_LOUDNORM*`; its
targets are runtime server settings (`KIDSPLAY_LOUDNESS_*` pin them); see
[LOUDNESS.md](LOUDNESS.md).

Runtime settings (device sync interval, WebP quality, pairing, loudness targets) and per-profile
settings (volume cap, bedtime) are described in `docs/SETTINGS.md`.

The uvicorn entry point is `kidsplay_server.api.app:create_app_from_env`
(a no-argument factory suitable for `uvicorn --factory`).  The
`create_app(db_path, media_store_root)` factory is used directly in tests
to pass isolated `tmp_path` directories.

## Design Decisions

### Why not PostgreSQL?

Two users, running on a NAS. SQLite is simpler to deploy (no separate process),
backs up as a single file, and handles the load trivially. If this ever needs
concurrent write throughput, the migration path is straightforward — the SQL
is standard and the data access layer is a thin wrapper.

### Why pre-process everything server-side?

The RPi CM4 has limited CPU and RAM. Runtime image decoding (especially for
200x200 thumbnails in a scrolling list) causes visible frame drops. The v1
player stores raw image bytes in a pandas DataFrame and decodes them via PIL
on every display — this is the primary cause of slow performance. By
pre-rendering all thumbnails as correctly-sized WebP files, the device only
needs `pygame.image.load()` on files that are already the right dimensions.

### Why not WebSockets for sync?

Polling is simpler, more resilient, and good enough. A 15-minute poll interval
means new content appears within 15 minutes of being assigned. For a kids'
music player, this is functionally instant. WebSocket push would add connection
management complexity for negligible benefit.

### Why content-addressed storage instead of database BLOBs?

The v1 player stores thumbnail bytes directly in a pandas DataFrame. This
means loading the entire library loads every thumbnail into memory. Content-
addressed files on disk let the device load only what it's currently displaying,
and the filesystem provides the caching layer for free.

### Why WebP for thumbnails?

~30% smaller than JPEG at equivalent visual quality. The CM4 has hardware
decode support. The only downside is pygame-ce needs SDL_image built with
WebP support, which is standard on Raspberry Pi OS Bookworm.

### Why a single flat MediaItem model?

All three media types share the same two-level hierarchy (playlist → item)
and the same small set of fields. Separate models per type would add
complexity for no benefit — the device UI treats them identically except
for which file path to use (audio vs photo). The `media_type` field
controls processing and playback behavior.

### Why profiles separate from devices?

If a device breaks and gets replaced, the new device just points at the
same profile. If two siblings want to share a device, you can swap profiles.
If a kid outgrows certain content, you update the profile's assignments
without touching device configuration.
