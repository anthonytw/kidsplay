"""Media processing models.

Defines the processing pipeline data types: ingest requests, processing
status tracking, thumbnail specifications, and processed file references.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field, model_validator

from .media import MediaType


class ProcessingStatus(StrEnum):
    """Processing pipeline states for a media item."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class ThumbnailSize(StrEnum):
    """Pre-defined thumbnail sizes matching device UI requirements.

    These are the exact sizes the device player expects. The server
    generates all three for every media item that has artwork.
    Thumbnails maintain aspect ratio within these bounds.
    """

    SMALL = "60x60"
    MEDIUM = "200x200"
    LARGE = "480x480"

    @property
    def width(self) -> int:
        """Extract width from the size string."""
        return int(self.value.split("x")[0])

    @property
    def height(self) -> int:
        """Extract height from the size string."""
        return int(self.value.split("x")[1])

    @property
    def dimensions(self) -> tuple[int, int]:
        """Return (width, height) tuple."""
        return (self.width, self.height)


class ProcessedFile(BaseModel):
    """A processed output file in content-addressed storage.

    Each media item may produce multiple processed files: the audio file
    itself, thumbnails at various sizes, resized photos. This model
    tracks each one.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    media_id: uuid.UUID = Field(description="The media item this file belongs to.")
    content_hash: str = Field(description="SHA-256 hash of the processed file content.")
    file_type: str = Field(
        description="What kind of processed output this is. "
        "One of: 'audio', 'audio_source', 'thumbnail_small', "
        "'thumbnail_medium', 'thumbnail_large', 'photo_resized'. "
        "'audio_source' is the original of a loudness-normalized 'audio' "
        "file; it stays on the server and is never synced to devices."
    )
    relative_path: str = Field(
        description="Path relative to MEDIA_STORE root. "
        "Example: 'thumbnails/ab/abcd1234_200x200.webp'"
    )
    size_bytes: int = Field(description="File size in bytes.")
    mime_type: str = Field(
        default="application/octet-stream",
        description="MIME type of the processed file.",
    )
    created_at: datetime = Field(default_factory=datetime.now)


class IngestRequest(BaseModel):
    """Request to ingest media from a filesystem path or URL.

    Exactly one of ``source_path`` or ``source_url`` must be provided.
    ``source_path`` can point to a single file or a directory; directories
    are walked recursively. ``source_url`` is fetched by the first installed
    importer that can handle it: direct HTTP(S) file URLs are built in, and
    plugins add more (e.g. YouTube via ``kidsplay-importer-ytdlp``).
    """

    source_path: str | None = Field(
        default=None,
        description="Absolute path to file or directory to ingest.",
    )
    source_url: str | None = Field(
        default=None,
        description=(
            "URL to download and ingest. Direct file URLs are built in; "
            "importer plugins add other sources."
        ),
    )
    media_type: MediaType = Field(description="What kind of media this is.")
    playlist_title: str = Field(
        description="Playlist/group name for all ingested items."
    )
    profile_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Profiles to assign this media to after ingest. "
        "Empty list means ingest without assignment.",
    )
    crop_x: float | None = Field(
        default=None,
        description="Left edge of crop region in source-image pixels.",
    )
    crop_y: float | None = Field(
        default=None,
        description="Top edge of crop region in source-image pixels.",
    )
    crop_width: float | None = Field(
        default=None,
        description="Width of crop region in source-image pixels.",
    )
    crop_height: float | None = Field(
        default=None,
        description="Height of crop region in source-image pixels.",
    )
    title_override: str | None = Field(
        default=None,
        description="Override the title extracted from file metadata.",
    )
    artist_override: str | None = Field(
        default=None,
        description="Override the artist extracted from file metadata.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "IngestRequest":
        """Ensure exactly one of source_path or source_url is provided."""
        has_path = self.source_path is not None
        has_url = self.source_url is not None
        if has_path == has_url:
            raise ValueError(
                "Exactly one of source_path or source_url must be provided."
            )
        return self


class IngestResult(BaseModel):
    """Result of ingesting a single media file."""

    media_id: uuid.UUID | None = Field(
        default=None,
        description="ID of the created media item. None if failed.",
    )
    source_path: str
    media_type: MediaType
    title: str = ""
    processing_status: ProcessingStatus = ProcessingStatus.PENDING
    errors: list[str] = Field(default_factory=list)
    skipped: bool = Field(
        default=False,
        description="True if file was already in the library (duplicate content_hash).",
    )


class IngestBatchResult(BaseModel):
    """Result of ingesting a directory of media files."""

    total_files: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[IngestResult] = Field(default_factory=list)


class NormalizeRequest(BaseModel):
    """Request to (re-)normalize the loudness of existing audio items.

    Exactly one of ``all`` or a non-empty ``media_ids`` must be given. Items
    already normalized to the current target are skipped, so repeating a
    request does not re-encode anything.
    """

    all: bool = Field(
        default=False,
        description="Normalize every music and audiobook item in the library.",
    )
    media_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Normalize only these media items.",
    )

    @model_validator(mode="after")
    def _exactly_one_selection(self) -> "NormalizeRequest":
        """Ensure exactly one of ``all`` or ``media_ids`` is provided."""
        if self.all == bool(self.media_ids):
            raise ValueError("Give exactly one of all=true or a list of media_ids.")
        return self


class NormalizeStatus(BaseModel):
    """Progress of the background loudness normalization.

    Covers the jobs on the import queue: the ones a library-wide request
    added, and the ones each audio ingest added.
    """

    MAX_ERRORS: ClassVar[int] = 20
    """Most messages kept in ``errors``; the rest are only counted."""

    running: bool = Field(
        default=False, description="True while a backfill is in progress."
    )
    total: int = Field(default=0, description="Items selected for this run.")
    normalized: int = Field(default=0, description="Items re-encoded to the target.")
    skipped: int = Field(
        default=0,
        description="Items left alone: already at the current target, not "
        "audio, or no longer in the library.",
    )
    unchanged: int = Field(
        default=0,
        description="Items too quiet or short to measure, kept as they are.",
    )
    failed: int = Field(default=0, description="Items that could not be normalized.")
    errors: list[str] = Field(
        default_factory=list,
        description="One message per failed item, at most 20; see ``errors_omitted``.",
    )
    errors_omitted: int = Field(
        default=0, description="Failures beyond the messages kept in ``errors``."
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def add_error(self, message: str) -> None:
        """Record one failure message, keeping at most ``MAX_ERRORS``.

        Args:
            message: What went wrong for one item.
        """
        if len(self.errors) < self.MAX_ERRORS:
            self.errors.append(message)
        else:
            self.errors_omitted += 1
