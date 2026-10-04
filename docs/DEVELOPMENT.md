# Development Guide

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — workspace manager and runner
- ffmpeg — loudness normalization at ingest, and the tests that check it
  (`sudo apt-get install ffmpeg`, `brew install ffmpeg`)

## Installation

Install all packages in development mode from the workspace root:

```bash
uv sync --all-packages
```

This installs every package (`kidsplay-models`, `kidsplay-server`,
`kidsplay-cli`, `kidsplay-device`) along with their dev dependencies
(pytest, ruff, ty, httpx).

---

## Running the Server

The server entry point is `create_app_from_env`, a no-argument factory that
reads configuration from environment variables:

```bash
uv run uvicorn kidsplay_server.api.app:create_app_from_env --factory \
  --no-proxy-headers --reload --host 0.0.0.0 --port 8000
```

### Configuration

All paths are configurable via environment variables. Defaults are
suitable for local development (data lives in `~/.local/share/kidsplay/`).

| Variable | Default | Description |
|---|---|---|
| `KIDSPLAY_DB_PATH` | `~/.local/share/kidsplay/db.sqlite` | SQLite database file |
| `KIDSPLAY_MEDIA_STORE` | `~/.local/share/kidsplay/media` | Content-addressed media root |

Both paths are created automatically on first use if they don't exist.

`KIDSPLAY_PROCESSING_NICE` (0-19) and `KIDSPLAY_PROCESSING_JOBS` run ffmpeg at
a lower priority and limit it to N concurrent jobs. Both are off by default; the
all-in-one install sets them (see [ALL_IN_ONE.md](ALL_IN_ONE.md)).

Loudness normalization is configured with `KIDSPLAY_LOUDNORM` and
`KIDSPLAY_LOUDNESS_*`; see [LOUDNESS.md](LOUDNESS.md).

Settings that can change at runtime (device sync interval, WebP quality,
pairing, loudness targets) are edited on the web UI's Settings page and can be
pinned with `KIDSPLAY_SYNC_INTERVAL_SECONDS`, `KIDSPLAY_WEBP_QUALITY`,
`KIDSPLAY_LOUDNESS_TARGET_LUFS` and friends. In tests, pin them by saving them
through `update_server_settings` / `PUT /server-settings` or by setting the
environment variable (`create_app` takes no settings arguments); per-child volume
cap and bedtime live on each profile. See [SETTINGS.md](SETTINGS.md).

### Authentication

The web UI and management API require the admin (a single account). Device
sync (`/api/v1/devices/{id}/manifest`, `/api/v1/sync/file/{hash}`) keeps using
per-device API keys and needs no admin credentials.

| Variable | Default | Description |
|---|---|---|
| `KIDSPLAY_SOURCE_URL` | `https://github.com/anthonytw/kidsplay` | Where the web UI's "Source code" footer link points. The AGPL (section 13) asks anyone running a **modified** server for others to offer its source: point this at your repository. Only `http(s)://` URLs are used. |
| `KIDSPLAY_AUTH` | `enabled` | `disabled` turns admin auth off (only behind an authenticating proxy; logs a warning at startup). Any other value is a startup error. |
| `KIDSPLAY_ADMIN_PASSWORD` | unset | Pre-seeds the admin password if none is set yet. Never overwrites an existing one. Must be 8 to 1024 characters, like the setup page: on a fresh install anything else stops the server at startup with a clear message (on an existing install it only logs a warning). |
| `KIDSPLAY_SECRET_KEY_FILE` | `session_secret.key` next to the DB | Key that signs session cookies. Generated with mode 0600 on first run. |
| `KIDSPLAY_TRUSTED_PROXIES` | none | Comma-separated IPs/CIDRs of your reverse proxy. Only from these peers are `X-Forwarded-For` and `X-Forwarded-Proto` believed. Wildcards (`*`, `0.0.0.0/0`) are refused. See [Behind a reverse proxy](#behind-a-reverse-proxy). |
| `KIDSPLAY_COOKIE_SECURE` | auto | The session cookie is marked `Secure` whenever the request arrived over HTTPS (directly, or through a trusted proxy). `1` always marks it, `0` never does. |

- **Browser:** the first visit shows `/setup` to choose the password (at least
  8 characters); afterwards `/login`. Sessions last 14 days and are revoked
  server-side on logout.
- **First-run setup code:** on a fresh install (no admin password, no
  `KIDSPLAY_ADMIN_PASSWORD`), the server prints a boxed one-time code in its log
  at startup, e.g. `Setup code:  7KQ2M-XWD4R`, and `/setup` refuses to set a
  password without it (case, spaces and the dash don't matter). Use
  `docker compose logs kidsplay-server`, or the terminal/journal you started it
  from. The code lives in memory only: it works once, is gone as soon as a
  password exists, and a restart prints a fresh one. Wrong codes are
  rate-limited per client, and 20 wrong guesses from anywhere replace the code
  (the new one is printed in the log). Installs that pre-seed the password
  (`KIDSPLAY_ADMIN_PASSWORD`) or are set up by `kidsplay-allinone` never show
  or need a code. The code and the rate limits live in the server's memory, so
  run a single worker (as the examples here do).
- **Account page:** **Account** in the web UI's top bar changes the admin
  password (asks for the current one; ends the other browsers' sessions; API tokens
  keep working unless you tick **Also revoke all API tokens**) and has **Log out all sessions**. The same actions are
  `PUT /api/v1/auth/password` and `POST /api/v1/auth/sessions/revoke`
  (see [API.md](API.md)). The live log view re-checks its login every few
  seconds, so an open tab stops when you log out, change the password or the
  session expires.
- **CLI:** `kidsplay auth login` exchanges the password for an admin API token
  and saves it in `~/.config/kidsplay/credentials.json` (mode 0600; the
  directory is tightened to 0700 if it was looser).
  `--token` or `KIDSPLAY_TOKEN` override the saved token. Tokens can also be
  created and revoked on the web UI's **API Tokens** page.
- **Scripts:** send `Authorization: Bearer <token>`.

To reset a forgotten password, stop the server and clear the admin tables,
which also ends every session and revokes every admin token; the next visit
shows the setup page again:

```bash
sqlite3 db.sqlite "DELETE FROM admin_account; DELETE FROM admin_sessions; DELETE FROM admin_tokens;"
```

With `KIDSPLAY_AUTH=disabled` there is no session and so no CSRF token. To
keep a page on a sibling site (same parent domain, so "same-site" for cookies)
from making your browser call the API with the authenticating proxy's cookie,
the server refuses browser requests that change something when the browser
marks them `Sec-Fetch-Site: cross-site` or `same-site` (or, for old browsers
without that header, sends an `Origin` other than this server's `Host` or
`X-Forwarded-Host`). The CLI and scripts send neither header and are unaffected.

### Behind a reverse proxy

Behind Caddy, nginx or Traefik every connection comes from the proxy, so the
server can't tell clients apart or see that the browser used HTTPS. Tell it
which peers are your proxy:

```bash
KIDSPLAY_TRUSTED_PROXIES=172.18.0.1        # or a network, e.g. 172.16.0.0/12
```

From a listed peer the server takes the client address from
`X-Forwarded-For` (the right-most hop that is not itself a trusted proxy) and
the scheme from `X-Forwarded-Proto`. From anyone else those headers are
ignored, so a client can't pick its own address. The client address is then
what the admin login, first-run setup and pairing limits count against (each
client gets its own budget instead of the whole household sharing the
proxy's), what appears in the log, and what a parent sees on a pairing request.
IPv6 clients share a budget per /64. Over HTTPS the session cookie is marked
`Secure` with no further configuration.

- Start uvicorn with `--no-proxy-headers` (the Docker image and
  `kidsplay-allinone` do) so this setting is the only thing deciding whom to
  believe. uvicorn's own default trusts `127.0.0.1`.
- In Docker the proxy's address is the one the *container* sees: the bridge
  gateway (often `172.17.0.1` or `172.18.0.1`) for a proxy on the host, or the
  proxy container's address on a shared network. `docker network inspect`
  shows it; a network such as `172.16.0.0/12` is fine if only your proxy can
  reach the port. Publish the container port to the proxy only if you can.
- The proxy must *set* `X-Forwarded-For`/`-Proto` (Caddy's `reverse_proxy` and
  Traefik do by default; nginx needs `proxy_set_header X-Forwarded-For
  $proxy_add_x_forwarded_for;` and `X-Forwarded-Proto $scheme;`).

Caddy:

```caddyfile
kidsplay.example.com {
    reverse_proxy kidsplay-server:80
}
```

#### With single sign-on (Authelia, Authentik, oauth2-proxy)

KidsPlay's own admin login is enough on its own. If you already put your
services behind a single sign-on proxy, you can let it guard the web UI
instead, and keep one login:

1. Set `KIDSPLAY_AUTH=disabled`, so the server stops asking for its own admin
   password (it logs a warning at startup). Keep `KIDSPLAY_TRUSTED_PROXIES` set
   as above: the pairing limits still count per client.
2. Make sure **only the proxy can reach the server**: no published port, or one
   bound to the proxy alone. With auth disabled, anyone who reaches the server
   directly is an admin.
3. Leave the device routes open at the proxy. Handhelds can't do an
   interactive login; these routes check their own credentials (each device's
   API key, or the pairing request's one-time secret) and are rate-limited:

| Method | Path | Used for |
|---|---|---|
| `GET` | `/api/v1/devices/{device_id}/manifest` | Sync: what to download |
| `GET` | `/api/v1/sync/file/{content_hash}` | Sync: the files |
| `POST` | `/api/v1/pairing` | On-device pairing: ask for a code |
| `POST` | `/api/v1/pairing/poll` | On-device pairing: wait for approval |
| `POST` | `/api/v1/pairing/confirm` | On-device pairing: key saved |

Listing, approving and denying pairing requests (`/api/v1/pairing/requests`,
`/approve`, `/deny`) are admin actions and stay behind the proxy's login, as
does everything else. A test checks this table and the example below against
the server's routes.

Caddy with Authelia:

```caddyfile
kidsplay.example.com {
    @kidsplay_devices {
        method GET
        path_regexp ^/api/v1/(devices/[^/]+/manifest|sync/file/[^/]+)$
    }
    @kidsplay_pairing {
        method POST
        path /api/v1/pairing /api/v1/pairing/poll /api/v1/pairing/confirm
    }
    handle @kidsplay_devices {
        reverse_proxy kidsplay-server:80
    }
    handle @kidsplay_pairing {
        reverse_proxy kidsplay-server:80
    }
    handle {
        forward_auth authelia:9091 {
            uri /api/authz/forward-auth
            copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
        }
        reverse_proxy kidsplay-server:80
    }
}
```

### Production

```bash
export KIDSPLAY_DB_PATH=/srv/kidsplay/db.sqlite
export KIDSPLAY_MEDIA_STORE=/srv/kidsplay/media
uv run uvicorn kidsplay_server.api.app:create_app_from_env --factory \
  --no-proxy-headers --host 0.0.0.0 --port 8000 --workers 1
```

### Interactive API docs

With the server running, open `http://localhost:8000/docs` for the
auto-generated OpenAPI UI (Swagger). It requires the admin login. To use "Try
it out" on endpoints that change data, click **Authorize** and paste an admin
API token: browser-session requests from Swagger lack the CSRF header.

---

## Running Tests

```bash
# All packages
uv run pytest

# Single package
uv run pytest packages/kidsplay-server/tests/

# Single file
uv run pytest packages/kidsplay-server/tests/test_api_sync.py

# Single test
uv run pytest packages/kidsplay-server/tests/test_api_sync.py::TestSyncManifest::test_manifest_304_when_etag_matches

# Verbose output
uv run pytest -v

# Stop on first failure
uv run pytest -x
```

Tests run against real SQLite databases and a real media store in pytest's
`tmp_path` fixture — nothing is mocked. The loudness tests run real ffmpeg on
tones they generate, so ffmpeg must be on `PATH`. API tests use `httpx.AsyncClient`
pointed directly at the FastAPI app (no live server process needed).

---

## Code Quality

```bash
# Format and lint
uv run ruff format .
uv run ruff check . --fix

# Type check (src/ and tests/ of every package)
uv run ty check
```

Line length is 88 characters (ruff default). All public functions must have
type hints and Google-style docstrings.

`ty` is pinned to an exact version in `pyproject.toml`: it is pre-1.0 and its
checks change between releases, so upgrade it deliberately in its own PR. Only
explicit `# ty: ignore[rule]` comments suppress a diagnostic (blanket
`# type: ignore` is not honoured), and unused suppressions are errors. CI runs
all of these checks plus `uv run pytest` on every push and pull request.

---

## Demo, sample media and screenshots

The `demo/` directory holds the one-command demo and the README images.

```bash
just demo           # throwaway server + sample media + player window
just screenshots    # regenerate docs/images/ (README screenshots and GIF)
just sample-media   # regenerate demo/media/ (needs ffmpeg with flite; see below)
```

- `demo/media/` is the bundled sample media, generated by
  `demo/make_sample_media.py` and dedicated CC0. Keep it under 5 MB, and list
  every file's license in `THIRD_PARTY_NOTICES.md` (a test checks both).
  `just sample-media` needs an ffmpeg built with `flite` and `libmp3lame`
  (Debian/Ubuntu's is; **Homebrew's has no flite**, so on macOS run it in a
  Debian/Ubuntu container). It checks first and stops before touching anything,
  and it only swaps the new files in once all of them were generated, so a
  failure never leaves `demo/media/` empty.
- `just screenshots` drives the real web UI with headless Chromium
  (Playwright; run `uv run playwright install chromium` once) and the real
  player under SDL's dummy video driver. Seeds, timestamps and window sizes
  are fixed, and a file whose *pixels* are unchanged is left alone (PNG and GIF
  bytes differ between machines even when the picture does not), so a rerun
  touches only real changes. Fonts differ between operating systems, so the two
  web shots change when regenerated on another OS: commit those only when the
  UI changed. The script drives the player through the public
  `MusicPlayerApp.run_frame()` hook, not its private methods. Rerun it after UI
  changes and commit the images.
- The demo's server prints only warnings on its console, so the friendly
  output stays readable (`demo/quiet_server.py`); its Logs page in the web UI
  still shows everything.
- `just screenshots --mobile` captures every web page at a phone (375x812)
  and a desktop (1280x800) viewport, in English and Spanish, into
  `screenshots-mobile/` (git-ignored) and exits non-zero if a page scrolls
  sideways at phone width. `demo/tests/test_web_mobile.py` runs the same
  check in `pytest`, plus the import flow at phone size through the real
  login form. CI installs Chromium for it (`playwright install chromium`);
  without the browser these tests skip locally and fail in CI.
- The screenshot script navigates the web UI in a single browser context, so
  once the admin login lands, logging in is one step at the top of
  `capture_web` in `demo/screenshots.py`.

---

## Ingesting Media

Use the CLI (once implemented) or call the API directly with an admin API
token:

```bash
# Ingest a directory of music and assign it to a profile
curl -X POST http://localhost:8000/api/v1/media/ingest \
  -H "Authorization: Bearer $KIDSPLAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source_path": "/path/to/pica-pica-halloween",
    "media_type": "music",
    "playlist_title": "Pica-Pica - Halloween",
    "profile_ids": ["<profile-uuid>"]
  }'
```

Supported audio extensions: `.mp3`, `.m4a`, `.flac`, `.ogg`, `.wav`, `.aac`

Supported photo extensions: `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.gif`

---

## Package Responsibilities

| Package | Role |
|---|---|
| `kidsplay-models` | Pydantic models shared by all packages. Edit data contracts here. |
| `kidsplay-server` | FastAPI server: ingest, processing, storage, sync. Runs on the NAS. |
| `kidsplay-cli` | Click CLI: wraps the server API for terminal use. |
| `kidsplay-device` | pygame-ce player: syncs from server, plays media offline. Runs on RPi. |

---

## Project Layout (server package)

```
kidsplay-server/src/kidsplay_server/
├── config.py          ← env-var settings (KIDSPLAY_DB_PATH, KIDSPLAY_AUTH, ...)
├── auth.py            ← admin password, sessions, API tokens, session secret
├── database.py        ← async SQLite CRUD (aiosqlite, raw SQL)
├── storage.py         ← content-addressed filesystem (MediaStore)
├── processing/
│   ├── audio.py       ← mutagen: extract_metadata(), extract_artwork();
│   │                     ffmpeg loudnorm: measure_loudness(), normalize_loudness()
│   ├── images.py      ← Pillow: generate_thumbnails(), process_photo()
│   ├── pipeline.py    ← orchestrator: ingest_file(), ingest_directory(),
│   │                     normalize_media_item()
│   ├── queue_worker.py ← import queue worker: imports and loudness jobs, one at a time
│   ├── queue_wakeup.py ← wakes the worker when a job is queued
│   ├── resources.py   ← nice + one-job limit for ffmpeg/yt-dlp, cancellable
│   └── loudness_backfill.py ← queues library-wide loudness jobs, reports progress
├── store_gc.py        ← `kidsplay-server gc`: delete long-unreferenced store files
└── api/
    ├── app.py         ← create_app() factory, create_app_from_env() entry point
    ├── deps.py        ← FastAPI dependency types: DBConn, DBPath, Store
    ├── auth.py        ← require_admin / CSRF dependencies, /auth/* endpoints
    ├── media.py       ← /media/* endpoints
    ├── devices.py     ← /profiles/* and /devices/* endpoints
    └── sync.py        ← /devices/{id}/manifest, /sync/file/{hash}
```
