"""Profile and device CRUD endpoints.

Routes
------
GET    /profiles                        — list all profiles
POST   /profiles                        — create profile
GET    /profiles/{profile_id}           — fetch profile
DELETE /profiles/{profile_id}           — delete profile
GET    /profiles/{profile_id}/settings  — fetch profile settings
PUT    /profiles/{profile_id}/settings  — replace profile settings
POST   /profiles/{profile_id}/assign-group  — bulk-assign media by filter

GET    /devices                         — list all devices
POST   /devices                         — register device
GET    /devices/{device_id}             — fetch device
DELETE /devices/{device_id}             — unregister device
PATCH  /devices/{device_id}             — partial update
"""

import logging
import uuid
from datetime import datetime

import aiosqlite
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from kidsplay_models import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    Device,
    Profile,
    ProfileSettings,
)
from kidsplay_models.device import DeviceCreate, ProfileCreate
from kidsplay_models.media import MediaType
from kidsplay_server.database import (
    assign_media_to_profile,
    create_device,
    create_profile,
    delete_device,
    delete_profile,
    get_device,
    get_profile,
    get_profile_settings,
    list_devices,
    list_media_items,
    list_profiles,
    set_profile_settings,
    update_device,
)
from kidsplay_server.themes import resolve_theme

from .deps import DBConn

router = APIRouter(tags=["devices"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _not_found(resource: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"detail": f"{resource} not found", "error_code": "NOT_FOUND"},
    )


def _conflict(detail: str, error_code: str = "CONFLICT") -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"detail": detail, "error_code": error_code},
    )


class DevicePatch(BaseModel):
    """Partial update for PATCH /devices/{device_id}.

    All fields are optional; only provided (non-None) values are applied.
    """

    name: str | None = None
    profile_id: uuid.UUID | None = None
    display_width: int | None = None
    display_height: int | None = None


class AssignGroupRequest(BaseModel):
    """Request body for POST /profiles/{profile_id}/assign-group."""

    media_type: MediaType | None = None
    playlist_title: str | None = None


class AssignGroupResult(BaseModel):
    """Response for POST /profiles/{profile_id}/assign-group."""

    assigned: int
    already_assigned: int
    total_matched: int


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@router.get("/profiles", response_model=list[Profile])
async def list_profiles_endpoint(db: DBConn) -> list[Profile]:
    """List all profiles ordered by name.

    Args:
        db: Database connection (injected).

    Returns:
        All ``Profile`` instances.
    """
    return await list_profiles(db)


@router.post("/profiles", response_model=Profile, status_code=201)
async def create_profile_endpoint(body: ProfileCreate, db: DBConn) -> Profile:
    """Create a new profile.

    Args:
        body: Profile name.
        db: Database connection (injected).

    Returns:
        The created ``Profile`` with generated id and created_at.
    """
    profile = Profile(name=body.name)
    await create_profile(db, profile)
    # New profiles start in English. Profiles created before language was a
    # setting have none stored, which devices show as Spanish (see
    # ProfileSettings.language), so they keep looking the way they did.
    await set_profile_settings(
        db, profile.id, ProfileSettings(language=DEFAULT_LANGUAGE), datetime.now()
    )
    await db.commit()
    logger.info("Created profile %s: %r", profile.id, profile.name)
    return profile


@router.get("/profiles/{profile_id}", response_model=Profile)
async def get_profile_endpoint(profile_id: uuid.UUID, db: DBConn) -> Profile:
    """Fetch a single profile by ID.

    Args:
        profile_id: UUID of the profile.
        db: Database connection (injected).

    Returns:
        The ``Profile``.

    Raises:
        HTTPException: 404 if not found.
    """
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise _not_found("Profile")
    return profile


@router.get("/profiles/{profile_id}/settings", response_model=ProfileSettings)
async def get_profile_settings_endpoint(
    profile_id: uuid.UUID, db: DBConn
) -> ProfileSettings:
    """Fetch a profile's settings (defaults if never saved).

    Args:
        profile_id: UUID of the profile.
        db: Database connection (injected).

    Returns:
        The profile's ``ProfileSettings``.

    Raises:
        HTTPException: 404 if the profile does not exist.
    """
    if await get_profile(db, profile_id) is None:
        raise _not_found("Profile")
    return await get_profile_settings(db, profile_id)


@router.put("/profiles/{profile_id}/settings", response_model=ProfileSettings)
async def put_profile_settings_endpoint(
    profile_id: uuid.UUID, body: ProfileSettings, db: DBConn
) -> ProfileSettings:
    """Replace a profile's settings.

    The body is a complete ``ProfileSettings``; omitted fields take their
    defaults, except ``language`` and ``theme``: a body without a ``language``
    key keeps the stored language (a client that predates the field must not
    flip the child's device to Spanish) and likewise for ``theme``, while an
    explicit ``null`` clears it.
    Devices linked to the profile pick the change up at their next sync.

    Args:
        profile_id: UUID of the profile.
        body: The new settings.
        db: Database connection (injected).

    Returns:
        The stored settings.

    Raises:
        HTTPException: 404 if the profile does not exist, 422 if the body is
            invalid, ``language`` is not a supported language or ``theme`` is
            not a known theme.
    """
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise _not_found("Profile")
    if body.language is not None and body.language not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=422,
            detail={
                "detail": f"Unsupported language {body.language!r}; "
                f"use one of: {', '.join(SUPPORTED_LANGUAGES)}",
                "error_code": "UNSUPPORTED_LANGUAGE",
            },
        )
    if body.theme is not None and await resolve_theme(db, body.theme) is None:
        raise HTTPException(
            status_code=422,
            detail={
                "detail": f"Unknown theme {body.theme!r}; see GET /api/v1/themes",
                "error_code": "UNKNOWN_THEME",
            },
        )
    # A client that predates a field must not reset it by leaving it out.
    stored = await get_profile_settings(db, profile_id)
    keep = {
        name: getattr(stored, name)
        for name in ("language", "theme")
        if name not in body.model_fields_set
    }
    body = body.model_copy(update=keep)
    await set_profile_settings(db, profile_id, body, datetime.now())
    await db.commit()
    logger.info(
        "Updated settings of profile %s (%r): %s",
        profile_id,
        profile.name,
        body.model_dump_json(),
    )
    return body


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile_endpoint(profile_id: uuid.UUID, db: DBConn) -> Response:
    """Delete a profile.

    Fails with 409 if any devices are still linked to this profile.

    Args:
        profile_id: UUID of the profile.
        db: Database connection (injected).

    Returns:
        204 No Content.

    Raises:
        HTTPException: 404 if not found, 409 if devices are linked.
    """
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise _not_found("Profile")
    try:
        await delete_profile(db, profile_id)
        await db.commit()
    except aiosqlite.IntegrityError as exc:
        raise _conflict(
            "Cannot delete profile: devices are still linked to it.",
            "PROFILE_HAS_DEVICES",
        ) from exc
    logger.info("Deleted profile %s: %r", profile_id, profile.name)
    return Response(status_code=204)


@router.post(
    "/profiles/{profile_id}/assign-group",
    response_model=AssignGroupResult,
)
async def assign_group(
    profile_id: uuid.UUID,
    body: AssignGroupRequest,
    db: DBConn,
) -> AssignGroupResult:
    """Assign all media matching a filter to a profile.

    Useful for "give Leo all the Pica-Pica songs" in one call.

    Args:
        profile_id: UUID of the target profile.
        body: Filter criteria (media_type and/or playlist_title).
        db: Database connection (injected).

    Returns:
        Counts of newly assigned, already-assigned, and total-matched items.

    Raises:
        HTTPException: 404 if the profile is not found.
    """
    profile = await get_profile(db, profile_id)
    if profile is None:
        raise _not_found("Profile")

    items = await list_media_items(
        db,
        media_type=body.media_type,
        playlist_title=body.playlist_title,
        limit=10_000,
    )

    assigned = 0
    already_assigned = 0
    now = datetime.now()

    for item in items:
        async with db.execute(
            "SELECT 1 FROM profile_media WHERE profile_id = ? AND media_id = ?",
            (str(profile_id), str(item.id)),
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            already_assigned += 1
        else:
            await assign_media_to_profile(db, profile_id, item.id, now)
            assigned += 1

    await db.commit()
    logger.info(
        "assign-group profile=%s: assigned=%d already=%d total=%d",
        profile_id,
        assigned,
        already_assigned,
        len(items),
    )
    return AssignGroupResult(
        assigned=assigned,
        already_assigned=already_assigned,
        total_matched=len(items),
    )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@router.get("/devices", response_model=list[Device])
async def list_devices_endpoint(db: DBConn) -> list[Device]:
    """List all devices ordered by name.

    Args:
        db: Database connection (injected).

    Returns:
        All ``Device`` instances.
    """
    return await list_devices(db)


@router.post("/devices", response_model=Device, status_code=201)
async def create_device_endpoint(body: DeviceCreate, db: DBConn) -> Device:
    """Register a new device.

    A unique ``api_key`` (UUID hex string) is generated automatically.

    Args:
        body: Device name, profile_id, and optional display dimensions.
        db: Database connection (injected).

    Returns:
        The created ``Device`` including the generated ``api_key``.

    Raises:
        HTTPException: 404 if the referenced profile does not exist.
    """
    profile = await get_profile(db, body.profile_id)
    if profile is None:
        raise _not_found("Profile")

    device = Device(
        name=body.name,
        profile_id=body.profile_id,
        display_width=body.display_width,
        display_height=body.display_height,
    )
    await create_device(db, device)
    await db.commit()
    logger.info(
        "Registered device %s: %r profile=%s", device.id, device.name, body.profile_id
    )
    return device


@router.get("/devices/{device_id}", response_model=Device)
async def get_device_endpoint(device_id: uuid.UUID, db: DBConn) -> Device:
    """Fetch a single device by ID.

    Args:
        device_id: UUID of the device.
        db: Database connection (injected).

    Returns:
        The ``Device``.

    Raises:
        HTTPException: 404 if not found.
    """
    device = await get_device(db, device_id)
    if device is None:
        raise _not_found("Device")
    return device


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device_endpoint(device_id: uuid.UUID, db: DBConn) -> Response:
    """Unregister a device.

    Args:
        device_id: UUID of the device.
        db: Database connection (injected).

    Returns:
        204 No Content.

    Raises:
        HTTPException: 404 if not found.
    """
    device = await get_device(db, device_id)
    if device is None:
        raise _not_found("Device")
    await delete_device(db, device_id)
    await db.commit()
    logger.info("Unregistered device %s: %r", device_id, device.name)
    return Response(status_code=204)


@router.patch("/devices/{device_id}", response_model=Device)
async def patch_device(
    device_id: uuid.UUID,
    body: DevicePatch,
    db: DBConn,
) -> Device:
    """Partially update a device.

    Only provided (non-None) fields are applied; omitted fields keep their
    current values.

    Args:
        device_id: UUID of the device.
        body: Partial device fields to update.
        db: Database connection (injected).

    Returns:
        The updated ``Device``.

    Raises:
        HTTPException: 404 if device or referenced profile not found.
    """
    device = await get_device(db, device_id)
    if device is None:
        raise _not_found("Device")

    new_name = body.name if body.name is not None else device.name
    new_profile_id = (
        body.profile_id if body.profile_id is not None else device.profile_id
    )
    new_width = (
        body.display_width if body.display_width is not None else device.display_width
    )
    new_height = (
        body.display_height
        if body.display_height is not None
        else device.display_height
    )

    if body.profile_id is not None:
        profile = await get_profile(db, new_profile_id)
        if profile is None:
            raise _not_found("Profile")

    await update_device(
        db,
        device_id,
        name=new_name,
        profile_id=new_profile_id,
        display_width=new_width,
        display_height=new_height,
    )
    await db.commit()
    logger.debug(
        "Patched device %s: name=%r profile=%s", device_id, new_name, new_profile_id
    )

    updated = await get_device(db, device_id)
    assert updated is not None  # just committed
    return updated
