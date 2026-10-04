# CLAUDE.md

## Project Overview

KidsPlay is a media management and playback system for two kids' Raspberry Pi CM4 handheld devices (gameboy-style cases, 640x480 screens). It consists of five Python packages in a uv workspace monorepo:

- **kidsplay-models** — Shared Pydantic models used by all other packages. Source of truth for all data contracts.
- **kidsplay-server** — FastAPI server, typically on a home server/NAS (or on the device itself). Handles media ingest, processing (transcoding, thumbnail generation, photo resizing), storage, device management, and sync manifest generation. Includes a web UI for media management.
- **kidsplay-cli** — Click-based CLI client that talks to the server REST API. Provides all media management operations from the terminal.
- **kidsplay-importer-ytdlp** — Optional importer plugin: YouTube audio via yt-dlp. The server core has no yt-dlp dependency; importers are discovered through the `kidsplay.importers` entry-point group (see `docs/IMPORTERS.md`).
- **kidsplay-device** — pygame-ce based player that runs on each RPi CM4. Syncs media from the server, plays audio, displays photos. Must work fully offline after sync.

## Architecture Principles

1. **All media processing happens server-side.** The device never decodes, resizes, or transcodes anything at runtime. It loads pre-processed files from disk. This is the single most important performance decision.
2. **Content-addressed storage.** Media files are stored by SHA-256 hash. Deduplication is automatic — the same song assigned to two kids is stored once.
3. **Manifest-based sync.** Devices pull a JSON manifest listing content hashes, compare against local state, and download/delete the diff. Server is authoritative; conflicts resolve as "server wins."
4. **Offline-first device.** The player must work with zero network connectivity. Sync is opportunistic (on startup + periodic timer). If the server is down, the device plays whatever it last synced.
5. **Per-profile content.** Each child has a profile. Each device is assigned to a profile. Media is assigned to profiles, not directly to devices. This means reassigning a device to a different kid is just changing the profile pointer.

## Package Structure

```
kidsplay/
├── CLAUDE.md                          # This file
├── pyproject.toml                     # uv workspace root
├── docs/
│   ├── ARCHITECTURE.md                # Design decisions and system overview
│   └── API.md                         # REST endpoint contracts
├── packages/
│   ├── kidsplay-models/               # Shared data models
│   │   ├── pyproject.toml
│   │   ├── src/kidsplay_models/
│   │   │   ├── __init__.py            # Public re-exports
│   │   │   ├── media.py               # MediaType, MediaItem
│   │   │   ├── device.py              # Device, Profile, ProfileMediaAssignment
│   │   │   ├── processing.py          # ProcessingStatus, ThumbnailSize, ProcessedFile, IngestRequest, IngestResult
│   │   │   ├── sync.py                # SyncManifest, SyncFileEntry, SyncMediaEntry
│   │   │   └── themes.py              # ThemeDefinition, built-in themes
│   │   └── tests/
│   │       └── test_models.py
│   ├── kidsplay-server/
│   │   ├── pyproject.toml
│   │   └── src/kidsplay_server/
│   │       ├── __init__.py
│   │       ├── config.py              # Settings, paths, thumbnail size definitions
│   │       ├── allinone.py            # kidsplay-allinone: server + player on one device
│       ├── pairing.py             # Pairing requests: codes, hashed binding secret, one-time key
│       ├── discovery.py           # Optional mDNS advertising of the server
│       ├── proxy.py               # Trusted reverse proxies: client address + scheme from X-Forwarded-*
│   │       ├── database.py            # SQLite via aiosqlite, table definitions, CRUD
│   │       ├── importers/             # Importer protocol, registry, built-in local + HTTP importers
│   │       ├── processing/            # Media processing pipeline
│   │       │   ├── __init__.py
│   │       │   ├── audio.py           # Metadata extraction, optional transcoding
│   │       │   ├── images.py          # Thumbnail generation, photo resizing
│   │       │   ├── pipeline.py        # Orchestrator: ingest → process → store
│   │       │   ├── resources.py       # nice + concurrency limits for ffmpeg
│   │       │   └── queue_worker.py    # Background import queue (importers with requires_queue, loudness jobs)
│   │       ├── storage.py             # Content-addressed filesystem operations
│   │       ├── store_gc.py            # kidsplay-server gc: delete long-unreferenced store files
│   │       ├── themes.py, theme_assets.py  # Custom themes and their validated assets
│   │       ├── api/                   # FastAPI routes
│   │       │   ├── __init__.py
│   │       │   ├── app.py             # FastAPI app factory
│   │       │   ├── media.py           # Media CRUD endpoints
│   │       │   ├── devices.py         # Device/profile management endpoints
│   │       │   └── sync.py            # Manifest and file download endpoints
│   │       └── web/                   # Web UI (Jinja2 + HTMX, later phase)
│   ├── kidsplay-cli/
│   │   ├── pyproject.toml
│   │   └── src/kidsplay_cli/
│   │       ├── __init__.py
│   │       ├── main.py                # Click group, entry point
│   │       ├── client.py              # httpx-based API client
│   │       ├── media.py               # Media management commands
│   │       ├── importers.py           # Importer listing command
│   │       └── devices.py             # Device/profile commands
│   ├── kidsplay-importer-ytdlp/       # Optional plugin: YouTube via yt-dlp
│   │   ├── pyproject.toml             # yt-dlp pin + kidsplay.importers entry point
│   │   └── src/kidsplay_importer_ytdlp/
│   │       ├── importer.py            # YtDlpImporter (fetch, queue retries, preview)
│   │       ├── preview.py             # yt-dlp -J playlist/video preview
│   │       └── ytdlp.py               # yt-dlp command, cookies, URL normalization
│   └── kidsplay-device/
│       ├── pyproject.toml
│       └── src/kidsplay_device/
│           ├── __init__.py
│           ├── config.py              # Device-local configuration
│           ├── layout.py              # Scales the 640x480 design to any resolution
│           ├── input_profiles.py      # Named key/joystick bindings (gpi2, keyboard, ...)
│           ├── theme.py               # Themes: colors, font, sounds, backgrounds
│           ├── sync.py                # Manifest diff + download client
│           ├── local_transport.py     # All-in-one: hard-link files from the server's store
│           ├── pairing.py             # On-device pairing client, atomic 0600 config.json writer
│           ├── pairing_screen.py      # Pairing screen (server pick, code + QR); pairing_app.py runs it
│           ├── keyboard.py            # On-screen keyboard (numeric layout first)
│           ├── discovery.py           # mDNS discovery of the server (_kidsplay._tcp)
│           ├── database.py            # Local SQLite for library cache
│           ├── player.py              # Playback state, controls
│           ├── app.py                 # Main pygame-ce application loop
│           └── views/                 # UI views (home, music, audiobooks, photos, play)
```

## Tech Stack — Mandatory Decisions

These are settled. Do not propose alternatives.

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Workspace/deps | **uv** | `uv sync`, `uv run`, lockfiles. |
| Shared models | **Pydantic v2** | Validation, serialization, schema generation. All packages import from `kidsplay-models`. |
| Server framework | **FastAPI** | Async, auto-OpenAPI from Pydantic, well-understood by coding LLMs. |
| Server database | **SQLite via aiosqlite** | Two users, async compatibility with FastAPI. No ORM — write SQL directly with parameter binding. |
| Server templates | **Jinja2 + HTMX** | Server-rendered web UI, no JS build pipeline. Later phase. |
| CLI framework | **Click** | Already used in v1. `kidsplay` as the single entry point. |
| Device GUI | **pygame-ce** | Drop-in replacement for pygame with better performance. No Qt. |
| Device database | **SQLite via sqlite3** | Sync runtime, no async needed on device. |
| Image processing | **Pillow** | Server-side only. Device never imports Pillow. |
| Audio metadata | **mutagen** | Server-side only for extraction. |
| HTTP client | **httpx** | Used in CLI (sync client) and device (sync client). Async in device sync thread. |
| Thumbnails | **WebP format** | ~30% smaller than JPEG at equivalent quality. |
| Testing | **pytest + pytest-asyncio** | All packages. httpx.AsyncClient for API tests. |
| Type checking | **ty** (exact pinned version) | Astral toolchain, same as uv/ruff. Checks `src/` and `tests/`. Type hints on all functions, no Any without justification. Suppress only with `# ty: ignore[rule]` plus a reason. |
| Formatting | **ruff** | Format and lint. Single tool, fast. |

## Content-Addressed Storage Layout

All processed media lives under a single `MEDIA_STORE` root on the server:

```
{MEDIA_STORE}/
  audio/
    ab/cd1234...sha256.mp3
  thumbnails/
    ef/gh5678...sha256_200x200.webp
    ef/gh5678...sha256_60x60.webp
    ef/gh5678...sha256_480x480.webp
  photos/
    ij/kl9012...sha256_640x480.webp
```

Path format: `{type}/{hash[0:2]}/{hash}.{ext}` (first two chars as subdirectory for filesystem performance).

Thumbnail path format: `thumbnails/{hash[0:2]}/{hash}_{width}x{height}.webp`

## Thumbnail Sizes

The device UI requires exactly these sizes (defined in `kidsplay_models.processing.ThumbnailSize`):

| Name | Dimensions | Used for |
|------|-----------|----------|
| SMALL | 60×60 | Playback bar |
| MEDIUM | 200×200 | List views, grid cells |
| LARGE | 480×480 | Full-screen play view |

All thumbnails maintain aspect ratio within these bounds (may be smaller on one axis). Generated as WebP.

## Device Sync Protocol

1. Device calls `GET /api/v1/devices/{device_id}/manifest`
2. Server returns `SyncManifest`: list of `SyncManifestEntry` (content_hash, relative_path, size_bytes, media_type)
3. Device compares against local manifest stored in its SQLite DB
4. New/changed entries: `GET /api/v1/sync/file/{content_hash}` to download
5. Removed entries: delete local file and DB record
6. Device updates local manifest hash to mark sync complete

Sync runs: on app startup, then every 15 minutes. Runs in a background thread — never blocks the UI.

## Testing Requirements

- Every module must have corresponding test file in `tests/`
- Use pytest fixtures for common setup (temp directories, sample data, test clients)
- Server API tests: use `httpx.AsyncClient` with FastAPI's `app` directly (no real server process)
- Mock filesystem operations — never create real files outside `tmp_path` fixtures
- Mock network calls in device tests — never hit a real server
- Target: every public function has at least one test
- Run all tests: `uv run pytest` from workspace root
- Run package tests: `cd packages/kidsplay-models && uv run pytest`

## Code Style

- Type hints on all function signatures. Return types included.
- Google-style docstrings on all public functions and classes.
- No wildcard imports (`from x import *`).
- No `Any` type without a comment explaining why.
- Ruff for formatting and linting: `uv run ruff check .` and `uv run ruff format .`
- Line length: 88 characters.
- Import order: stdlib → third-party → local (ruff handles this).

## Development Commands

```bash
# Install all packages in development mode
uv sync --all-packages

# Run all tests across all packages
uv run pytest

# Run tests for a specific package
uv run pytest packages/kidsplay-models/tests/

# Format and lint
uv run ruff format .
uv run ruff check . --fix

# Type check (src/ and tests/ of every package; config in pyproject.toml)
uv run ty check

# Start the server (development)
uv run uvicorn kidsplay_server.api.app:create_app_from_env --factory --no-proxy-headers --reload --host 0.0.0.0 --port 8000

# CLI usage
uv run kidsplay --help
uv run kidsplay media ingest /path/to/music
uv run kidsplay device list
```

## Agent Workflow Notes

When writing issue specs (for humans or coding agents):

- Reference specific files and functions by path
- Include acceptance criteria as testable assertions
- Specify which package is affected
- Link to the API contract in `docs/API.md` for interface-dependent work
- One issue per module. Do not combine "implement endpoint X" with "implement CLI command for X" — those are separate issues with a shared contract.
