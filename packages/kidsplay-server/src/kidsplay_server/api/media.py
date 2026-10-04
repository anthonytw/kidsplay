"""Media CRUD, ingest, and assignment endpoints.

Routes
------
POST   /media/ingest              — ingest file or directory
GET    /media                     — list with optional filters
GET    /media/{media_id}          — fetch single item
DELETE /media/{media_id}          — delete item and all processed files
POST   /media/{media_id}/assign   — assign to profiles
DELETE /media/{media_id}/assign/{profile_id}  — remove one assignment
GET    /media/{media_id}/files    — list processed files
POST   /media/assign-batch        — bulk assign media to a profile
POST   /media/normalize           — start the loudness backfill
GET    /media/normalize           — loudness backfill progress
"""

import logging
import tarfile
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from kidsplay_models import (
    IngestBatchResult,
    IngestResult,
    MediaItem,
    NormalizeRequest,
    NormalizeStatus,
    ProcessedFile,
    ProfileMediaAssignment,
)
from kidsplay_models.media import MediaType
from kidsplay_models.processing import IngestRequest, ProcessingStatus
from kidsplay_server.database import (
    assign_media_to_profile,
    delete_media_item,
    get_media_item,
    list_media_items,
    list_processed_files,
    unassign_media_from_profile,
    update_media_item_metadata,
)
from kidsplay_server.importers import ImportPreview, PreviewError, SupportsPreview
from kidsplay_server.processing.pipeline import (
    ingest_directory,
    ingest_file,
    ingest_url_batch,
)

from .deps import Backfill, DBConn, DBPath, Importers, Store

router = APIRouter(tags=["media"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response helpers
# ---------------------------------------------------------------------------


def _not_found(resource: str = "Media item") -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"detail": f"{resource} not found", "error_code": "NOT_FOUND"},
    )


def _single_result_to_batch(result: IngestResult) -> IngestBatchResult:
    """Wrap a single ``IngestResult`` in an ``IngestBatchResult``."""
    skipped = 1 if result.skipped else 0
    failed = (
        1
        if not result.skipped and result.processing_status == ProcessingStatus.FAILED
        else 0
    )
    successful = 1 if not result.skipped and not failed else 0
    return IngestBatchResult(
        total_files=1,
        successful=successful,
        failed=failed,
        skipped=skipped,
        results=[result],
    )


class AssignRequest(BaseModel):
    """Request body for POST /media/{id}/assign."""

    profile_ids: list[uuid.UUID]


class AssignBatchRequest(BaseModel):
    """Request body for POST /media/assign-batch."""

    profile_id: uuid.UUID
    media_ids: list[uuid.UUID]


class AssignBatchResult(BaseModel):
    """Response for POST /media/assign-batch."""

    assigned: int
    already_assigned: int
    errors: list[str] = []


class UnassignBatchRequest(BaseModel):
    """Request body for POST /media/unassign-batch."""

    profile_id: uuid.UUID
    media_ids: list[uuid.UUID]


class UnassignBatchResult(BaseModel):
    """Response for POST /media/unassign-batch."""

    unassigned: int
    errors: list[str] = []


class UpdateBatchRequest(BaseModel):
    """Request body for POST /media/update-batch.

    Only fields present in the request body are updated.  Use
    ``model_fields_set`` to distinguish "not provided" from ``None``
    (which clears the field).
    """

    media_ids: list[uuid.UUID]
    artist: str | None = None
    playlist_title: str | None = None


class UpdateBatchResult(BaseModel):
    """Response for POST /media/update-batch."""

    updated: int
    errors: list[str] = []


# Default number of tracks a playlist preview fetches. Caps how many entries
# a large (or unbounded) list dumps into the UI at once; the operator can raise
# it per-request.
DEFAULT_PLAYLIST_PREVIEW_LIMIT = 50


class PreviewRequest(BaseModel):
    """Request body for POST /media/preview."""

    url: str
    max_items: int | None = None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@router.post("/media/ingest", response_model=IngestBatchResult)
async def ingest_media(
    req: IngestRequest,
    db_path: DBPath,
    store: Store,
    importers: Importers,
) -> IngestBatchResult:
    """Ingest media from a server-side path or a URL.

    When ``source_url`` is supplied, it is fetched by the first installed
    importer that can handle it (plain HTTP download is built in; plugins
    add sources such as YouTube).
    When ``source_path`` is supplied it can be a file or a directory;
    directories are walked recursively.

    Args:
        req: Ingest parameters — exactly one of ``source_path`` or
            ``source_url``, plus media type, playlist title, and optional
            profile assignment list.
        db_path: Path to the database (injected from app state).
        store: Content-addressed media store (injected).
        importers: Installed importers (injected).

    Returns:
        Batch result with per-file status and aggregate counts.
    """
    req_crop: dict[str, float] | None = None
    if (
        req.crop_x is not None
        and req.crop_y is not None
        and req.crop_width is not None
        and req.crop_height is not None
    ):
        req_crop = {
            "x": req.crop_x,
            "y": req.crop_y,
            "width": req.crop_width,
            "height": req.crop_height,
        }

    if req.source_url is not None:
        logger.info(
            "Ingest URL: %s type=%s playlist=%r",
            req.source_url,
            req.media_type,
            req.playlist_title,
        )
        url_batch = await ingest_url_batch(
            req.source_url,
            req.media_type,
            db_path,
            store,
            playlist_title=req.playlist_title,
            profile_ids=req.profile_ids or None,
            crop=req_crop,
            title_override=req.title_override or None,
            artist_override=req.artist_override or None,
            registry=importers,
        )
        logger.info(
            "Ingest URL done: ok=%d skip=%d fail=%d",
            url_batch.successful,
            url_batch.skipped,
            url_batch.failed,
        )
        return url_batch

    # source_path is guaranteed non-None by the model validator.
    assert req.source_path is not None
    source = Path(req.source_path)

    if source.is_dir():
        logger.info("Ingest directory: %s type=%s", source, req.media_type)
        batch = await ingest_directory(
            source,
            req.media_type,
            db_path,
            store,
            playlist_title=req.playlist_title,
            profile_ids=req.profile_ids or None,
        )
        logger.info(
            "Ingest directory done: ok=%d skip=%d fail=%d",
            batch.successful,
            batch.skipped,
            batch.failed,
        )
        return batch

    if source.is_file():
        logger.info("Ingest file: %s type=%s", source.name, req.media_type)
        result = await ingest_file(
            source,
            req.media_type,
            db_path,
            store,
            playlist_title=req.playlist_title,
            profile_ids=req.profile_ids or None,
            crop=req_crop,
            title_override=req.title_override or None,
            artist_override=req.artist_override or None,
        )
        logger.info(
            "Ingest file done: status=%s title=%r",
            result.processing_status,
            result.title,
        )
        return _single_result_to_batch(result)

    raise HTTPException(
        status_code=404,
        detail={
            "detail": f"Path not found: {req.source_path}",
            "error_code": "PATH_NOT_FOUND",
        },
    )


@router.post("/media/upload", response_model=IngestBatchResult)
async def upload_media(
    db_path: DBPath,
    store: Store,
    file: UploadFile,
    media_type: Annotated[MediaType, Form()],
    playlist_title: Annotated[str, Form()],
    profile_ids: Annotated[list[uuid.UUID] | None, Form()] = None,
    crop_x: Annotated[float | None, Form()] = None,
    crop_y: Annotated[float | None, Form()] = None,
    crop_width: Annotated[float | None, Form()] = None,
    crop_height: Annotated[float | None, Form()] = None,
    title_override: Annotated[str | None, Form()] = None,
) -> IngestBatchResult:
    """Ingest an uploaded file directly.

    Saves the multipart upload to a temporary file, then passes it through
    the standard ingest pipeline.  The temp file is deleted after ingest.

    For photo uploads, optional crop coordinates (pixel offsets on the
    original image as produced by Cropper.js) can be supplied via
    ``crop_x``, ``crop_y``, ``crop_width``, ``crop_height``.  All four
    must be present for the crop to be applied; partial sets are ignored.

    Args:
        db_path: Path to the database (injected).
        store: Content-addressed media store (injected).
        file: Uploaded file (multipart/form-data).
        media_type: MUSIC, AUDIOBOOK, or PHOTO.
        playlist_title: Playlist/group name for the ingested item.
        profile_ids: Optional list of profile UUIDs to assign after ingest.
        crop_x: Left edge of crop region in source-image pixels.
        crop_y: Top edge of crop region in source-image pixels.
        crop_width: Width of crop region in source-image pixels.
        crop_height: Height of crop region in source-image pixels.

    Returns:
        Batch result wrapping the single-file ingest outcome.
    """
    crop: dict[str, float] | None = None
    if (
        crop_x is not None
        and crop_y is not None
        and crop_width is not None
        and crop_height is not None
    ):
        crop = {"x": crop_x, "y": crop_y, "width": crop_width, "height": crop_height}

    logger.info(
        "Upload ingest: %s type=%s playlist=%r",
        file.filename,
        media_type,
        playlist_title,
    )
    with tempfile.TemporaryDirectory() as tmp:
        filename = file.filename or "upload"
        dest = Path(tmp) / filename
        dest.write_bytes(await file.read())
        result: IngestResult = await ingest_file(
            dest,
            media_type,
            db_path,
            store,
            playlist_title=playlist_title,
            profile_ids=profile_ids or None,
            crop=crop,
            title_override=title_override or None,
        )
    logger.info(
        "Upload ingest done: status=%s title=%r",
        result.processing_status,
        result.title,
    )
    return _single_result_to_batch(result)


def _detect_archive_format(filename: str) -> str | None:
    """Return the archive format string for tarfile/zipfile, or None if unsupported.

    Args:
        filename: Original upload filename.

    Returns:
        ``"zip"``, ``"tar"``, or ``None`` if the extension is not a recognised
        archive format.
    """
    name = filename.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
        return "tar"
    return None


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a ZipFile, rejecting members that escape the destination.

    Args:
        zf: Open ZipFile.
        dest: Absolute extraction root.

    Raises:
        ValueError: If any member path would land outside ``dest``.
    """
    dest_str = str(dest.resolve())
    for member in zf.namelist():
        member_dest = (dest / member).resolve()
        if not str(member_dest).startswith(dest_str):
            raise ValueError(f"Unsafe zip path rejected: {member}")
    zf.extractall(dest)


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract a TarFile using the 'data' filter when available (Python ≥ 3.12).

    Args:
        tf: Open TarFile.
        dest: Absolute extraction root.
    """
    try:
        tf.extractall(dest, filter="data")
    except TypeError:
        tf.extractall(dest)


def _extract_archive(archive_path: Path, dest: Path) -> None:
    """Extract ``archive_path`` into ``dest``, dispatching by extension.

    Args:
        archive_path: Path to the archive file.
        dest: Directory to extract into (must already exist).

    Raises:
        ValueError: If the file extension is not a supported archive format.
    """
    name = archive_path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            _safe_extract_zip(zf, dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
        with tarfile.open(archive_path) as tf:
            _safe_extract_tar(tf, dest)
    else:
        raise ValueError(f"Unsupported archive format: {archive_path.name}")


@router.post("/media/upload-archive", response_model=IngestBatchResult)
async def upload_archive(
    db_path: DBPath,
    store: Store,
    file: UploadFile,
    media_type: Annotated[MediaType, Form()],
    playlist_title: Annotated[str, Form()] = "",
    profile_ids: Annotated[list[uuid.UUID] | None, Form()] = None,
) -> IngestBatchResult:
    """Ingest all media files from an uploaded archive (zip / tar).

    Supported formats: ``.zip``, ``.tar.gz``, ``.tgz``, ``.tar.bz2``,
    ``.tar.xz``, ``.tar``.

    **Directory structure → playlist mapping:**
    Files inside a subdirectory use that directory's name as the
    ``playlist_title``.  Files at the archive root use the ``playlist_title``
    form field (falling back to the archive filename stem).

    Example::

        my_songs.zip
        ├── Favourites/
        │   ├── song_a.mp3   →  playlist "Favourites"
        │   └── song_b.mp3   →  playlist "Favourites"
        └── single_track.mp3 →  playlist from form field

    Args:
        db_path: Path to the database (injected).
        store: Content-addressed media store (injected).
        file: Uploaded archive file (multipart/form-data).
        media_type: MUSIC, AUDIOBOOK, or PHOTO.
        playlist_title: Fallback playlist/group for root-level files.
            Defaults to the archive filename stem.
        profile_ids: Optional list of profile UUIDs to assign after ingest.

    Returns:
        Batch result with per-file status and aggregate counts.

    Raises:
        HTTPException: 422 if the file is not a recognised archive format.
    """
    filename = file.filename or "upload"
    if _detect_archive_format(filename) is None:
        raise HTTPException(
            status_code=422,
            detail={
                "detail": (
                    f"Unsupported file type: {filename!r}. "
                    "Upload a .zip, .tar.gz, .tgz, .tar.bz2, .tar.xz, or .tar file."
                ),
                "error_code": "UNSUPPORTED_ARCHIVE",
            },
        )

    extensions = (
        frozenset({".mp3", ".m4a", ".flac", ".ogg", ".wav", ".aac"})
        if media_type in (MediaType.MUSIC, MediaType.AUDIOBOOK)
        else frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"})
    )

    fallback_playlist = playlist_title.strip() or Path(filename).stem

    logger.info(
        "Archive upload ingest: %s type=%s fallback_playlist=%r",
        filename,
        media_type,
        fallback_playlist,
    )

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / filename
        archive_path.write_bytes(await file.read())

        extract_root = Path(tmp) / "extracted"
        extract_root.mkdir()
        try:
            _extract_archive(archive_path, extract_root)
        except (zipfile.BadZipFile, tarfile.TarError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "detail": f"Failed to extract archive: {exc}",
                    "error_code": "ARCHIVE_EXTRACT_FAILED",
                },
            ) from exc

        files = sorted(
            p
            for p in extract_root.rglob("*")
            if p.is_file() and p.suffix.lower() in extensions
        )

        batch = IngestBatchResult(total_files=len(files))

        for file_path in files:
            rel = file_path.relative_to(extract_root)
            if len(rel.parts) > 1:
                # File is inside a subdirectory — use immediate parent as playlist.
                file_playlist: str = file_path.parent.name
            else:
                file_playlist = fallback_playlist

            result: IngestResult = await ingest_file(
                file_path,
                media_type,
                db_path,
                store,
                playlist_title=file_playlist,
                profile_ids=profile_ids or None,
            )
            batch.results.append(result)
            if result.skipped:
                batch.skipped += 1
            elif result.processing_status == ProcessingStatus.FAILED:
                batch.failed += 1
            else:
                batch.successful += 1

    logger.info(
        "Archive upload done: ok=%d skip=%d fail=%d",
        batch.successful,
        batch.skipped,
        batch.failed,
    )
    return batch


@router.post("/media/preview", response_model=ImportPreview)
async def preview_media(req: PreviewRequest, importers: Importers) -> ImportPreview:
    """List what a URL contains without downloading it.

    Delegates to the first installed importer that can handle the URL, if it
    supports previews (``SupportsPreview``); for YouTube that is the
    ``kidsplay-importer-ytdlp`` plugin. At most ``max_items`` playlist entries
    are returned (default ``DEFAULT_PLAYLIST_PREVIEW_LIMIT``); the response
    reports ``total_available`` and ``truncated`` so the caller can offer to
    fetch more or import a subset.

    Args:
        req: Request containing the URL to preview and an optional
            ``max_items`` cap on the number of playlist entries returned.
        importers: Installed importers (injected).

    Returns:
        ``ImportPreview`` with is_playlist flag and track list.

    Raises:
        HTTPException: 422 ``PREVIEW_UNSUPPORTED`` if no installed importer
            can preview the URL, or ``PREVIEW_FAILED`` if the preview fails.
    """
    importer = importers.resolve(req.url)
    if importer is None or not isinstance(importer, SupportsPreview):
        raise HTTPException(
            status_code=422,
            detail={
                "detail": f"No installed importer can preview {req.url}",
                "error_code": "PREVIEW_UNSUPPORTED",
            },
        )
    limit = (
        req.max_items
        if req.max_items and req.max_items > 0
        else DEFAULT_PLAYLIST_PREVIEW_LIMIT
    )
    try:
        return await importer.preview(req.url, limit)
    except PreviewError as exc:
        detail: dict[str, object] = {
            "detail": exc.detail,
            "error_code": "PREVIEW_FAILED",
        }
        if exc.debug is not None:
            detail["debug"] = exc.debug.model_dump()
        raise HTTPException(status_code=422, detail=detail) from exc


# ---------------------------------------------------------------------------
# Media CRUD
# ---------------------------------------------------------------------------


@router.get("/media", response_model=list[MediaItem])
async def list_media(
    db: DBConn,
    media_type: MediaType | None = None,
    profile_id: uuid.UUID | None = None,
    playlist_title: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[MediaItem]:
    """List media items with optional filtering.

    All filters are ANDed together.  Results are ordered by
    ``playlist_title, title``.

    Args:
        db: Database connection (injected).
        media_type: Restrict to one media type.
        profile_id: Only items assigned to this profile.
        playlist_title: Exact match on playlist title.
        status: Exact match on processing status.
        q: Substring search across title, artist, and playlist_title.
        limit: Maximum results (default 100).
        offset: Pagination offset (default 0).

    Returns:
        List of matching ``MediaItem`` instances.
    """
    return await list_media_items(
        db,
        media_type=media_type,
        profile_id=profile_id,
        playlist_title=playlist_title,
        status=status,
        q=q,
        limit=limit,
        offset=offset,
    )


# Declared before ``/media/{media_id}`` so "normalize" is not read as an ID.
@router.post("/media/normalize", response_model=NormalizeStatus, status_code=202)
async def start_normalize(
    body: NormalizeRequest, backfill: Backfill
) -> NormalizeStatus:
    """Queue loudness normalization of existing audio.

    One job per item goes on the import queue, where the worker runs them one
    at a time beside the imports; the response returns at once. Items already
    normalized to their type's current target are skipped, so running this
    twice re-encodes nothing the second time. Items that already have a
    pending job are not queued again, so this is safe to call while an upload
    is still normalizing.

    Args:
        body: ``{"all": true}`` for the whole library, or ``media_ids``.
        backfill: The app's backfill runner (injected).

    Returns:
        The run's status (202 Accepted).
    """
    return await backfill.start(None if body.all else body.media_ids)


@router.get("/media/normalize", response_model=NormalizeStatus)
async def normalize_status(backfill: Backfill) -> NormalizeStatus:
    """Report the progress of the current or last loudness normalization.

    Covers the jobs of the latest library-wide request and of any upload
    normalizing meanwhile; ``running`` is true while any is unfinished.

    Args:
        backfill: The app's backfill runner (injected).

    Returns:
        Counts so far; all zero if nothing is queued and no request has been
        made since startup.
    """
    return await backfill.status()


@router.get("/media/{media_id}", response_model=MediaItem)
async def get_media(media_id: uuid.UUID, db: DBConn) -> MediaItem:
    """Fetch a single media item by ID.

    Args:
        media_id: UUID of the media item.
        db: Database connection (injected).

    Returns:
        The ``MediaItem``.

    Raises:
        HTTPException: 404 if not found.
    """
    item = await get_media_item(db, media_id)
    if item is None:
        raise _not_found()
    return item


class UpdateMediaRequest(BaseModel):
    """Request body for PATCH /media/{id}."""

    title: str
    artist: str | None = None
    playlist_title: str
    media_type: MediaType | None = None


@router.patch("/media/{media_id}", response_model=MediaItem)
async def update_media(
    media_id: uuid.UUID,
    body: UpdateMediaRequest,
    db: DBConn,
) -> MediaItem:
    """Update the user-visible metadata of a media item.

    Args:
        media_id: UUID of the media item.
        body: New title, artist, and playlist_title values.
        db: Database connection (injected).

    Returns:
        The updated ``MediaItem``.

    Raises:
        HTTPException: 404 if not found.
    """
    item = await get_media_item(db, media_id)
    if item is None:
        raise _not_found()
    logger.debug(
        "Updated metadata for %s: title=%r artist=%r playlist=%r",
        media_id,
        body.title,
        body.artist,
        body.playlist_title,
    )
    now = datetime.now()
    await update_media_item_metadata(
        db,
        media_id,
        title=body.title,
        artist=body.artist,
        playlist_title=body.playlist_title,
        updated_at=now,
        media_type=body.media_type.value if body.media_type is not None else None,
    )
    await db.commit()
    updated = await get_media_item(db, media_id)
    assert updated is not None
    return updated


@router.delete("/media/{media_id}", status_code=204)
async def delete_media(media_id: uuid.UUID, db: DBConn) -> Response:
    """Delete a media item and all its processed files.

    ``delete_media_item`` cascades to ``processed_files`` and
    ``profile_media`` and commits internally.

    Args:
        media_id: UUID of the media item.
        db: Database connection (injected).

    Returns:
        204 No Content.

    Raises:
        HTTPException: 404 if not found.
    """
    item = await get_media_item(db, media_id)
    if item is None:
        raise _not_found()
    logger.info("Deleted media %s: %r (%s)", media_id, item.title, item.media_type)
    await delete_media_item(db, media_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


@router.post(
    "/media/{media_id}/assign",
    response_model=list[ProfileMediaAssignment],
    status_code=200,
)
async def assign_media(
    media_id: uuid.UUID,
    body: AssignRequest,
    db: DBConn,
) -> list[ProfileMediaAssignment]:
    """Assign a media item to one or more profiles.

    Idempotent: re-assigning an already-assigned pair is a no-op.

    Args:
        media_id: UUID of the media item.
        body: List of profile UUIDs to assign to.
        db: Database connection (injected).

    Returns:
        List of ``ProfileMediaAssignment`` records.

    Raises:
        HTTPException: 404 if the media item is not found.
    """
    item = await get_media_item(db, media_id)
    if item is None:
        raise _not_found()

    now = datetime.now()
    assignments: list[ProfileMediaAssignment] = []
    for profile_id in body.profile_ids:
        assignment = await assign_media_to_profile(db, profile_id, media_id, now)
        assignments.append(assignment)
    await db.commit()
    return assignments


@router.delete("/media/{media_id}/assign/{profile_id}", status_code=204)
async def unassign_media(
    media_id: uuid.UUID,
    profile_id: uuid.UUID,
    db: DBConn,
) -> Response:
    """Remove a media-to-profile assignment.

    No-op if the assignment does not exist.

    Args:
        media_id: UUID of the media item.
        profile_id: UUID of the profile.
        db: Database connection (injected).

    Returns:
        204 No Content.
    """
    await unassign_media_from_profile(db, profile_id, media_id)
    await db.commit()
    return Response(status_code=204)


@router.get("/media/{media_id}/files", response_model=list[ProcessedFile])
async def list_files(media_id: uuid.UUID, db: DBConn) -> list[ProcessedFile]:
    """List processed output files for a media item.

    Args:
        media_id: UUID of the media item.
        db: Database connection (injected).

    Returns:
        List of ``ProcessedFile`` instances ordered by ``file_type``.

    Raises:
        HTTPException: 404 if the media item is not found.
    """
    item = await get_media_item(db, media_id)
    if item is None:
        raise _not_found()
    return await list_processed_files(db, media_id)


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------


@router.post("/media/assign-batch", response_model=AssignBatchResult)
async def assign_batch(body: AssignBatchRequest, db: DBConn) -> AssignBatchResult:
    """Assign multiple media items to a single profile in one call.

    Idempotent: items already assigned contribute to ``already_assigned``
    rather than ``assigned``.  Items that do not exist are counted in
    ``errors``.

    Args:
        body: Profile UUID and list of media item UUIDs.
        db: Database connection (injected).

    Returns:
        Counts of newly assigned, already-assigned, and errored items.
    """
    assigned = 0
    already_assigned = 0
    errors: list[str] = []
    now = datetime.now()

    for media_id in body.media_ids:
        item = await get_media_item(db, media_id)
        if item is None:
            errors.append(f"Media item {media_id} not found")
            continue
        # assign_media_to_profile uses INSERT OR IGNORE; check rowcount to
        # distinguish new vs. existing assignment.
        async with db.execute(
            "SELECT 1 FROM profile_media WHERE profile_id = ? AND media_id = ?",
            (str(body.profile_id), str(media_id)),
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            already_assigned += 1
        else:
            await assign_media_to_profile(db, body.profile_id, media_id, now)
            assigned += 1

    await db.commit()
    return AssignBatchResult(
        assigned=assigned,
        already_assigned=already_assigned,
        errors=errors,
    )


@router.post("/media/unassign-batch", response_model=UnassignBatchResult)
async def unassign_batch(body: UnassignBatchRequest, db: DBConn) -> UnassignBatchResult:
    """Remove multiple media items from a single profile in one call.

    No-op for items that are not currently assigned to the profile.
    Items that do not exist are counted in ``errors``.

    Args:
        body: Profile UUID and list of media item UUIDs.
        db: Database connection (injected).

    Returns:
        Counts of unassigned and errored items.
    """
    unassigned = 0
    errors: list[str] = []

    for media_id in body.media_ids:
        item = await get_media_item(db, media_id)
        if item is None:
            errors.append(f"Media item {media_id} not found")
            continue
        await unassign_media_from_profile(db, body.profile_id, media_id)
        unassigned += 1

    await db.commit()
    return UnassignBatchResult(unassigned=unassigned, errors=errors)


@router.post("/media/update-batch", response_model=UpdateBatchResult)
async def update_batch(body: UpdateBatchRequest, db: DBConn) -> UpdateBatchResult:
    """Update artist and/or playlist_title for multiple items at once.

    Only fields explicitly present in the request body are updated.
    Setting ``artist`` to ``null`` clears the artist field.

    Args:
        body: List of media item UUIDs and optional new field values.
        db: Database connection (injected).

    Returns:
        Counts of updated and errored items.
    """
    set_fields = body.model_fields_set - {"media_ids"}
    if not set_fields:
        return UpdateBatchResult(updated=0, errors=[])

    set_clauses: list[str] = ["updated_at = ?"]
    base_values: list[object] = [datetime.now().isoformat()]

    if "artist" in set_fields:
        set_clauses.append("artist = ?")
        base_values.append(body.artist)
    if "playlist_title" in set_fields:
        set_clauses.append("playlist_title = ?")
        base_values.append(body.playlist_title)

    sql = f"UPDATE media_items SET {', '.join(set_clauses)} WHERE id = ?"

    updated = 0
    errors: list[str] = []

    for media_id in body.media_ids:
        item = await get_media_item(db, media_id)
        if item is None:
            errors.append(f"Media item {media_id} not found")
            continue
        await db.execute(sql, (*base_values, str(media_id)))
        updated += 1

    await db.commit()
    return UpdateBatchResult(updated=updated, errors=errors)
