"""Import queue models.

Defines the data types for the background import queue. Slow or
rate-limited importers (``Importer.requires_queue``) run through it, and each
failed attempt is retried by the worker until ``max_retries`` is reached.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from .media import MediaType

LOUDNESS_JOB = "loudness"
"""``QueueItem.importer`` value of a loudness-normalization job.

Such a job normalizes one existing media item (``QueueItem.media_id``) instead
of importing a source: the import queue also carries the server's background
audio processing, so a single worker runs one ffmpeg job at a time and pending
work survives a restart. ``url`` is ``loudness:<media id>``. It is a reserved
name: no importer may use it."""


class QueueStatus(StrEnum):
    """States for a queued import job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QueueItem(BaseModel):
    """A queued import job.

    Tracks the source URL, the importer that fetches it, ingest parameters,
    retry state, and the full log for debugging failed imports.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    url: str = Field(description="Source URL (or path) to import.")
    importer: str | None = Field(
        default=None,
        description=(
            "Name of the importer that fetches ``url``. ``None`` means the "
            "worker picks the first importer whose ``can_handle`` matches."
        ),
    )
    media_type: MediaType = Field(description="MUSIC or AUDIOBOOK.")
    playlist_title: str = Field(description="Playlist/group name for ingested items.")
    profile_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Profiles to assign after successful ingest.",
    )
    title_override: str | None = Field(
        default=None,
        description="Override the title extracted from metadata.",
    )
    artist_override: str | None = Field(
        default=None,
        description="Override the artist extracted from metadata.",
    )
    status: QueueStatus = Field(default=QueueStatus.PENDING)
    attempt: int = Field(default=0, description="Current attempt number (0-based).")
    max_retries: int = Field(default=5, description="Maximum number of retry attempts.")
    last_error: str | None = Field(
        default=None,
        description="Error message from the most recent failed attempt.",
    )
    log: str = Field(
        default="",
        description="Full log of every attempt, as reported by the importer.",
    )
    media_id: uuid.UUID | None = Field(
        default=None,
        description="ID of the created media item on success.",
    )
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = Field(default=None)


class QueueRequest(BaseModel):
    """Request to submit a source to the import queue."""

    url: str = Field(description="Source URL (or path) to import.")
    importer: str | None = Field(
        default=None,
        description=(
            "Importer to use, by name. Omit to pick the first importer whose "
            "``can_handle`` matches ``url``."
        ),
    )
    media_type: MediaType = Field(description="MUSIC or AUDIOBOOK.")
    playlist_title: str = Field(description="Playlist/group name for ingested items.")
    profile_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Profiles to assign after successful ingest.",
    )
    title_override: str | None = Field(
        default=None,
        description="Override the title extracted from metadata.",
    )
    artist_override: str | None = Field(
        default=None,
        description="Override the artist extracted from metadata.",
    )
    max_retries: int = Field(
        default=5,
        description="Maximum number of retry attempts (default 5).",
    )
