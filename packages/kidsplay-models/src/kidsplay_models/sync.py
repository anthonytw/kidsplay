"""Sync protocol models.

Defines the manifest format used for device synchronization. The sync
protocol is pull-based: devices request a manifest from the server,
diff it against local state, and download/delete the difference.

The manifest contains:
1. files — every file that should exist on the device
2. media — metadata for every media item (for the device's local DB)
3. profile_settings — the device's profile settings (volume cap, bedtime)
4. sync_interval_seconds — how often the device should sync
5. theme — the theme chosen by the device's profile, if any; its asset files
   are also listed in ``files``

Unknown fields are ignored, so an older device keeps syncing when a newer
server adds a field.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .media import MediaType
from .settings import ProfileSettings
from .themes import ThemeDefinition


class SyncFileEntry(BaseModel):
    """A single file that should exist on the device.

    The device uses content_hash to determine if it already has this
    file. If not, it downloads from GET /api/v1/sync/file/{content_hash}.
    """

    content_hash: str = Field(description="SHA-256 hash. Used for dedup and download.")
    relative_path: str = Field(
        description="Where this file should live on the device, "
        "relative to the device's media root."
    )
    size_bytes: int = Field(
        description="File size for storage estimation and progress."
    )
    file_type: str = Field(
        description="One of: 'audio', 'thumbnail', 'photo', 'theme'."
    )


class SyncMediaEntry(BaseModel):
    """Metadata for a single media item, sent as part of sync.

    Provides everything the device needs to display and play the item
    without server connectivity. Written into the device's local SQLite.
    """

    media_id: uuid.UUID
    media_type: MediaType
    playlist_title: str
    title: str
    artist: str | None = None
    duration_seconds: int | None = None

    audio_path: str | None = Field(
        default=None,
        description="Relative path to the audio file on device.",
    )
    photo_path: str | None = Field(
        default=None,
        description="Relative path to the processed photo on device.",
    )
    thumbnail_paths: dict[str, str] = Field(
        default_factory=dict,
        description="Map of ThumbnailSize value to relative path. "
        "Example: {'60x60': 'thumbnails/abc_60x60.webp'}",
    )


class SyncManifest(BaseModel):
    """Complete sync manifest for a device.

    Response to GET /api/v1/devices/{device_id}/manifest.
    Contains everything the device needs to bring itself up to date.
    """

    # Explicit: older devices must ignore fields added by newer servers.
    model_config = ConfigDict(extra="ignore")

    device_id: uuid.UUID
    profile_id: uuid.UUID
    generated_at: datetime = Field(default_factory=datetime.now)
    manifest_hash: str = Field(
        description="SHA-256 hash of the manifest content. "
        "Used for quick change detection via If-None-Match."
    )
    files: list[SyncFileEntry] = Field(
        default_factory=list,
        description="All files that should exist on the device.",
    )
    media: list[SyncMediaEntry] = Field(
        default_factory=list,
        description="Metadata for all assigned media items.",
    )
    total_size_bytes: int = Field(
        default=0,
        description="Sum of all file sizes for storage estimation.",
    )
    profile_settings: ProfileSettings = Field(
        default_factory=ProfileSettings,
        description="Settings of the device's profile. The device persists "
        "them so they apply offline.",
    )
    sync_interval_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Seconds between the device's sync attempts. None: the "
        "device keeps its locally configured interval.",
    )
    theme: ThemeDefinition | None = Field(
        default=None,
        description="The theme the profile chose, with its assets (which are "
        "also in ``files``). None: the profile chose none. The device parses "
        "it separately, so an unreadable theme never stops media from syncing.",
    )
