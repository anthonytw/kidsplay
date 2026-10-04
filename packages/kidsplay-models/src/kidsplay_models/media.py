"""Media data models.

Defines a single MediaItem type covering music, audiobooks, and photos.
All media follows a two-level hierarchy: playlist_title (grouping) → title (item).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MediaType(StrEnum):
    """Types of media managed by the system."""

    MUSIC = "music"
    AUDIOBOOK = "audiobook"
    PHOTO = "photo"


class MediaItem(BaseModel):
    """A single piece of media in the system.

    All three media types (music, audiobook, photo) use the same model.
    The UI organizes items in a two-level hierarchy:
      playlist_title (the group) → title (the individual item).

    Examples:
      - Music: playlist_title="Pica-Pica Halloween", title="La Bamba"
      - Audiobook: playlist_title="The Gruffalo", title="Chapter 3"
      - Photo: playlist_title="Beach Trip 2025", title="sunset_01"
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    media_type: MediaType
    content_hash: str = Field(
        description="SHA-256 hash of the original source file. "
        "Used for deduplication and content-addressed storage."
    )
    playlist_title: str = Field(
        description="Group/playlist name shown in the top-level list view."
    )
    title: str = Field(description="Individual item title shown within a playlist.")
    artist: str | None = Field(
        default=None,
        description="Optional artist or author. Displayed alongside the title.",
    )
    duration_seconds: int | None = Field(
        default=None,
        description="Duration in seconds. Required for audio, None for photos.",
    )
    processing_status: str = Field(
        default="pending",
        description="Current processing state. See processing.ProcessingStatus.",
    )
    loudness_source_lufs: float | None = Field(
        default=None,
        description="Integrated loudness of the original audio (EBU R128, LUFS). "
        "None if never measured, or too quiet to measure.",
    )
    loudness_source_true_peak_dbtp: float | None = Field(
        default=None,
        description="True peak of the original audio (dBTP).",
    )
    loudness_gain_db: float | None = Field(
        default=None,
        description="Loudness change applied by normalization (output minus "
        "source, dB). None if the audio was stored unchanged.",
    )
    loudness_mode: str | None = Field(
        default=None,
        description="How the gain was applied: 'linear' (one constant gain), "
        "'dynamic' (gain plus true-peak limiting, which reshapes loud peaks) "
        "or 'capped' (linear, but held below the target so the true-peak "
        "ceiling is met without limiting). None if the audio was stored "
        "unchanged or was normalized before the mode was recorded.",
    )
    loudness_target_lufs: float | None = Field(
        default=None,
        description="Integrated-loudness target the audio was processed for. "
        "None means loudness normalization has not run for this item.",
    )
    loudness_target_true_peak_dbtp: float | None = Field(
        default=None,
        description="True-peak ceiling the audio was processed for (dBTP).",
    )
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
