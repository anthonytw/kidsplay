"""Server settings endpoints.

Routes
------
GET /server-settings  — effective server settings and which are env-locked
PUT /server-settings  — save or reset settings that are not env-locked
"""

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from kidsplay_server.server_settings import (
    ENV_VARS,
    EnvLockedError,
    ResolvedServerSettings,
    ServerSettings,
    ServerSettingsUpdate,
    load_server_settings,
    update_server_settings,
)

from .deps import DBConn

if TYPE_CHECKING:
    from kidsplay_server.discovery import AdvertisingSwitch

router = APIRouter(tags=["settings"])
logger = logging.getLogger(__name__)


class ServerSettingsView(BaseModel):
    """Response for the server settings endpoints.

    Attributes:
        values: Effective settings.
        env_locked: Keys pinned by an environment variable; they cannot be
            changed from the API or the web UI.
        env_vars: Environment variable for each key.
    """

    values: ServerSettings
    env_locked: list[str]
    env_vars: dict[str, str]


def _view(resolved: ResolvedServerSettings) -> ServerSettingsView:
    return ServerSettingsView(
        values=resolved.values,
        env_locked=sorted(resolved.env_locked),
        env_vars=dict(ENV_VARS),
    )


@router.get("/server-settings", response_model=ServerSettingsView)
async def get_server_settings_endpoint(db: DBConn) -> ServerSettingsView:
    """Return the effective server settings.

    Args:
        db: Database connection (injected).

    Returns:
        Effective values and which keys are env-locked.
    """
    return _view(await load_server_settings(db))


@router.put("/server-settings", response_model=ServerSettingsView)
async def put_server_settings_endpoint(
    body: ServerSettingsUpdate, db: DBConn, request: Request
) -> ServerSettingsView:
    """Save or reset server settings.

    Args:
        body: Values to save (omitted or null: unchanged) and keys to reset.
        db: Database connection (injected).
        request: Current request (for the mDNS advertiser).

    Returns:
        The effective settings after the change.

    Raises:
        HTTPException: 409 if a key is env-locked, 422 if a value is invalid.
    """
    try:
        resolved = await update_server_settings(db, body)
    except EnvLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"detail": str(exc), "error_code": "ENV_LOCKED"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"detail": str(exc), "error_code": "INVALID_SETTING"},
        ) from exc
    await db.commit()
    logger.info("Updated server settings: %s", resolved.values.model_dump_json())
    # Handhelds look for the server only to pair: announce it only while
    # pairing is allowed. (Absent when the server was started without mDNS.)
    advertising: AdvertisingSwitch | None = getattr(
        request.app.state, "advertising", None
    )
    if advertising is not None:
        await advertising.set_enabled(resolved.values.pairing_enabled)
    return _view(resolved)
