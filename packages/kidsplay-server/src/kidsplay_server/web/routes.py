"""Web UI routes returning Jinja2-rendered HTML pages.

All routes here serve admin pages for human operators. ``create_app``
guards the whole router with ``require_admin_page`` (redirect to ``/login``).
Login, logout and first-run setup live in ``auth_routes``.

Routes
------
GET  /                 — Dashboard: counts by type, quick links.
GET  /media            — Media browser with type/profile/search filter.
GET  /media/import     — Import page: importer previews, photo crop, bulk filesystem.
GET  /queue            — Import queue: status, logs, retry/delete actions.
GET  /profiles         — Profile list with create / delete.
GET  /profiles/{id}/settings — Per-profile settings form (volume cap, bedtime).
GET  /devices          — Device list with create / delete / reveal API key.
GET  /settings         — Server settings (env-locked values shown read-only).
GET  /logs             — Real-time log viewer (SSE).
GET  /logs/stream      — SSE stream of log entries for the log viewer.
GET  /web/media-file/{content_hash} — Serve a processed file to the browser.
POST /web/photo-proxy  — Proxy an image URL for display in Cropper.js.
"""

import asyncio
import collections.abc
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

import aiosqlite
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from kidsplay_models import (
    DEFAULT_LANGUAGE,
    LANGUAGE_NAMES,
    LEGACY_DEVICE_LANGUAGE,
    BedtimeMode,
    ProcessingStatus,
    Weekday,
    normalize_language,
    short_server_id,
)
from kidsplay_models.media import MediaType
from kidsplay_server import pairing
from kidsplay_server.api.auth import admin_still_authenticated, auth_template_context
from kidsplay_server.api.deps import DBConn, Importers, Store
from kidsplay_server.api.sync import build_file_response
from kidsplay_server.auth import MIN_PASSWORD_LENGTH
from kidsplay_server.config import source_url
from kidsplay_server.database import (
    get_or_create_server_id,
    get_profile,
    get_profile_settings,
    list_distinct_artists,
    list_distinct_playlist_titles,
    list_normalizing_media_ids,
)
from kidsplay_server.i18n import (
    N_,
    gettext_now,
    i18n_template_context,
    install_jinja_i18n,
)
from kidsplay_server.importers import SupportsPreview
from kidsplay_server.logging_setup import LogBroadcaster, LogEntry, get_broadcaster
from kidsplay_server.server_settings import (
    ENV_VARS,
    LOUDNESS_KEYS,
    ServerSettings,
    load_server_settings,
)
from kidsplay_server.themes import list_themes, theme_label

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"])


def error_messages_context(request: Request) -> dict[str, object]:
    """Template context processor: the translated API error messages.

    Runs after ``i18n_template_context``, which picks the request's language.

    Args:
        request: The request being rendered (unused).

    Returns:
        ``error_messages`` for ``apiDetail()`` in ``base.html``.
    """
    del request
    return {"error_messages": error_messages()}


def source_context(request: Request) -> dict[str, object]:
    """Template context processor: the source-code URL for the footer link.

    Args:
        request: The request being rendered (unused).

    Returns:
        ``source_url`` (AGPL-3.0 section 13; see ``config.source_url``).
    """
    del request
    return {"source_url": source_url()}


_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(
    directory=str(_TEMPLATES_DIR),
    context_processors=[
        auth_template_context,
        i18n_template_context,
        error_messages_context,
        source_context,
    ],
)
install_jinja_i18n(templates.env)

# Display labels for enum values. Templates show these, never the raw value;
# raw values stay in ``data-*`` attributes and JS logic. Every enum member is
# listed so the extractor sees each string (a dynamic ``{{ item.status }}``
# would be invisible to it).
_MEDIA_TYPE_LABELS: dict[MediaType, str] = {
    MediaType.MUSIC: N_("Music"),
    MediaType.AUDIOBOOK: N_("Audiobook"),
    MediaType.PHOTO: N_("Photo"),
}

_PROCESSING_STATUS_LABELS: dict[ProcessingStatus, str] = {
    ProcessingStatus.PENDING: N_("Pending"),
    ProcessingStatus.PROCESSING: N_("Processing"),
    ProcessingStatus.READY: N_("Ready"),
    ProcessingStatus.FAILED: N_("Failed"),
}


def media_type_label(value: str) -> str:
    """Translated label for a ``MediaType`` value (unknown values pass through)."""
    try:
        return gettext_now(_MEDIA_TYPE_LABELS[MediaType(value)])
    except (ValueError, KeyError):
        return str(value)


def processing_status_label(value: str) -> str:
    """Translated label for a ``ProcessingStatus`` value (unknown pass through)."""
    try:
        return gettext_now(_PROCESSING_STATUS_LABELS[ProcessingStatus(value)])
    except (ValueError, KeyError):
        return str(value)


# API ``error_code`` -> translated message for the web UI's toasts. Only codes
# whose English ``detail`` is a fixed sentence are listed: for the others (a
# plugin name, an extraction reason, a validation message) the detail carries
# information a canned sentence would lose, so the page shows it as-is.
_ERROR_CODE_MESSAGES: dict[str, str] = {
    "CSRF_FAILED": N_("The request was refused. Reload the page and try again."),
    "UNAUTHORIZED": N_(
        "Not signed in, or the password was wrong. Sign in and try again."
    ),
    "FORBIDDEN": N_("This token is not allowed to do that."),
    "RATE_LIMITED": N_("Too many attempts. Try again later."),
    "SETUP_REQUIRED": N_("No admin password is set yet. Finish setup first."),
    "AUTH_DISABLED": N_("Admin sign-in is turned off, so there is no password."),
    "WRONG_PASSWORD": N_("The current password is incorrect."),
    "NOT_FOUND": N_("That item was not found. It may have been removed."),
    "NO_IMPORTER": N_("No installed importer can handle that address."),
    "PREVIEW_UNSUPPORTED": N_("No installed importer can preview that address."),
    "UNSUPPORTED_ARCHIVE": N_(
        "Unsupported file type. Upload a .zip, .tar.gz, .tgz, .tar.bz2, .tar.xz "
        "or .tar file."
    ),
    "INVALID_STATE": N_(
        "That item can't be retried right now. Only failed or cancelled items can."
    ),
    "THEME_BUILTIN": N_("Built-in themes can't be changed or deleted."),
    "INVALID_CODE": N_("That pairing code is not valid."),
    "PAIRING_NOT_FOUND": N_("That pairing request was not found."),
    "PAIRING_EXPIRED": N_("That pairing code has expired."),
    "PAIRING_USED": N_("That pairing code was already used."),
    "PAIRING_DENIED": N_("That pairing was declined."),
    "PAIRING_DISABLED": N_("Pairing is turned off on this server."),
    "TOO_MANY_PENDING": N_("Too many pairings are waiting. Try again later."),
}


def error_messages() -> dict[str, str]:
    """Translated messages for the API error codes the web UI knows.

    Returns:
        ``error_code`` to message, in the language of the request being served.
        Emitted into every page for ``apiDetail()`` in ``base.html``.
    """
    return {
        code: gettext_now(message) for code, message in _ERROR_CODE_MESSAGES.items()
    }


templates.env.filters["media_type_label"] = media_type_label
templates.env.filters["processing_status_label"] = processing_status_label


# ---------------------------------------------------------------------------
# Internal data helpers
# ---------------------------------------------------------------------------


@dataclass
class MediaRow:
    """Enriched media item for template rendering."""

    id: uuid.UUID
    media_type: str
    title: str
    playlist_title: str
    processing_status: str
    artist: str
    thumbnail_hash: str | None
    large_thumbnail_hash: str | None
    audio_hash: str | None
    profiles: list[str] = field(default_factory=list)
    profile_ids: list[str] = field(default_factory=list)
    loudness: str = ""
    normalizing: bool = False


def loudness_label(
    media_type: str,
    source_lufs: float | None,
    gain_db: float | None,
    target_lufs: float | None,
    mode: str | None = None,
    normalizing: bool = False,
) -> str:
    """Describe an item's loudness normalization for the media details.

    Args:
        media_type: The item's media type.
        source_lufs: Measured loudness of the original, if any.
        gain_db: Gain applied, or ``None`` if the audio was kept unchanged.
        target_lufs: Target processed for, or ``None`` if never normalized.
        mode: How the gain was applied: ``linear``, ``dynamic`` (peaks were
            limited) or ``capped`` (gain held below the target so as not to
            limit). ``None`` for items normalized before it was recorded.
        normalizing: Whether a normalization job for the item is waiting or
            running.

    Returns:
        A short human-readable label; empty for photos.
    """
    if media_type == MediaType.PHOTO:
        return ""
    if normalizing:
        return gettext_now("Loudness: normalizing…")
    if target_lufs is None:
        return gettext_now("Loudness: not normalized")
    if gain_db is None or source_lufs is None:
        return gettext_now("Loudness: too quiet to measure, kept unchanged")
    if mode == "dynamic":
        return gettext_now(
            "Loudness: {source:.1f} LUFS → {target:g} LUFS "
            "({gain:+.1f} dB, loud peaks limited)",
            source=source_lufs,
            target=target_lufs,
            gain=gain_db,
        )
    if mode == "capped":
        return gettext_now(
            "Loudness: {source:.1f} LUFS → {output:.1f} LUFS "
            "({gain:+.1f} dB, kept below the {target:g} LUFS target to avoid limiting)",
            source=source_lufs,
            output=source_lufs + gain_db,
            gain=gain_db,
            target=target_lufs,
        )
    return gettext_now(
        "Loudness: {source:.1f} LUFS → {target:g} LUFS ({gain:+.1f} dB)",
        source=source_lufs,
        target=target_lufs,
        gain=gain_db,
    )


async def _count(
    db: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()
) -> int:
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _fetch_all(
    db: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()
) -> list[aiosqlite.Row]:
    async with db.execute(sql, params) as cur:
        return list(await cur.fetchall())


async def _list_profiles(db: aiosqlite.Connection) -> list[dict[str, str]]:
    rows = await _fetch_all(db, "SELECT id, name FROM profiles ORDER BY name")
    return [{"id": r[0], "name": r[1]} for r in rows]


async def _list_devices(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    rows = await _fetch_all(
        db,
        """
        SELECT d.id, d.name, d.api_key, d.last_sync_at, p.name AS profile_name
        FROM devices d
        LEFT JOIN profiles p ON p.id = d.profile_id
        ORDER BY d.name
        """,
    )
    return [
        {
            "id": r[0],
            "name": r[1],
            "api_key": r[2],
            "last_sync_at": r[3],
            "profile_name": r[4] or "",
        }
        for r in rows
    ]


async def _list_media_rows(
    db: aiosqlite.Connection,
    *,
    media_type: str | None = None,
    profile_id: str | None = None,
    q: str | None = None,
    limit: int = 10_000,
    offset: int = 0,
) -> list[MediaRow]:
    """Fetch enriched media rows for the browser page."""
    conditions: list[str] = []
    params: list[Any] = []

    if media_type:
        conditions.append("m.media_type = ?")
        params.append(media_type)
    if profile_id:
        conditions.append(
            "EXISTS (SELECT 1 FROM profile_media pm "
            "WHERE pm.media_id = m.id AND pm.profile_id = ?)"
        )
        params.append(profile_id)
    if q:
        conditions.append(
            "(m.title LIKE ? OR m.artist LIKE ? OR m.playlist_title LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like])

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT m.id, m.media_type, m.title, m.playlist_title,
               m.processing_status, COALESCE(m.artist, '') AS artist,
               m.loudness_source_lufs, m.loudness_gain_db, m.loudness_target_lufs,
               m.loudness_mode
        FROM media_items m
        {where}
        ORDER BY m.playlist_title, m.title
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    rows = await _fetch_all(db, sql, tuple(params))
    if not rows:
        return []

    media_ids = [str(r[0]) for r in rows]
    placeholders = ",".join("?" * len(media_ids))

    # Thumbnail hashes (small + large) and audio hash in one query.
    thumb_rows = await _fetch_all(
        db,
        f"SELECT media_id, content_hash, file_type FROM processed_files "
        f"WHERE media_id IN ({placeholders}) "
        f"AND file_type IN ('thumbnail_small', 'thumbnail_large', 'audio')",
        tuple(media_ids),
    )
    thumb_map: dict[str, str] = {}
    large_thumb_map: dict[str, str] = {}
    audio_map: dict[str, str] = {}
    for r in thumb_rows:
        mid = str(r[0])
        if r[2] == "thumbnail_small":
            thumb_map[mid] = r[1]
        elif r[2] == "thumbnail_large":
            large_thumb_map[mid] = r[1]
        elif r[2] == "audio":
            audio_map[mid] = r[1]

    # Assigned profile names and IDs in one query.
    profile_rows = await _fetch_all(
        db,
        f"SELECT pm.media_id, pm.profile_id, p.name FROM profile_media pm "
        f"JOIN profiles p ON p.id = pm.profile_id "
        f"WHERE pm.media_id IN ({placeholders}) "
        f"ORDER BY p.name, p.id",
        tuple(media_ids),
    )
    profile_map: dict[str, list[str]] = {}
    profile_id_map: dict[str, list[str]] = {}
    for mid, pid, pname in profile_rows:
        profile_map.setdefault(str(mid), []).append(pname)
        profile_id_map.setdefault(str(mid), []).append(str(pid))

    normalizing = await list_normalizing_media_ids(db)

    return [
        MediaRow(
            id=r[0],
            media_type=r[1],
            title=r[2],
            playlist_title=r[3] or "",
            processing_status=r[4],
            artist=r[5],
            thumbnail_hash=thumb_map.get(str(r[0])),
            large_thumbnail_hash=large_thumb_map.get(str(r[0])),
            audio_hash=audio_map.get(str(r[0])),
            profiles=profile_map.get(str(r[0]), []),
            profile_ids=profile_id_map.get(str(r[0]), []),
            loudness=loudness_label(
                r[1], r[6], r[7], r[8], r[9], uuid.UUID(str(r[0])) in normalizing
            ),
            normalizing=uuid.UUID(str(r[0])) in normalizing and r[1] != MediaType.PHOTO,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request, db: DBConn) -> HTMLResponse:
    """Render the dashboard with aggregate counts.

    Args:
        request: FastAPI request (required by Jinja2Templates).
        db: Database connection (injected).

    Returns:
        Rendered ``dashboard.html``.
    """
    music_count = await _count(
        db,
        "SELECT COUNT(*) FROM media_items WHERE media_type = ?",
        (MediaType.MUSIC,),
    )
    audiobook_count = await _count(
        db,
        "SELECT COUNT(*) FROM media_items WHERE media_type = ?",
        (MediaType.AUDIOBOOK,),
    )
    photo_count = await _count(
        db,
        "SELECT COUNT(*) FROM media_items WHERE media_type = ?",
        (MediaType.PHOTO,),
    )
    profile_count = await _count(db, "SELECT COUNT(*) FROM profiles")
    device_count = await _count(db, "SELECT COUNT(*) FROM devices")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_page": "dashboard",
            "music_count": music_count,
            "audiobook_count": audiobook_count,
            "photo_count": photo_count,
            "profile_count": profile_count,
            "device_count": device_count,
        },
    )


@router.get("/media", response_class=HTMLResponse, include_in_schema=False)
async def media_browser(
    request: Request,
    db: DBConn,
    media_type: str | None = None,
    profile_id: str | None = None,
    q: str | None = None,
    group_by: str = "playlist",
) -> HTMLResponse:
    """Render the media browser with optional filters.

    Args:
        request: FastAPI request.
        db: Database connection (injected).
        media_type: Filter by media type (music/audiobook/photo).
        profile_id: Filter to items assigned to this profile UUID.
        q: Substring search across title, artist, playlist_title.
        group_by: Grouping mode — ``playlist`` (default), ``artist``, or
            ``none`` for a flat list.

    Returns:
        Rendered ``media.html``.
    """
    if group_by not in ("playlist", "artist", "none"):
        group_by = "playlist"

    media_rows = await _list_media_rows(
        db,
        media_type=media_type or None,
        profile_id=profile_id or None,
        q=q or None,
    )
    profiles = await _list_profiles(db)
    distinct_artists = await list_distinct_artists(db)
    distinct_playlists = await list_distinct_playlist_titles(db)

    return templates.TemplateResponse(
        request,
        "media.html",
        {
            "active_page": "media",
            "media_items": media_rows,
            "profiles": profiles,
            "media_type": media_type or "",
            "profile_id": profile_id or "",
            "q": q or "",
            "group_by": group_by,
            "distinct_artists": distinct_artists,
            "distinct_playlists": distinct_playlists,
        },
    )


@router.get("/media/import", response_class=HTMLResponse, include_in_schema=False)
async def media_import_page(
    request: Request, db: DBConn, importers: Importers
) -> HTMLResponse:
    """Render the import page (importer previews, photo crop, bulk filesystem).

    The preview-and-queue tab appears only when an installed importer supports
    previews (e.g. YouTube via the yt-dlp plugin), labelled with its name.

    Args:
        request: FastAPI request.
        db: Database connection (injected).
        importers: Installed importers (injected).

    Returns:
        Rendered ``media_import.html``.
    """
    profiles = await _list_profiles(db)
    distinct_playlists = await list_distinct_playlist_titles(db)
    preview_label = " / ".join(
        i.label for i in importers if isinstance(i, SupportsPreview)
    )
    return templates.TemplateResponse(
        request,
        "media_import.html",
        {
            "active_page": "media",
            "profiles": profiles,
            "distinct_playlists": distinct_playlists,
            "preview_label": preview_label,
        },
    )


@router.get("/queue", response_class=HTMLResponse, include_in_schema=False)
async def queue_page(request: Request, db: DBConn) -> HTMLResponse:
    """Render the import queue page with status, logs, and actions.

    Args:
        request: FastAPI request.
        db: Database connection (injected).

    Returns:
        Rendered ``queue.html``.
    """
    return templates.TemplateResponse(
        request,
        "queue.html",
        {"active_page": "queue"},
    )


@router.get("/profiles", response_class=HTMLResponse, include_in_schema=False)
async def profiles_page(request: Request, db: DBConn) -> HTMLResponse:
    """Render the profiles management page.

    Args:
        request: FastAPI request.
        db: Database connection (injected).

    Returns:
        Rendered ``profiles.html``.
    """
    profiles = await _list_profiles(db)
    return templates.TemplateResponse(
        request,
        "profiles.html",
        {"active_page": "profiles", "profiles": profiles},
    )


_WEEKDAY_LABELS: dict[Weekday, str] = {
    Weekday.MONDAY: N_("Monday"),
    Weekday.TUESDAY: N_("Tuesday"),
    Weekday.WEDNESDAY: N_("Wednesday"),
    Weekday.THURSDAY: N_("Thursday"),
    Weekday.FRIDAY: N_("Friday"),
    Weekday.SATURDAY: N_("Saturday"),
    Weekday.SUNDAY: N_("Sunday"),
}

_MODE_LABELS: dict[BedtimeMode, str] = {
    BedtimeMode.OFF: N_("Off — no restrictions"),
    BedtimeMode.AUDIOBOOKS_ONLY: N_("Audiobooks only"),
    BedtimeMode.SLEEP_SCREEN: N_("Sleep screen — no playback"),
}


@router.get(
    "/profiles/{profile_id}/settings",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def profile_settings_page(
    request: Request, profile_id: uuid.UUID, db: DBConn
) -> HTMLResponse:
    """Render the per-profile settings form.

    Args:
        request: FastAPI request.
        profile_id: UUID of the profile.
        db: Database connection (injected).

    Returns:
        Rendered ``profile_settings.html``.

    Raises:
        HTTPException: 404 if the profile does not exist.
    """
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    settings = await get_profile_settings(db, profile_id)
    return templates.TemplateResponse(
        request,
        "profile_settings.html",
        {
            "active_page": "profiles",
            "profile": profile,
            "settings": settings,
            "weekdays": [
                (d, gettext_now(label)) for d, label in _WEEKDAY_LABELS.items()
            ],
            "modes": [(m, gettext_now(label)) for m, label in _MODE_LABELS.items()],
            # What the child's device shows; a profile that never chose one
            # shows Spanish (ProfileSettings.language).
            "themes": [(t.id, theme_label(t)) for t in await list_themes(db)],
            "device_languages": list(LANGUAGE_NAMES.items()),
            "device_language": normalize_language(settings.language)
            or (
                LEGACY_DEVICE_LANGUAGE
                if settings.language is None
                else DEFAULT_LANGUAGE
            ),
        },
    )


@dataclass
class _SettingField:
    """One server setting as shown on the settings page."""

    key: str
    label: str
    help: str
    value: int | float | bool | None
    default: int | float | bool | None
    shown: str
    default_shown: str
    kind: str
    min: float
    max: float
    env_var: str
    locked: bool
    saved: bool


_SERVER_SETTING_LABELS: dict[str, str] = {
    "sync_interval_seconds": N_("Device sync interval (seconds)"),
    "webp_quality": N_("Thumbnail and photo quality (WebP, 1-100)"),
    "pairing_enabled": N_("Allow pairing new devices"),
    "loudness_target_lufs": N_("Loudness target (LUFS)"),
    "loudness_target_lufs_music": N_("Loudness target for music (LUFS)"),
    "loudness_target_lufs_audiobook": N_("Loudness target for audiobooks (LUFS)"),
}


def _shown(value: int | float | bool | None) -> str:
    """Render a setting value for a form input (empty for ``None``)."""
    if value is None or isinstance(value, bool):
        return ""
    return f"{value:g}"


def _setting_kind(annotation: object) -> str:
    """The form input kind (``bool``, ``float`` or ``int``) for a field type.

    Args:
        annotation: A ``ServerSettings`` field's type annotation, possibly
            optional (``float | None``).

    Returns:
        ``"bool"``, ``"float"`` or ``"int"``.
    """
    types = set(get_args(annotation)) or {annotation}
    if bool in types:
        return "bool"
    return "float" if float in types else "int"


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request, db: DBConn) -> HTMLResponse:
    """Render the server settings page.

    Args:
        request: FastAPI request.
        db: Database connection (injected).

    Returns:
        Rendered ``settings.html``.
    """
    resolved = await load_server_settings(db)
    fields: list[_SettingField] = []
    for key, info in ServerSettings.model_fields.items():
        bounds = {
            name: getattr(meta, name)
            for meta in info.metadata
            for name in ("ge", "le")
            if hasattr(meta, name)
        }
        value = getattr(resolved.values, key)
        fields.append(
            _SettingField(
                key=key,
                label=gettext_now(_SERVER_SETTING_LABELS.get(key, key)),
                help=gettext_now(info.description) if info.description else "",
                value=value,
                default=info.default,
                shown=_shown(value),
                default_shown=_shown(info.default),
                kind=_setting_kind(info.annotation),
                min=bounds.get("ge", 0),
                max=bounds.get("le", 0),
                env_var=ENV_VARS[key],
                locked=key in resolved.env_locked,
                saved=key in resolved.saved,
            )
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active_page": "settings",
            "fields": fields,
            "loudness_keys": sorted(LOUDNESS_KEYS),
        },
    )


@router.get("/devices", response_class=HTMLResponse, include_in_schema=False)
async def devices_page(
    request: Request, db: DBConn, pair: str | None = None
) -> HTMLResponse:
    """Render the devices management page.

    Args:
        request: FastAPI request.
        db: Database connection (injected).
        pair: Pairing code from a handheld's QR code, to prefill the form.

    Returns:
        Rendered ``devices.html``.
    """
    profiles = await _list_profiles(db)
    devices = await _list_devices(db)
    code = pairing.normalize_code(pair or "")
    pending = await pairing.list_pending(db, pairing.utcnow())
    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "active_page": "devices",
            "devices": devices,
            "profiles": profiles,
            "pairing_enabled": (await load_server_settings(db)).values.pairing_enabled,
            "pairing_waiting": len(pending),
            "pair_request": next((r for r in pending if r.code == code), None),
            "pair_code": pairing.format_code(code) if code else "",
            "server_id": short_server_id(await get_or_create_server_id(db)),
        },
    )


# ---------------------------------------------------------------------------
# Utility routes
# ---------------------------------------------------------------------------


@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def logs_page(request: Request) -> HTMLResponse:
    """Render the real-time log viewer page.

    Pre-populates the page with buffered recent log entries so the viewer
    is not blank on load.  New entries stream in via the SSE endpoint.

    Args:
        request: FastAPI request.

    Returns:
        Rendered ``logs.html``.
    """
    broadcaster = get_broadcaster()
    recent = [
        {"ts": e.ts, "level": e.level, "logger": e.logger, "message": e.message}
        for e in broadcaster.get_recent()
    ]
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"active_page": "logs", "recent_logs": recent},
    )


LOG_STREAM_RECHECK_SECONDS = 5.0
"""How often an open log stream re-checks that its login still holds."""

LOG_STREAM_KEEPALIVE_SECONDS = 20.0
"""Idle time after which a keepalive comment keeps proxies from closing it."""


async def stream_log_events(
    request: Request,
    broadcaster: LogBroadcaster,
    *,
    recheck_seconds: float = LOG_STREAM_RECHECK_SECONDS,
    keepalive_seconds: float = LOG_STREAM_KEEPALIVE_SECONDS,
) -> collections.abc.AsyncGenerator[str, None]:
    """Yield server-sent events for log entries until the client goes away.

    The stream was authenticated once, when it opened. Every
    ``recheck_seconds`` it checks again, so an open tab stops receiving logs
    soon after the admin logs out, changes the password, revokes the token or
    the session expires. Then it sends an ``auth-expired`` event and ends.

    Args:
        request: The request that opened the stream.
        broadcaster: Where log entries come from.
        recheck_seconds: Seconds between authentication re-checks.
        keepalive_seconds: Seconds of silence before a keepalive comment.

    Yields:
        SSE frames.
    """
    q: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=200)
    broadcaster.subscribe(q)
    loop = asyncio.get_running_loop()
    last_check = last_sent = loop.time()
    try:
        while True:
            if await request.is_disconnected():
                break
            timeout = min(recheck_seconds, keepalive_seconds)
            try:
                entry = await asyncio.wait_for(q.get(), timeout=timeout)
            except TimeoutError:
                entry = None
            now = loop.time()
            if now - last_check >= recheck_seconds:
                last_check = now
                if not await admin_still_authenticated(request):
                    yield "event: auth-expired\ndata: {}\n\n"
                    break
            if entry is not None:
                last_sent = now
                yield f"data: {entry.to_sse_data()}\n\n"
            elif now - last_sent >= keepalive_seconds:
                last_sent = now
                yield ": keepalive\n\n"
    finally:
        broadcaster.unsubscribe(q)


@router.get("/logs/stream", include_in_schema=False)
async def logs_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of log entries.

    Each event is a JSON object with keys ``ts``, ``level``, ``logger``,
    and ``message``.  A keepalive comment is sent every 20 seconds to
    prevent proxies from closing idle connections.  Authentication is
    re-checked every few seconds (see ``stream_log_events``).

    Args:
        request: FastAPI request (used to detect client disconnect).

    Returns:
        ``StreamingResponse`` with ``text/event-stream`` media type.
    """
    return StreamingResponse(
        stream_log_events(request, get_broadcaster()),
        media_type="text/event-stream",
    )


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request) -> HTMLResponse:
    """Render the admin account page: change password, log out everywhere.

    Args:
        request: FastAPI request.

    Returns:
        Rendered ``account.html``.
    """
    return templates.TemplateResponse(
        request,
        "account.html",
        {"active_page": "account", "min_password_length": MIN_PASSWORD_LENGTH},
    )


@router.get(
    "/web/media-file/{content_hash}",
    include_in_schema=False,
    response_class=Response,
)
async def web_media_file(
    content_hash: str,
    db: DBConn,
    store: Store,
) -> Response:
    """Serve a processed file to the browser for the admin UI.

    The device-facing ``/api/v1/sync/file/{content_hash}`` endpoint requires a
    device Bearer token, which a browser ``<img>``/``<audio>`` tag cannot send.
    This route serves the same bytes at the web UI's trust level (admin
    session, like every other page here).

    Args:
        content_hash: SHA-256 hex digest of the processed file.
        db: Database connection (injected).
        store: Media store (injected).

    Returns:
        Raw file bytes with ``Content-Type`` set from the stored MIME type.

    Raises:
        HTTPException: 404 if the hash is unknown or the file is missing.
    """
    return await build_file_response(db, store, content_hash)


@router.post(
    "/web/photo-proxy",
    include_in_schema=False,
    response_class=Response,
)
async def photo_proxy(request: Request) -> Response:
    """Download an image URL server-side and return the bytes.

    Used by the photo import tab: the browser cannot load arbitrary
    cross-origin images into Cropper.js, so this endpoint proxies the
    download.

    The URL is supplied as JSON body ``{"url": "https://..."}`` or as
    form data ``url=...``.

    Args:
        request: FastAPI request (body parsed manually to support both
            JSON and form data).

    Returns:
        Raw image bytes with the Content-Type from the upstream server.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        url: str = body.get("url", "")
    else:
        form = await request.form()
        url = str(form.get("url", ""))

    if not url:
        return Response(content="url required", status_code=400)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return Response(
                    content=f"upstream {resp.status_code}",
                    status_code=502,
                )
            upstream_ct = resp.headers.get("content-type", "image/jpeg")
            return Response(content=resp.content, media_type=upstream_ct)
    except httpx.HTTPError as exc:
        return Response(content=str(exc), status_code=502)
