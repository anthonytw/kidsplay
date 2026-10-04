"""Device and profile models.

Profiles represent children (content is assigned per-child).
Devices represent physical hardware (RPi CM4 handhelds).
Each device is linked to exactly one profile.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class Profile(BaseModel):
    """A child's profile.

    Media is assigned to profiles, not devices. This means swapping a
    device to a different kid only requires changing the device's profile_id,
    not reassigning every piece of media.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str = Field(description="Child's display name.")
    created_at: datetime = Field(default_factory=datetime.now)


class ProfileCreate(BaseModel):
    """Request model for creating a new profile."""

    name: str


class Device(BaseModel):
    """A physical playback device.

    Represents one RPi CM4 handheld. The device syncs media from the server
    based on its linked profile's assigned content.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str = Field(description="Human-readable device name, e.g. 'Leo\\'s GameBoy'.")
    profile_id: uuid.UUID = Field(description="Which child's profile this device uses.")
    display_width: int = Field(
        default=640, description="Device screen width in pixels."
    )
    display_height: int = Field(
        default=480, description="Device screen height in pixels."
    )
    last_sync_at: datetime | None = Field(
        default=None, description="When this device last completed a sync."
    )
    last_sync_manifest_hash: str | None = Field(
        default=None,
        description="Hash of the manifest at last successful sync. "
        "Used for quick 'anything changed?' checks.",
    )
    api_key: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Simple bearer token for device authentication during sync.",
    )
    created_at: datetime = Field(default_factory=datetime.now)


class DeviceCreate(BaseModel):
    """Request model for registering a new device."""

    name: str
    profile_id: uuid.UUID
    display_width: int = 640
    display_height: int = 480


class ProfileMediaAssignment(BaseModel):
    """Links a media item to a profile.

    This is the join table concept: which media is available on which
    child's devices. Assigning a track to a profile makes it appear in
    the sync manifest for all devices linked to that profile.
    """

    profile_id: uuid.UUID
    media_id: uuid.UUID
    assigned_at: datetime = Field(default_factory=datetime.now)
