"""Device-facing sync endpoints.

Routes
------
GET /devices/{device_id}/manifest  — full sync manifest (media, files, profile
                                     settings) with ETag support
GET /sync/file/{content_hash}      — download a processed file by hash
"""

import hashlib
import json
import logging
import uuid
from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from kidsplay_models import SERVER_ID_HEADER
from kidsplay_models.sync import SyncFileEntry, SyncManifest, SyncMediaEntry
from kidsplay_server.database import (
    get_or_create_server_id,
    get_processed_file_by_hash,
    get_profile_settings,
    get_theme_asset_file,
    list_media_items,
    list_processed_files,
)
from kidsplay_server.server_settings import load_server_settings
from kidsplay_server.storage import MediaStore
from kidsplay_server.themes import resolve_theme

from .deps import AuthDevice, DBConn, Store

router = APIRouter(tags=["sync"])
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File-type mappings
# ---------------------------------------------------------------------------

# Maps ProcessedFile.file_type → SyncFileEntry.file_type
_SYNC_FILE_TYPE: dict[str, str] = {
    "audio": "audio",
    "thumbnail_small": "thumbnail",
    "thumbnail_medium": "thumbnail",
    "thumbnail_large": "thumbnail",
    "photo_resized": "photo",
}

# Server-only files: the original behind a loudness-normalized "audio" file is
# kept for re-normalization and never sent to devices.
_SERVER_ONLY_FILE_TYPES: frozenset[str] = frozenset({"audio_source"})

# Maps ProcessedFile.file_type → ThumbnailSize.value key for thumbnail_paths
_THUMB_SIZE_FOR_TYPE: dict[str, str] = {
    "thumbnail_small": "60x60",
    "thumbnail_medium": "200x200",
    "thumbnail_large": "480x480",
}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@router.get("/devices/{device_id}/manifest")
async def get_manifest(
    device_id: uuid.UUID,
    db: DBConn,
    store: Store,
    auth_device: AuthDevice,
    if_none_match: Annotated[str | None, Header(alias="if-none-match")] = None,
) -> Response:
    """Build and return the sync manifest for a device.

    The manifest lists every file the device should have and metadata for
    every assigned media item.  A SHA-256 hash of the manifest content is
    returned in the ``ETag`` response header.  If the caller supplies an
    ``If-None-Match`` header matching the current hash, 304 is returned
    with no body.

    Only media items with ``processing_status = 'ready'`` are included.

    Requires ``Authorization: Bearer {api_key}``; a device may only read its
    own manifest.

    Args:
        device_id: UUID of the requesting device.
        db: Database connection (injected).
        store: Media store (injected, unused here but kept for consistency).
        auth_device: Authenticated device resolved from the Bearer token.
        if_none_match: Previous manifest hash for conditional GET support.

    Returns:
        ``JSONResponse`` with the ``SyncManifest`` body and ``ETag`` header,
        or a 304 response if the manifest is unchanged.

    Raises:
        HTTPException: 401 if the token is missing/invalid, 403 if the token
            belongs to a different device.
    """
    if auth_device.id != device_id:
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "Token does not match the requested device",
                "error_code": "FORBIDDEN",
            },
        )
    device = auth_device

    # Load all ready media assigned to the device's profile.
    media_items = await list_media_items(
        db,
        profile_id=device.profile_id,
        status="ready",
        limit=10_000,
    )

    files_seen: set[str] = set()
    file_entries: list[SyncFileEntry] = []
    media_entries: list[SyncMediaEntry] = []

    for item in media_items:
        processed = await list_processed_files(db, item.id)

        audio_path: str | None = None
        photo_path: str | None = None
        thumbnail_paths: dict[str, str] = {}

        for pf in processed:
            if pf.file_type in _SERVER_ONLY_FILE_TYPES:
                continue
            # Deduplicate files by content_hash (same artwork on two albums).
            if pf.content_hash not in files_seen:
                files_seen.add(pf.content_hash)
                file_entries.append(
                    SyncFileEntry(
                        content_hash=pf.content_hash,
                        relative_path=pf.relative_path,
                        size_bytes=pf.size_bytes,
                        file_type=_SYNC_FILE_TYPE.get(pf.file_type, pf.file_type),
                    )
                )

            if pf.file_type == "audio":
                audio_path = pf.relative_path
            elif pf.file_type == "photo_resized":
                photo_path = pf.relative_path
            elif pf.file_type in _THUMB_SIZE_FOR_TYPE:
                thumbnail_paths[_THUMB_SIZE_FOR_TYPE[pf.file_type]] = pf.relative_path

        media_entries.append(
            SyncMediaEntry(
                media_id=item.id,
                media_type=item.media_type,
                playlist_title=item.playlist_title,
                title=item.title,
                artist=item.artist,
                duration_seconds=item.duration_seconds,
                audio_path=audio_path,
                photo_path=photo_path,
                thumbnail_paths=thumbnail_paths,
            )
        )

    profile_settings = await get_profile_settings(db, device.profile_id)

    # The chosen theme travels with its asset files, which are listed in
    # ``files`` like any other synced file. A theme id nothing resolves (a
    # deleted custom theme) sends none, and the device shows the default.
    theme = (
        await resolve_theme(db, profile_settings.theme)
        if profile_settings.theme is not None
        else None
    )
    if theme is not None:
        for asset in theme.assets:
            if asset.content_hash not in files_seen:
                files_seen.add(asset.content_hash)
                file_entries.append(
                    SyncFileEntry(
                        content_hash=asset.content_hash,
                        relative_path=asset.relative_path,
                        size_bytes=asset.size_bytes,
                        file_type="theme",
                    )
                )
    # Only an interval someone chose (saved on the settings page, or pinned by
    # the environment) is sent. Otherwise the device keeps the interval from
    # its own config.json rather than having the server's default override it.
    resolved_settings = await load_server_settings(db)
    interval_chosen = "sync_interval_seconds" in (
        resolved_settings.saved | resolved_settings.env_locked
    )
    sync_interval: int | None = (
        resolved_settings.values.sync_interval_seconds if interval_chosen else None
    )

    # Compute a stable hash from the deterministic content (no generated_at).
    # Settings are part of it, so a settings change alone reaches the device.
    partial: dict = {
        "device_id": str(device_id),
        "profile_id": str(device.profile_id),
        "files": [f.model_dump(mode="json") for f in file_entries],
        "media": [m.model_dump(mode="json") for m in media_entries],
        "profile_settings": profile_settings.model_dump(mode="json"),
        "sync_interval_seconds": sync_interval,
    }
    if theme is not None:
        # Only when there is one, so a profile without a theme keeps the
        # manifest hash it had before themes existed.
        partial["theme"] = theme.model_dump(mode="json")
    raw = json.dumps(partial, sort_keys=True, default=str)
    manifest_hash = hashlib.sha256(raw.encode()).hexdigest()

    # Names this server, so a device can tell it is still talking to the one it
    # paired with (see kidsplay_models.pairing.SERVER_ID_HEADER).
    identity = {SERVER_ID_HEADER: await get_or_create_server_id(db)}

    if if_none_match == manifest_hash:
        logger.debug(
            "Manifest unchanged for device %s (%r) — 304",
            device_id,
            device.name,
        )
        return Response(status_code=304, headers=identity)

    logger.info(
        "Manifest for device %s (%r): %d media items, %d files",
        device_id,
        device.name,
        len(media_entries),
        len(file_entries),
    )

    manifest = SyncManifest(
        device_id=device_id,
        profile_id=device.profile_id,
        manifest_hash=manifest_hash,
        files=file_entries,
        media=media_entries,
        total_size_bytes=sum(f.size_bytes for f in file_entries),
        profile_settings=profile_settings,
        sync_interval_seconds=sync_interval,
        theme=theme,
    )

    return JSONResponse(
        content=manifest.model_dump(mode="json"),
        headers={"ETag": manifest_hash, **identity},
    )


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------


async def build_file_response(
    db: aiosqlite.Connection,
    store: MediaStore,
    content_hash: str,
    *,
    include_theme_assets: bool = False,
) -> Response:
    """Resolve a content hash to a ``FileResponse`` from the media store.

    Shared by the device-facing ``/sync/file/{content_hash}`` endpoint and the
    unauthenticated web-UI route that serves the same bytes to the browser.
    Performs no authorization — callers own that decision.

    Args:
        db: Database connection.
        store: Media store.
        content_hash: SHA-256 hex digest of the processed file.
        include_theme_assets: Also serve theme assets (device sync only; the
            web UI route serves media and nothing else).

    Returns:
        Raw file bytes with ``Content-Type`` set from the stored MIME type.

    Raises:
        HTTPException: 404 if the hash is not in the DB or the file is missing
            from the store.
    """
    pf = await get_processed_file_by_hash(db, content_hash)
    if pf is None and include_theme_assets:
        found = await get_theme_asset_file(db, content_hash)
        if found is not None and store.exists(found[0]):
            return FileResponse(
                path=store.get_absolute_path(found[0]), media_type=found[1]
            )
    if pf is None or not store.exists(pf.relative_path):
        raise HTTPException(
            status_code=404,
            detail={"detail": "File not found", "error_code": "NOT_FOUND"},
        )

    abs_path = store.get_absolute_path(pf.relative_path)
    return FileResponse(path=abs_path, media_type=pf.mime_type)


@router.get("/sync/file/{content_hash}")
async def download_file(
    content_hash: str,
    db: DBConn,
    store: Store,
    auth_device: AuthDevice,
) -> Response:
    """Download a processed file by its content hash.

    Looks up the hash in ``processed_files``, then streams the file from
    the media store with the correct MIME type.

    Requires ``Authorization: Bearer {api_key}`` from a registered device.

    Args:
        content_hash: SHA-256 hex digest of the processed file.
        db: Database connection (injected).
        store: Media store (injected).
        auth_device: Authenticated device resolved from the Bearer token.

    Returns:
        Raw file bytes with ``Content-Type`` set from the stored MIME type.

    Raises:
        HTTPException: 401 if the token is missing/invalid, 404 if the hash
            is not found in the DB or the file is missing from the store.
    """
    del auth_device  # Presence is the authorization check; value unused.
    return await build_file_response(db, store, content_hash, include_theme_assets=True)
