"""Media ingestion pipeline.

Orchestrates the full ingest flow for a single file or a directory:
  1. Compute SHA-256 hash, bail early if already in the library (dedup).
  2. Extract metadata / artwork (audio) or derive from filesystem (photo).
  3. Copy/process files into content-addressed storage. Audio is stored as it
     is, fast, and plays right away; loudness normalization (EBU R128) is
     queued as a background job (see below), so an upload never waits for it.
  4. Write ``media_items`` and ``processed_files`` DB records.
  5. Update processing status to ``ready``.
  6. Optionally assign the new item to one or more profiles.

Duplicate detection (same content_hash already in DB) sets
``IngestResult.skipped = True`` without modifying any state.

Missing artwork is logged at DEBUG level and does not fail the ingest —
the item is created without thumbnails.

Callers own the choice of ``db_path``; the pipeline opens a fresh
connection per ``ingest_file`` call and commits atomically.

``normalize_media_item`` brings one existing item to the current loudness
target. The import queue worker runs it, one job at a time, for the job each
audio ingest queues and for the library backfill (``loudness_backfill``). It
writes the normalized output as a new store file and repoints the item's rows
in one transaction; it never rewrites or deletes a store file.

Typical usage:
    store = MediaStore(Path("/mnt/media"))
    result = await ingest_file(Path("/tmp/song.mp3"), MediaType.MUSIC, db, store)
    batch  = await ingest_directory(Path("/tmp/music"), MediaType.MUSIC, db, store)
"""

import asyncio
import contextlib
import logging
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path

import aiosqlite

from kidsplay_models import IngestBatchResult, IngestResult, MediaItem, ProcessedFile
from kidsplay_models.media import MediaType
from kidsplay_models.processing import ProcessingStatus, ThumbnailSize
from kidsplay_server.database import (
    assign_media_to_profile,
    configure_conn,
    create_loudness_job,
    create_media_item,
    create_processed_file,
    get_media_item,
    get_media_item_by_hash,
    init_db,
    list_processed_files,
    update_media_item_loudness,
    update_media_item_status,
    update_processed_file,
)
from kidsplay_server.importers import (
    FetchContext,
    FetchedItem,
    ImporterRegistry,
    SourceNeedsPluginError,
    get_default_registry,
)
from kidsplay_server.processing import queue_wakeup
from kidsplay_server.processing.audio import (
    NORMALIZED_MIME,
    NORMALIZED_SUFFIX,
    LoudnessConfig,
    LoudnessError,
    LoudnessTarget,
    NormalizationResult,
    UnmeasurableLoudnessError,
    extract_artwork,
    extract_metadata,
    ffmpeg_available,
    normalize_loudness,
)
from kidsplay_server.processing.images import generate_thumbnails, process_photo
from kidsplay_server.processing.resources import kill_jobs
from kidsplay_server.server_settings import load_loudness_config, load_server_settings
from kidsplay_server.storage import MediaStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CANCEL_JOIN_SECONDS = 2.0
"""How long a cancelled normalization waits for its worker thread to finish."""

_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".flac", ".ogg", ".wav", ".aac"}
)
_PHOTO_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
)
MEDIA_EXTENSIONS: frozenset[str] = _AUDIO_EXTENSIONS | _PHOTO_EXTENSIONS
"""File extensions the pipeline can ingest, audio and photo alike."""

_ALL_SIZES: list[ThumbnailSize] = [
    ThumbnailSize.SMALL,
    ThumbnailSize.MEDIUM,
    ThumbnailSize.LARGE,
]

_FILE_TYPE_FOR_SIZE: dict[ThumbnailSize, str] = {
    ThumbnailSize.SMALL: "thumbnail_small",
    ThumbnailSize.MEDIUM: "thumbnail_medium",
    ThumbnailSize.LARGE: "thumbnail_large",
}

_AUDIO_TYPES: frozenset[MediaType] = frozenset({MediaType.MUSIC, MediaType.AUDIOBOOK})

_AUDIO_MIME: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ingest_file(
    file_path: Path,
    media_type: MediaType,
    db_path: Path,
    media_store: MediaStore,
    *,
    playlist_title: str | None = None,
    profile_ids: list[uuid.UUID] | None = None,
    artwork_bytes_override: bytes | None = None,
    crop: dict[str, float] | None = None,
    title_override: str | None = None,
    artist_override: str | None = None,
    loudness: LoudnessConfig | None = None,
) -> IngestResult:
    """Ingest a single media file into the library.

    Opens a fresh DB connection, checks for duplicates, processes the file,
    writes all records, and commits. The connection is closed on return.

    Args:
        file_path: Absolute path to the source file.
        media_type: MUSIC, AUDIOBOOK, or PHOTO — controls the processing
            branch taken.
        db_path: Path to the SQLite database file.
        media_store: Initialised MediaStore for content-addressed storage.
        playlist_title: Optional playlist/group override. When ``None``,
            the pipeline derives it from tags (audio) or the parent
            directory name (photo).
        profile_ids: Profiles to assign the new item to after ingest.
            Empty or ``None`` means ingest without assignment.
        artwork_bytes_override: Raw image bytes to use as artwork when no
            embedded artwork is found in the audio file. Ignored for photos.
        crop: Optional crop box ``{"x", "y", "width", "height"}`` in
            original-image pixel coordinates. Applied before resizing.
            Ignored for audio media types.
        title_override: Title to use instead of the one from tags.
        artist_override: Artist to use instead of the one from tags.
        loudness: Loudness normalization settings for audio. Defaults to
            the server settings as they are now (settings page and
            environment); tests pass one to pin them.

    Returns:
        An ``IngestResult`` with ``processing_status=READY`` on success,
        ``skipped=True`` if the file was already in the library, or
        ``processing_status=FAILED`` with populated ``errors`` on failure.
    """
    result = IngestResult(source_path=str(file_path), media_type=media_type)

    logger.debug("Ingesting %s (type=%s)", file_path.name, media_type)
    try:
        content_hash = media_store.compute_hash(file_path)

        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)

            existing = await get_media_item_by_hash(conn, content_hash)
            if existing is not None:
                logger.debug(
                    "Skipping duplicate %s (hash %s…)",
                    file_path.name,
                    content_hash[:8],
                )
                result.skipped = True
                result.media_id = existing.id
                result.title = existing.title
                result.processing_status = ProcessingStatus(existing.processing_status)
                return result

            if media_type in (MediaType.MUSIC, MediaType.AUDIOBOOK):
                result = await _ingest_audio(
                    conn,
                    file_path,
                    media_type,
                    content_hash,
                    media_store,
                    playlist_title,
                    profile_ids or [],
                    result,
                    artwork_bytes_override=artwork_bytes_override,
                    title_override=title_override,
                    artist_override=artist_override,
                    loudness=loudness or await load_loudness_config(conn),
                )
                # Normalization was queued in the ingest transaction; don't
                # make it wait for the worker's next poll.
                queue_wakeup.wake(db_path)
                return result
            else:
                return await _ingest_photo(
                    conn,
                    file_path,
                    content_hash,
                    media_store,
                    playlist_title,
                    profile_ids or [],
                    result,
                    crop=crop,
                    title_override=title_override,
                )

    except Exception as exc:
        logger.exception("Failed to ingest %s", file_path)
        result.errors.append(str(exc))
        result.processing_status = ProcessingStatus.FAILED
        return result


async def ingest_fetched(
    items: list[FetchedItem],
    media_type: MediaType,
    db_path: Path,
    media_store: MediaStore,
    *,
    playlist_title: str | None = None,
    profile_ids: list[uuid.UUID] | None = None,
    crop: dict[str, float] | None = None,
    title_override: str | None = None,
    artist_override: str | None = None,
    loudness: LoudnessConfig | None = None,
) -> IngestBatchResult:
    """Ingest the files an importer fetched.

    Each item goes through ``ingest_file``. An item's own title, artist and
    thumbnail are used only where the caller gave no override and, for the
    thumbnail, where the file has no embedded artwork.

    Args:
        items: Output of ``Importer.fetch``.
        media_type: MUSIC, AUDIOBOOK, or PHOTO.
        db_path: Path to the SQLite database file.
        media_store: Initialised MediaStore for content-addressed storage.
        playlist_title: Optional playlist/group override.
        profile_ids: Profiles to assign the new items to after ingest.
        crop: Optional crop box for photo media. Ignored for audio.
        title_override: Title for every item, over the item's own.
        artist_override: Artist for every item, over the item's own.
        loudness: Loudness normalization settings for audio.

    Returns:
        ``IngestBatchResult`` with one result per item.
    """
    batch = IngestBatchResult(total_files=len(items))
    for item in items:
        artwork: bytes | None = None
        if item.thumbnail is not None:
            try:
                artwork = item.thumbnail.read_bytes()
            except OSError as exc:
                logger.warning("Could not read thumbnail %s: %s", item.thumbnail, exc)
        result = await ingest_file(
            item.path,
            media_type,
            db_path,
            media_store,
            playlist_title=playlist_title,
            profile_ids=profile_ids,
            crop=crop,
            artwork_bytes_override=artwork,
            title_override=title_override or item.title,
            artist_override=artist_override or item.artist,
            loudness=loudness,
        )
        batch.results.append(result)
        if result.skipped:
            batch.skipped += 1
        elif result.processing_status == ProcessingStatus.FAILED:
            batch.failed += 1
        else:
            batch.successful += 1
    return batch


async def ingest_url_batch(
    url: str,
    media_type: MediaType,
    db_path: Path,
    media_store: MediaStore,
    *,
    playlist_title: str | None = None,
    profile_ids: list[uuid.UUID] | None = None,
    crop: dict[str, float] | None = None,
    title_override: str | None = None,
    artist_override: str | None = None,
    registry: ImporterRegistry | None = None,
    loudness: LoudnessConfig | None = None,
) -> IngestBatchResult:
    """Fetch a URL with the first matching importer and ingest every file.

    Plain HTTP(S) file URLs are handled by the built-in ``HttpImporter``;
    installed plugins add other sources (e.g. YouTube). The fetch runs inline,
    without the retries of the import queue.

    Args:
        url: URL to fetch.
        media_type: MUSIC, AUDIOBOOK, or PHOTO — controls the processing
            branch taken after download.
        db_path: Path to the SQLite database file.
        media_store: Initialised MediaStore for content-addressed storage.
        playlist_title: Optional playlist/group override.
        profile_ids: Profiles to assign the new item to after ingest.
        crop: Optional crop box for photo media. Ignored for audio.
        title_override: Optional title override.
        artist_override: Optional artist override.
        registry: Importers to choose from. Defaults to the process-wide
            registry discovered from entry points.
        loudness: Loudness normalization settings for audio.

    Returns:
        One result per fetched file, with the same semantics as
        ``ingest_file``. If no importer can handle *url*, the source needs a
        plugin that is not installed, or the fetch fails, the batch holds a
        single failed result whose ``errors`` say why.
    """
    registry = registry or get_default_registry()
    try:
        registry.require_plugin(url)
        importer = registry.resolve(url)
        if importer is None:
            raise RuntimeError(f"No installed importer can handle {url}")
        with tempfile.TemporaryDirectory() as tmp:
            items = await importer.fetch(url, Path(tmp), FetchContext())
            if not items:
                raise RuntimeError(f"{importer.label} importer fetched nothing")
            return await ingest_fetched(
                items,
                media_type,
                db_path,
                media_store,
                playlist_title=playlist_title,
                profile_ids=profile_ids,
                crop=crop,
                title_override=title_override,
                artist_override=artist_override,
                loudness=loudness,
            )
    except Exception as exc:
        # A source that needs a missing plugin is a configuration problem, not
        # a crash worth a traceback.
        if isinstance(exc, SourceNeedsPluginError):
            logger.warning("Cannot ingest %s: %s", url, exc)
        else:
            logger.exception("Failed to ingest URL %s", url)
        failed = IngestResult(
            source_path=url,
            media_type=media_type,
            processing_status=ProcessingStatus.FAILED,
            errors=[str(exc)],
        )
        return IngestBatchResult(total_files=1, failed=1, results=[failed])


async def ingest_url(
    url: str,
    media_type: MediaType,
    db_path: Path,
    media_store: MediaStore,
    *,
    playlist_title: str | None = None,
    profile_ids: list[uuid.UUID] | None = None,
    crop: dict[str, float] | None = None,
    title_override: str | None = None,
    artist_override: str | None = None,
    registry: ImporterRegistry | None = None,
    loudness: LoudnessConfig | None = None,
) -> IngestResult:
    """Fetch a URL and ingest it, returning only the first file's result.

    Every fetched file is ingested; use ``ingest_url_batch`` to see the
    result of each one. Arguments are those of ``ingest_url_batch``.

    Returns:
        The first file's ``IngestResult``, with the semantics of
        ``ingest_file``, or a failed result if the fetch failed.
    """
    batch = await ingest_url_batch(
        url,
        media_type,
        db_path,
        media_store,
        playlist_title=playlist_title,
        profile_ids=profile_ids,
        crop=crop,
        title_override=title_override,
        artist_override=artist_override,
        registry=registry,
        loudness=loudness,
    )
    return batch.results[0]


async def ingest_directory(
    dir_path: Path,
    media_type: MediaType,
    db_path: Path,
    media_store: MediaStore,
    *,
    playlist_title: str | None = None,
    profile_ids: list[uuid.UUID] | None = None,
    loudness: LoudnessConfig | None = None,
) -> IngestBatchResult:
    """Ingest all supported media files in a directory tree.

    Recursively walks ``dir_path``, calling ``ingest_file`` on each file
    whose extension matches the expected type for ``media_type``. Files
    with unsupported extensions are silently skipped.

    Args:
        dir_path: Root directory to walk.
        media_type: Controls which file extensions are considered and
            which processing branch is used.
        db_path: Path to the SQLite database file.
        media_store: Initialised MediaStore for content-addressed storage.
        playlist_title: Optional playlist/group override applied to all
            files in the batch.
        profile_ids: Profiles to assign all newly ingested items to.
        loudness: Loudness normalization settings for audio.

    Returns:
        ``IngestBatchResult`` with per-file results and aggregate counts.
    """
    extensions = (
        _AUDIO_EXTENSIONS
        if media_type in (MediaType.MUSIC, MediaType.AUDIOBOOK)
        else _PHOTO_EXTENSIONS
    )

    files = sorted(
        p for p in dir_path.rglob("*") if p.is_file() and p.suffix.lower() in extensions
    )

    batch = IngestBatchResult(total_files=len(files))

    for file_path in files:
        result = await ingest_file(
            file_path,
            media_type,
            db_path,
            media_store,
            playlist_title=playlist_title,
            profile_ids=profile_ids,
            loudness=loudness,
        )
        batch.results.append(result)

        if result.skipped:
            batch.skipped += 1
        elif result.processing_status == ProcessingStatus.FAILED:
            batch.failed += 1
        else:
            batch.successful += 1

    return batch


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _ingest_audio(
    conn: aiosqlite.Connection,
    file_path: Path,
    media_type: MediaType,
    content_hash: str,
    media_store: MediaStore,
    playlist_title: str | None,
    profile_ids: list[uuid.UUID],
    result: IngestResult,
    *,
    artwork_bytes_override: bytes | None = None,
    title_override: str | None = None,
    artist_override: str | None = None,
    loudness: LoudnessConfig,
) -> IngestResult:
    """Process one audio file and write all DB records.

    Stores the original audio file as the ``audio`` file and generates
    thumbnails from embedded artwork if present, then writes the MediaItem and
    all ProcessedFile rows in a single transaction. When loudness
    normalization is on and ffmpeg is available, a normalization job for the
    item goes on the import queue in that same transaction; the worker then
    replaces the ``audio`` file (keeping the original as ``audio_source``).
    Until then the item is playable, "not normalized".

    Without ffmpeg the item is left "not normalized" (``loudness_target_lufs``
    is NULL) for the backfill, and a warning is logged.

    Args:
        conn: Open, configured database connection (not yet committed).
        file_path: Source audio file path.
        media_type: MUSIC or AUDIOBOOK.
        content_hash: Pre-computed SHA-256 of ``file_path``.
        media_store: Destination storage.
        playlist_title: Explicit playlist override, or ``None`` to derive
            from the ``album`` tag / parent directory.
        profile_ids: Profiles to assign the item to after commit.
        result: IngestResult instance to populate and return.
        artwork_bytes_override: Raw image bytes used as artwork fallback
            when no embedded artwork is found in the file (e.g. from
            an importer thumbnail).
        title_override: Title to use instead of the one from tags.
        artist_override: Artist to use instead of the one from tags.
        loudness: Loudness normalization settings; whether to queue the job.

    Returns:
        The populated ``result`` with ``processing_status=READY``.
    """
    meta = extract_metadata(file_path)
    title: str = title_override or meta["title"] or file_path.stem
    artist: str | None = artist_override or meta.get("artist")
    derived_playlist = playlist_title or meta["album"] or file_path.parent.name

    # Store the original audio file unchanged.
    suffix = file_path.suffix.lower()
    audio_hash, audio_rel = media_store.store(file_path, "audio", suffix)
    audio_size = (media_store.root / audio_rel).stat().st_size
    mime_type = _AUDIO_MIME.get(suffix, "audio/mpeg")

    queue_normalization = loudness.enabled
    if queue_normalization and not ffmpeg_available():
        logger.warning(
            "ffmpeg not found: storing %s without loudness normalization. "
            "Install ffmpeg, then run 'kidsplay media normalize --all'.",
            file_path.name,
        )
        queue_normalization = False

    now = datetime.now()
    item = MediaItem(
        media_type=media_type,
        content_hash=content_hash,
        playlist_title=derived_playlist,
        title=title,
        artist=artist,
        duration_seconds=meta.get("duration_seconds"),
        processing_status=ProcessingStatus.PROCESSING,
        created_at=now,
        updated_at=now,
    )
    await create_media_item(conn, item)

    await create_processed_file(
        conn,
        ProcessedFile(
            media_id=item.id,
            content_hash=audio_hash,
            file_type="audio",
            relative_path=audio_rel,
            size_bytes=audio_size,
            mime_type=mime_type,
        ),
    )

    # Artwork → thumbnails.  Missing artwork is not an error.
    # Prefer embedded artwork; fall back to the override (e.g. an importer thumbnail).
    artwork_bytes = extract_artwork(file_path) or artwork_bytes_override
    if artwork_bytes:
        try:
            await _write_thumbnails(conn, item.id, artwork_bytes, media_store)
        except Exception as exc:
            logger.warning("Thumbnail generation failed for %s: %s", file_path, exc)
    else:
        logger.debug("No embedded artwork in %s", file_path)

    now = datetime.now()
    await update_media_item_status(conn, item.id, ProcessingStatus.READY, now)

    for pid in profile_ids:
        await assign_media_to_profile(conn, pid, item.id, now)

    if queue_normalization:
        await create_loudness_job(conn, item.id, media_type)

    await conn.commit()

    logger.info(
        "Ingested audio %s: %r by %r (hash %s…)",
        media_type.value,
        title,
        artist,
        content_hash[:8],
    )
    result.media_id = item.id
    result.title = title
    result.processing_status = ProcessingStatus.READY
    return result


async def _ingest_photo(
    conn: aiosqlite.Connection,
    file_path: Path,
    content_hash: str,
    media_store: MediaStore,
    playlist_title: str | None,
    profile_ids: list[uuid.UUID],
    result: IngestResult,
    *,
    crop: dict[str, float] | None = None,
    title_override: str | None = None,
) -> IngestResult:
    """Process one photo file and write all DB records.

    Resizes the photo to fit the 640×480 device screen, generates three
    thumbnail sizes from the original bytes, then writes all records.

    Args:
        conn: Open, configured database connection.
        file_path: Source photo path.
        content_hash: Pre-computed SHA-256 of the original file.
        media_store: Destination storage.
        playlist_title: Explicit playlist override, or ``None`` to use the
            parent directory name.
        profile_ids: Profiles to assign the item to after commit.
        result: IngestResult instance to populate and return.
        crop: Optional crop box ``{"x", "y", "width", "height"}`` applied
            before resizing.

    Returns:
        The populated ``result`` with ``processing_status=READY``.
    """
    title: str = title_override or file_path.stem
    derived_playlist = playlist_title or file_path.parent.name

    quality = (await load_server_settings(conn)).values.webp_quality
    photo_hash, photo_rel = process_photo(
        file_path, 640, 480, media_store, crop=crop, quality=quality
    )
    photo_size = (media_store.root / photo_rel).stat().st_size

    now = datetime.now()
    item = MediaItem(
        media_type=MediaType.PHOTO,
        content_hash=content_hash,
        playlist_title=derived_playlist,
        title=title,
        processing_status=ProcessingStatus.PROCESSING,
        created_at=now,
        updated_at=now,
    )
    await create_media_item(conn, item)

    await create_processed_file(
        conn,
        ProcessedFile(
            media_id=item.id,
            content_hash=photo_hash,
            file_type="photo_resized",
            relative_path=photo_rel,
            size_bytes=photo_size,
            mime_type="image/webp",
        ),
    )

    # Thumbnails from the original photo bytes.
    photo_bytes = file_path.read_bytes()
    try:
        await _write_thumbnails(conn, item.id, photo_bytes, media_store)
    except Exception as exc:
        logger.warning("Thumbnail generation failed for %s: %s", file_path, exc)

    now = datetime.now()
    await update_media_item_status(conn, item.id, ProcessingStatus.READY, now)

    for pid in profile_ids:
        await assign_media_to_profile(conn, pid, item.id, now)

    await conn.commit()

    logger.info(
        "Ingested photo %r in %r (hash %s…)", title, derived_playlist, content_hash[:8]
    )
    result.media_id = item.id
    result.title = title
    result.processing_status = ProcessingStatus.READY
    return result


async def _write_thumbnails(
    conn: aiosqlite.Connection,
    media_id: uuid.UUID,
    image_bytes: bytes,
    media_store: MediaStore,
) -> None:
    """Generate all three thumbnail sizes and insert ProcessedFile rows.

    Args:
        conn: Open database connection.
        media_id: UUID of the parent MediaItem.
        image_bytes: Raw source image bytes (any Pillow-readable format).
        media_store: Destination content-addressed store.
    """
    quality = (await load_server_settings(conn)).values.webp_quality
    thumbnails = generate_thumbnails(
        image_bytes, _ALL_SIZES, media_store, quality=quality
    )
    for size, thumb_hash, thumb_rel in thumbnails:
        thumb_size = (media_store.root / thumb_rel).stat().st_size
        await create_processed_file(
            conn,
            ProcessedFile(
                media_id=media_id,
                content_hash=thumb_hash,
                file_type=_FILE_TYPE_FOR_SIZE[size],
                relative_path=thumb_rel,
                size_bytes=thumb_size,
                mime_type="image/webp",
            ),
        )


# ---------------------------------------------------------------------------
# Loudness normalization
# ---------------------------------------------------------------------------


class NormalizeOutcome(StrEnum):
    """What ``normalize_media_item`` did to one item."""

    NORMALIZED = "normalized"
    """Re-encoded to the target and repointed at the new file."""

    SKIPPED = "skipped"
    """Nothing to do: already at the target, not audio, or gone."""

    UNCHANGED = "unchanged"
    """Too quiet or short to measure; audio kept as it is."""


@dataclass(frozen=True)
class _LoudnessOutcome:
    """Result of normalizing one audio file into the store.

    Attributes:
        target: Target the file was processed for.
        result: Measurements, or ``None`` if the source was too quiet to
            measure and was kept unchanged.
        stored: ``(content_hash, relative_path, size_bytes)`` of the
            normalized file in the store, or ``None`` when unchanged.
    """

    target: LoudnessTarget
    result: NormalizationResult | None
    stored: tuple[str, str, int] | None

    @property
    def mode(self) -> str | None:
        """How the gain was applied, or ``None`` if the audio is unchanged."""
        return self.result.mode if self.result else None

    def item_fields(self) -> dict[str, float | None]:
        """Return the numeric ``MediaItem`` loudness fields for this outcome."""
        return {
            "loudness_source_lufs": (
                self.result.source.integrated_lufs if self.result else None
            ),
            "loudness_source_true_peak_dbtp": (
                self.result.source.true_peak_dbtp if self.result else None
            ),
            "loudness_gain_db": self.result.gain_db if self.result else None,
            "loudness_target_lufs": self.target.integrated_lufs,
            "loudness_target_true_peak_dbtp": self.target.true_peak_dbtp,
        }

    def stored_file(self, media_id: uuid.UUID) -> ProcessedFile:
        """Return an ``audio`` row for the normalized file.

        Args:
            media_id: The item the file belongs to.

        Raises:
            ValueError: If nothing was stored.
        """
        if self.stored is None:
            raise ValueError("no normalized file was stored")
        content_hash, rel, size = self.stored
        return ProcessedFile(
            media_id=media_id,
            content_hash=content_hash,
            file_type="audio",
            relative_path=rel,
            size_bytes=size,
            mime_type=NORMALIZED_MIME,
        )


async def _normalize_to_store(
    source: Path,
    target: LoudnessTarget,
    media_store: MediaStore,
    *,
    allow_limiting: bool = True,
) -> _LoudnessOutcome:
    """Normalize ``source`` and store the result as a new store file.

    ffmpeg runs in a worker thread so the event loop stays responsive.
    Cancelling the awaiting task kills the running ffmpeg instead of waiting
    for the encode to finish.

    Args:
        source: Audio file to normalize (left untouched).
        target: Loudness target.
        media_store: Destination store.
        allow_limiting: Whether loudnorm may limit peaks (see
            ``LoudnessConfig.allow_limiting``).

    Returns:
        The outcome. A source too quiet to measure is not an error: the
        outcome then has no stored file.

    Raises:
        LoudnessError: If ffmpeg is missing or fails.
    """
    cancel = threading.Event()
    # ignore_cleanup_errors: on cancellation the killed ffmpeg may still be
    # closing its output when the directory is removed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        out = Path(tmp) / f"normalized{NORMALIZED_SUFFIX}"
        work = asyncio.ensure_future(
            asyncio.to_thread(
                partial(
                    normalize_loudness,
                    source,
                    out,
                    target,
                    allow_limiting=allow_limiting,
                    cancel=cancel,
                )
            )
        )
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            # Kill ffmpeg now rather than at the worker's next poll: on a
            # server shutdown the process may exit before that poll, leaving
            # ffmpeg orphaned and running beside the next one.
            kill_jobs(cancel)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(work), _CANCEL_JOIN_SECONDS)
            raise
        except UnmeasurableLoudnessError as exc:
            logger.info("Keeping %s unchanged: %s", source.name, exc)
            return _LoudnessOutcome(target=target, result=None, stored=None)
        content_hash, rel = media_store.store(out, "audio", NORMALIZED_SUFFIX)
    size = (media_store.root / rel).stat().st_size
    return _LoudnessOutcome(
        target=target, result=result, stored=(content_hash, rel, size)
    )


def _source_audio(files: list[ProcessedFile]) -> ProcessedFile | None:
    """Return the row holding an item's original audio.

    That is the ``audio_source`` row once the item has been normalized, and
    the ``audio`` row (still the original) before.
    """
    by_type = {pf.file_type: pf for pf in files}
    return by_type.get("audio_source") or by_type.get("audio")


async def normalize_media_item(
    media_id: uuid.UUID,
    db_path: Path,
    media_store: MediaStore,
    loudness: LoudnessConfig | None = None,
) -> NormalizeOutcome:
    """Bring one existing audio item to the current loudness target.

    Idempotent: an item already processed for its type's current target is
    skipped without decoding anything. The exception is a ``capped`` item
    while ``loudness.allow_limiting`` is on (limiting was switched back on):
    it is normalized again. Cancelling the awaiting task kills the
    running ffmpeg. Otherwise the original audio (the
    ``audio_source`` file, or the ``audio`` file of a never-normalized item)
    is normalized into a *new* store file, and in one transaction the
    item's ``audio`` row is repointed at it (keeping the original as
    ``audio_source``) and its loudness fields are updated. No store file is
    rewritten or deleted; a superseded normalized file stays in the store
    until garbage collection.

    Args:
        media_id: Item to normalize.
        db_path: Path to the SQLite database file.
        media_store: Content-addressed store.
        loudness: Loudness settings (targets per media type). Defaults to
            the server settings as they are now.

    Returns:
        What was done.

    Raises:
        LoudnessError: If ffmpeg is missing or fails, or the item's stored
            audio is missing.
    """
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        loudness = loudness or await load_loudness_config(conn)
        item = await get_media_item(conn, media_id)
        if (
            item is None
            or item.media_type not in _AUDIO_TYPES
            or item.processing_status != ProcessingStatus.READY
        ):
            return NormalizeOutcome.SKIPPED
        target = loudness.target_for(item.media_type)
        # A capped item is only up to date while limiting stays forbidden:
        # once it is allowed again, the item may reach the target after all.
        capped_but_limiting_allowed = (
            item.loudness_mode == "capped" and loudness.allow_limiting
        )
        if not capped_but_limiting_allowed and target.matches(
            item.loudness_target_lufs, item.loudness_target_true_peak_dbtp
        ):
            return NormalizeOutcome.SKIPPED
        source = _source_audio(await list_processed_files(conn, media_id))

    if source is None:
        raise LoudnessError(f"{item.title!r} has no stored audio")
    source_path = media_store.get_absolute_path(source.relative_path)
    if not source_path.is_file():
        raise LoudnessError(f"stored audio is missing: {source.relative_path}")
    if not ffmpeg_available():
        raise LoudnessError("ffmpeg is not installed or not on PATH")

    # Encode with no connection open: it can take minutes for an audiobook.
    outcome = await _normalize_to_store(
        source_path, target, media_store, allow_limiting=loudness.allow_limiting
    )

    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        files = await list_processed_files(conn, media_id)
        current_source = _source_audio(files)
        audio = next((pf for pf in files if pf.file_type == "audio"), None)
        if (
            current_source is None
            or audio is None
            or current_source.id != source.id
            or current_source.content_hash != source.content_hash
        ):
            # Deleted or replaced while encoding; the new file is harmless.
            logger.info("Media %s changed during normalization; skipped", media_id)
            return NormalizeOutcome.SKIPPED
        if outcome.stored is not None:
            new_audio = outcome.stored_file(media_id)
            if audio.id == current_source.id:
                # First normalization: the audio row is the original. Keep
                # it as audio_source and add the normalized file as audio.
                await update_processed_file(
                    conn, audio.model_copy(update={"file_type": "audio_source"})
                )
                await create_processed_file(conn, new_audio)
            else:
                await update_processed_file(
                    conn, new_audio.model_copy(update={"id": audio.id})
                )
        fields = outcome.item_fields()
        await update_media_item_loudness(
            conn,
            media_id,
            source_lufs=fields["loudness_source_lufs"],
            source_true_peak_dbtp=fields["loudness_source_true_peak_dbtp"],
            gain_db=fields["loudness_gain_db"],
            mode=outcome.mode,
            target_lufs=target.integrated_lufs,
            target_true_peak_dbtp=target.true_peak_dbtp,
            updated_at=datetime.now(),
        )
        await conn.commit()

    if outcome.stored is None:
        return NormalizeOutcome.UNCHANGED
    logger.info(
        "Normalized %r to %.1f LUFS (gain %+.1f dB)",
        item.title,
        target.integrated_lufs,
        outcome.result.gain_db if outcome.result else 0.0,
    )
    return NormalizeOutcome.NORMALIZED
