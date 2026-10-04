# API Endpoints

Base URL: `http://{server}:8000/api/v1`

All endpoints accept and return JSON unless noted otherwise.
Request/response models reference types from `kidsplay-models`.

## Authentication

Every endpoint except `POST /auth/login` and the device-facing sync endpoints
requires the admin, via either:

- `Authorization: Bearer {admin_token}` — an admin API token (`kpa_...`), or
- the web UI's session cookie. Cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE`
  requests must also send the session's CSRF token in `X-CSRF-Token`
  (the web UI does this automatically); otherwise they get
  `403 CSRF_FAILED`.

If an `Authorization` header is sent, it alone decides; a cookie is not used
as a fallback. Unauthenticated requests get `401 UNAUTHORIZED`. When the
server runs with `KIDSPLAY_AUTH=disabled`, no admin credentials are needed,
but a browser request that changes something (`POST`/`PUT`/`PATCH`/`DELETE`)
and says it comes from another site (`Sec-Fetch-Site: cross-site`/`same-site`,
or an `Origin` that is not this server) gets `403 CSRF_FAILED`. Requests with
neither header (the CLI, scripts) are unaffected.

The sync endpoints use device API keys instead (see
[Sync](#sync-device-facing)); admin tokens are not accepted there.

### `POST /auth/login`

Exchange the admin password for a new admin API token (used by
`kidsplay auth login`). No authentication required.

**Request body:** `AdminLoginRequest` — `{"password": "...", "token_name": "laptop"}`
(`token_name` defaults to `kidsplay-cli`).

**Response:** `201 Created` with `AdminTokenCreated` — `id`, `name`,
`created_at`, `last_used_at`, and `token`. The token secret is returned only
here; the server stores just its hash.

**Errors:** `401 UNAUTHORIZED` (wrong password), `409 SETUP_REQUIRED` (no
admin password set yet), `429 RATE_LIMITED` (10 failures within 5 minutes
from one client).

### `GET /auth/status`

**Response:** `AuthStatus` — `{"auth_enabled": true, "method": "token"}`
(`method` is `token`, `session` or `disabled`).

### `PUT /auth/password`

Change the admin password. **Request body:** `AdminPasswordChange` —
`{"current_password": "...", "new_password": "...", "revoke_tokens": false}`.
Ends every browser session except the caller's own. API tokens keep working
unless `revoke_tokens` is `true` (optional, default `false`), which revokes all
of them, including the one used for this call. **Response:**
`204 No Content`.

**Errors:** `403 WRONG_PASSWORD` (counts against the same per-client limit as
login), `422 PASSWORD_TOO_SHORT` (under 8 characters), `429 RATE_LIMITED`,
`409 AUTH_DISABLED`.

### `POST /auth/sessions/revoke`

Log out every browser session, the caller's included. API tokens are not
affected. **Response:** `200` with `{"revoked": 2}`.

### `GET /auth/tokens`

List admin API tokens (`list[AdminToken]`, without secrets).

### `POST /auth/tokens`

Create an admin API token. **Request body:** `AdminTokenCreate` —
`{"name": "backup script"}`. **Response:** `201 Created` with
`AdminTokenCreated`.

### `DELETE /auth/tokens/{token_id}`

Revoke a token. **Response:** `204 No Content`; `404 NOT_FOUND` if unknown.

---

## Profiles

### `GET /profiles`

List all profiles.

**Response:** `list[Profile]`

### `POST /profiles`

Create a new profile.

**Request:** `ProfileCreate`
**Response:** `Profile` (201 Created)

### `GET /profiles/{profile_id}`

Get a single profile.

**Response:** `Profile`

### `DELETE /profiles/{profile_id}`

Delete a profile. Fails if any devices are still linked to it. The
profile's settings are deleted with it.

**Response:** 204 No Content

### `GET /profiles/{profile_id}/settings`

Get a profile's settings. A profile whose settings were never saved returns
the defaults.

**Response:** `ProfileSettings`; 404 if the profile does not exist.

### `PUT /profiles/{profile_id}/settings`

Replace a profile's settings. Omitted fields take their defaults, except
`language` and `theme`, which keep their stored value when the key is absent
(send `null` to clear one); unknown fields are ignored. Devices linked to
the profile receive the change in the manifest at their next sync.

**Request:** `ProfileSettings`, for example:

```json
{
  "max_volume": 70,
  "volume_buttons": false,
  "ui_sounds": true,
  "bedtime_mode": "sleep_screen",
  "bedtime_schedule": {
    "mon": {"bedtime": "20:00", "wake": "07:00"},
    "sat": {"bedtime": "21:00", "wake": "08:30"}
  },
  "language": "es"
}
```

- `max_volume`: 0-100 (percent), default 100.
- `volume_buttons`: in-app volume buttons: `true` (on), `false` (off), or
  `null` / omitted (the default: the device decides from its input profile, on
  for a keyboard, off for the GPi Case 2). `null` is left out of the JSON the
  server returns and sends in the manifest.
- `ui_sounds`: `true` (the default) or `false`: whether button presses play a
  short sound. Off also stops the music dipping under it. Omitted means `true`.
- `bedtime_mode`: `off` | `audiobooks_only` | `sleep_screen`, default `off`.
- `bedtime_schedule`: keys `mon` ... `sun`; a missing day has no bedtime.
  `wake` not after `bedtime` means the next morning. The two must differ.
- `language`: `en` | `es`, the language of the child's device screens. Omitted
  keeps the stored language; an explicit `null` means "never chosen", which
  devices show as Spanish (what every device showed before this setting
  existed). New profiles are created with
  `en`. Any other value is rejected with 422 and error code
  `UNSUPPORTED_LANGUAGE`.
- `theme`: the id of a theme from `GET /themes` (built-in or custom), or
  `null`: the child picks a color on the device. Omitted keeps the stored
  theme. An id that is not a known theme is rejected with 422 and error code
  `UNKNOWN_THEME`. See [THEMES.md](THEMES.md).

**Response:** `ProfileSettings`; 404 if the profile does not exist, 422 if
the body is invalid.

---

## Themes

Admin authentication. A theme is `{id, name, builtin, colors, assets}`:
`colors` has the nine `#rrggbb` fields `bg`, `surface`, `surface_sel`,
`primary`, `text`, `text_dim`, `text_bright`, `accent`, `progress_bg`; `assets`
lists `{role, content_hash, relative_path, size_bytes}`. Built-in themes cannot
be changed or deleted (409, error code `THEME_BUILTIN`). See
[THEMES.md](THEMES.md) for the asset rules and examples.

### `GET /themes`

The built-in themes, then the custom ones by name. **Response:**
`list[ThemeDefinition]`.

### `GET /themes/{theme_id}`

One theme; 404 if unknown.

### `PUT /themes/{theme_id}`

Create a custom theme, or replace its name and palette (uploaded assets are
kept). **Request:** `{"name": "...", "colors": {...}}`. **Response:**
`ThemeDefinition`; 422 for an invalid id (1-40 lowercase letters, digits,
hyphens) or color, 409 for a built-in id.

### `DELETE /themes/{theme_id}`

Delete a custom theme (204). Profiles that chose it show the default theme from
their devices' next sync. The asset files stay in the media store. 404 if
unknown, 409 for a built-in.

### `PUT /themes/{theme_id}/assets/{role}`

Upload an asset (`multipart/form-data`, field `file`). `role` is `background`,
`home_background`, `font`, `sound_move`, `sound_select`, `sound_back` or
`sound_open`. The file is validated first; **422** (error code
`INVALID_THEME_ASSET`) says why it was refused. An asset of the same role is
replaced. **Response:** the `ThemeDefinition`; 404 if the custom theme does
not exist.

### `DELETE /themes/{theme_id}/assets/{role}`

Remove an asset (the file stays in the store). **Response:** the
`ThemeDefinition`; 404 if the theme or the asset does not exist.

---

## Devices

### `GET /devices`

List all devices.

**Response:** `list[Device]`

### `POST /devices`

Register a new device.

**Request:** `DeviceCreate`
**Response:** `Device` (201 Created, includes generated `api_key`)

### `GET /devices/{device_id}`

Get a single device.

**Response:** `Device`

### `DELETE /devices/{device_id}`

Unregister a device.

**Response:** 204 No Content

### `PATCH /devices/{device_id}`

Update device fields (name, profile_id, display dimensions).

**Request:** Partial `DeviceCreate` (any subset of fields)
**Response:** `Device`

---

## Media

### `POST /media/ingest`

Ingest media from a server-side filesystem path (`source_path`) or a URL
(`source_url`). A URL is fetched inline by the first installed importer that
can handle it (see [IMPORTERS.md](IMPORTERS.md)); a URL no importer handles
gives a `failed` result.

**Request:** `IngestRequest`
**Response:** `IngestBatchResult`

This is an async operation for directories. For single files, processing
may complete synchronously and the result includes `ProcessingStatus.READY`.
For directories, items start as `PENDING` and are processed in the background.

Audio is stored as it is and the request returns; its loudness normalization
(see [LOUDNESS.md](LOUDNESS.md)) is queued as a background job and finishes
later, so the item is playable, "not normalized" until then. Without ffmpeg, or
if normalization fails, the item stays that way.

### `GET /media`

List media items with optional filtering.

**Query params:**
- `media_type` (optional): Filter by type (`music`, `audiobook`, `photo`)
- `profile_id` (optional): Only media assigned to this profile
- `playlist_title` (optional): Filter by playlist/group name
- `status` (optional): Filter by processing status
- `q` (optional): Search across title, artist, playlist_title
- `limit` (optional, default 100): Max results
- `offset` (optional, default 0): Pagination offset

**Response:** `list[MediaItem]`

### `GET /media/{media_id}`

Get a single media item.

**Response:** `MediaItem`. Audio items carry the loudness fields
`loudness_source_lufs`, `loudness_source_true_peak_dbtp`, `loudness_gain_db`,
`loudness_mode` (`linear`, `dynamic` or `capped`; `null` if unchanged or
recorded before the mode existed), `loudness_target_lufs` and
`loudness_target_true_peak_dbtp` (`loudness_target_lufs: null` means not
normalized yet, or its job is still waiting).

### `DELETE /media/{media_id}`

Delete a media item and all its processed files.

**Response:** 204 No Content

### `POST /media/{media_id}/assign`

Assign a media item to one or more profiles.

**Request:**
```json
{
  "profile_ids": ["uuid1", "uuid2"]
}
```

**Response:** `list[ProfileMediaAssignment]`

### `DELETE /media/{media_id}/assign/{profile_id}`

Remove a media assignment.

**Response:** 204 No Content

### `GET /media/{media_id}/files`

List processed files for a media item.

**Response:** `list[ProcessedFile]`. A normalized audio item has an `audio`
file (the normalized MP3 devices sync) and an `audio_source` file (the
original, kept on the server and never in a manifest).

### `POST /media/normalize`

Queue loudness normalization of existing music and audiobooks. One job per
item goes on the import queue, where the worker runs them one at a time; the
request returns at once. Items already normalized to their type's current target
are skipped, so repeating the request re-encodes nothing, and an item that
already has an unfinished job is not queued twice. It is fine to call while
uploads are still normalizing.

**Request:** `NormalizeRequest`, exactly one of:
```json
{"all": true}
{"media_ids": ["uuid1", "uuid2"]}
```

**Response:** 202 with `NormalizeStatus` (`running`, `total`, `normalized`,
`skipped`, `unchanged`, `failed`, `errors`, `errors_omitted`, `started_at`,
`finished_at`). `errors` holds at most 20 messages; `errors_omitted` counts
the rest. 422 if the body selects nothing or both.

### `GET /media/normalize`

Progress of the current or last run: the loudness jobs on the queue, which
include the ones each audio upload queued. A run is a stretch of busy queue; jobs
queued while it is unfinished belong to it. Kept in the database, so a restart
does not lose it.

**Response:** `NormalizeStatus`

### `POST /media/preview`

List the tracks at a video or playlist URL without downloading anything.
Handled by the first importer that can handle the URL, if it supports previews
(YouTube does, with the `kidsplay-importer-ytdlp` plugin installed).

**Request:**
```json
{
  "url": "https://www.youtube.com/playlist?list=PL...",
  "max_items": 50
}
```

`max_items` is optional (default 50) and caps the playlist entries returned.

**Response:** `ImportPreview` (`kidsplay_server.importers`): `is_playlist`,
`playlist_title`, `tracks` (`url`, `title`, `artist`, `duration_seconds`,
`thumbnail_url`), `total_available`, `truncated`, and `debug` (`command`,
`returncode`, `stderr`).

**Errors:** 422 `PREVIEW_UNSUPPORTED` when no installed importer can preview
the URL; 422 `PREVIEW_FAILED` (with `debug`) when the preview itself fails.

---

## Importers

### `GET /importers`

List installed importers in the order they are tried (plugins first, then the
built-in `local` and `http`).

**Response:** `list[ImporterInfo]`
```json
[
  {"name": "ytdlp", "label": "YouTube", "requires_queue": true, "supports_preview": true},
  {"name": "local", "label": "Local file or folder", "requires_queue": false, "supports_preview": false},
  {"name": "http", "label": "Web URL", "requires_queue": false, "supports_preview": false}
]
```

### `GET /importers/match`

Return the importer that would handle a source.

**Query params:**
- `source` (required): URL or server-side path

**Response:** `ImporterInfo`, or 404 `NO_IMPORTER`.

---

## Import queue

Background imports with retries, for slow or rate-limited sources (importers
with `requires_queue`, such as YouTube). A background worker processes one
pending job at a time and passes the attempt number to the importer so it can
back off. A job fails without retrying if its importer is not installed.

### `POST /queue`

Queue a source for import. Replaces the former `POST /queue/youtube`.

**Request:** `QueueRequest`
```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "importer": null,
  "media_type": "music",
  "playlist_title": "Favourites",
  "profile_ids": [],
  "title_override": null,
  "artist_override": null,
  "max_retries": 5
}
```

`importer` is optional: omit it to use the first importer whose `can_handle`
matches `url`. The chosen importer normalizes the URL before it is stored.

**Response:** 201, `QueueItem` (includes the resolved `importer`).

**Errors:** 422 `UNKNOWN_IMPORTER` (named importer not installed) or
`NO_IMPORTER` (no importer can handle the URL).

### `GET /queue`

List queue items, newest first. Loudness-normalization jobs (importer
`loudness`) share the queue but are not imports; they are left out here and
reported by `GET /media/normalize`.

**Query params:** `status` (optional), `limit` (default 100), `offset` (default 0)

**Response:** `list[QueueItem]`

### `GET /queue/{item_id}`

**Response:** `QueueItem`, including the full per-attempt `log`.

### `DELETE /queue/{item_id}`

**Response:** 204 No Content

### `POST /queue/{item_id}/retry`

Reset a `failed` or `cancelled` item to `pending` with a fresh set of retries.

**Response:** `QueueItem`; 409 `INVALID_STATE` for other states.

---

## Sync (Device-facing)

These endpoints are called by devices during sync.
Authentication: `Authorization: Bearer {device.api_key}` header. They do not
use admin authentication and are unaffected by `KIDSPLAY_AUTH`.

### `GET /devices/{device_id}/manifest`

Get the sync manifest for a device.

**Headers:**
- `If-None-Match` (optional): Previous manifest hash for change detection

**Response:**
- `200 OK` with `SyncManifest` body and `ETag` header if content has changed.
  The body includes the profile's `profile_settings` and the server's
  `sync_interval_seconds` (`null` until an interval is saved or pinned; the
  device then keeps its own); a change to either changes the hash. When the
  profile chose a theme, `theme` holds its full definition and the theme's
  asset files are listed in `files` with `file_type: "theme"`; without one,
  `theme` is `null` and the hash is what it was before themes existed.
- `304 Not Modified` if manifest hash matches `If-None-Match`

Both responses carry `X-KidsPlay-Server-Id`, the server's stable identity. A
device pins the id it paired with and refuses a manifest from a server that
answers with another (or none); see [PAIRING.md](PAIRING.md#server-identity).

### `GET /sync/file/{content_hash}`

Download a processed file (or a theme asset) by its content hash.

**Response:** Raw file bytes with appropriate `Content-Type` header.

Returns 404 if the hash is not found in the media store.

---

## Pairing

On-device pairing (see [PAIRING.md](PAIRING.md) for the flow and the security
reasoning). The two device-facing endpoints need **no credentials**; they are
rate-limited per client (429 `RATE_LIMITED`) and refuse with 403
`PAIRING_DISABLED` when the `pairing_enabled` server setting is off. Models are
in `kidsplay_models.pairing`.

### `POST /pairing` (device, unauthenticated)

A device asks to be added.

**Request:**

```json
{
  "code": "ABCD-2345",
  "binding_secret": "<32+ random characters, kept by the device>",
  "device_name": "Handheld",
  "display_width": 640,
  "display_height": 480
}
```

The code is 8 characters from `23456789ABCDEFGHJKMNPQRSTUVWXYZ` (dash and case
ignored). The secret is stored only as a hash.

**Response:** `201` with `{"code": "ABCD2345", "expires_at": "...",
"poll_interval_seconds": 3, "server_id": "<uuid>"}`. `server_id` is the
server's stable identity (see [PAIRING.md](PAIRING.md#server-identity)). A code
that a live request already holds gets **the same `201`** as a free one, so
probing can't tell which codes are live; nothing is stored for the newcomer,
whose polls then find nothing. `422` for a malformed code or a secret shorter
than 32 characters; `429 TOO_MANY_PENDING` if the server holds too many
requests.

### `POST /pairing/poll` (device, unauthenticated)

**Request:** `{"code": "ABCD-2345", "binding_secret": "..."}`

**Response:**
- `200 {"status": "pending"}`: not approved yet; poll again.
- `200 {"status": "approved", "device_id": "...", "api_key": "...",
  "server_id": "..."}`: only for the holder of the secret. The same secret gets
  the same key again for up to 2 minutes, until the device calls
  `POST /pairing/confirm`, so a lost response or a failed write on the device
  doesn't strand it.
- `404 PAIRING_NOT_FOUND`: unknown code, or wrong secret (indistinguishable).
- `410 PAIRING_EXPIRED` (10 minutes, plus 2 minutes to collect after approval),
  `410 PAIRING_USED` (confirmed, or the 2 minutes are over), `403
  PAIRING_DENIED`.

### `POST /pairing/confirm` (device, unauthenticated)

The device has saved its config; the server closes the request at once.
Optional (the request closes by itself after the re-delivery window).

**Request:** `{"code": "ABCD-2345", "binding_secret": "..."}`

**Response:** `204`, idempotent. `404 PAIRING_NOT_FOUND` for an unknown code or a
wrong secret. Counts against the same limits as polls.

### `GET /pairing/requests` (admin)

Pending, unexpired requests: `code`, `device_name`, `display_width`,
`display_height`, `client` (remote address), `created_at`, `expires_at`.

### `POST /pairing/approve` (admin)

Approve a request and create the device.

**Request:** `{"code": "ABCD-2345", "profile_id": "<uuid>", "name": "Leo's player"}`
(`name` optional; defaults to the device's own suggestion).

**Response:** `200` with `{"device_id", "name", "profile_id"}`. The API key is
not returned: it goes to the device. Errors: 404 `PAIRING_NOT_FOUND` or a
missing profile, 409 `PAIRING_NOT_PENDING`, 410 `PAIRING_EXPIRED`, 403
`PAIRING_DISABLED`.

### `POST /pairing/deny` (admin)

**Request:** `{"code": "ABCD-2345"}`. **Response:** `204`.

---

## Server settings

Runtime settings edited on the web UI's Settings page. An environment
variable, when set, wins and locks the setting (see `docs/SETTINGS.md`).

### `GET /server-settings`

**Response:**

```json
{
  "values": {
    "sync_interval_seconds": 900,
    "webp_quality": 85,
    "pairing_enabled": true,
    "loudness_target_lufs": -16.0,
    "loudness_target_lufs_music": null,
    "loudness_target_lufs_audiobook": null
  },
  "env_locked": ["webp_quality"],
  "env_vars": {
    "sync_interval_seconds": "KIDSPLAY_SYNC_INTERVAL_SECONDS",
    "webp_quality": "KIDSPLAY_WEBP_QUALITY",
    "pairing_enabled": "KIDSPLAY_PAIRING_ENABLED",
    "loudness_target_lufs": "KIDSPLAY_LOUDNESS_TARGET_LUFS",
    "loudness_target_lufs_music": "KIDSPLAY_LOUDNESS_TARGET_LUFS_MUSIC",
    "loudness_target_lufs_audiobook": "KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIOBOOK"
  }
}
```

### `PUT /server-settings`

Save or reset settings. Omitted or `null` fields are unchanged; `reset`
lists keys to return to their default (for a per-type loudness target,
"same as the overall target"). A key the server does not know is rejected.

**Request:** `{"sync_interval_seconds": 600, "reset": ["webp_quality"]}`
**Response:** as `GET`; 409 `ENV_LOCKED` if a key is set by an environment
variable, 422 `INVALID_SETTING` for an out-of-range value or an unknown `reset` key,
422 (validation error) for an unknown field. A changed loudness target applies to
new ingests; queue `POST /media/normalize` to bring existing items to it.

---

## Bulk Operations

### `POST /media/assign-batch`

Assign multiple media items to a profile in one call.

**Request:**
```json
{
  "profile_id": "uuid",
  "media_ids": ["uuid1", "uuid2", "uuid3"]
}
```

**Response:**
```json
{
  "assigned": 3,
  "already_assigned": 0,
  "errors": []
}
```

### `POST /profiles/{profile_id}/assign-group`

Assign all media matching a filter to a profile. Useful for "give Leo
all the Pica-Pica songs."

**Request:**
```json
{
  "media_type": "music",
  "playlist_title": "Pica-Pica - Halloween"
}
```

**Response:**
```json
{
  "assigned": 12,
  "already_assigned": 3,
  "total_matched": 15
}
```

---

## Error Format

All error responses use this format:

```json
{
  "detail": "Human-readable error message",
  "error_code": "MACHINE_READABLE_CODE"
}
```

Standard HTTP status codes:
- 400: Bad request (validation error)
- 401: Not authenticated (`UNAUTHORIZED`)
- 403: Forbidden, e.g. missing CSRF token (`CSRF_FAILED`)
- 404: Resource not found
- 409: Conflict (e.g., duplicate content_hash on ingest)
- 422: Unprocessable entity (Pydantic validation failure)
- 500: Internal server error
