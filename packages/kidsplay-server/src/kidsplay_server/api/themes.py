"""Theme endpoints (admin only).

Routes
------
GET    /themes                       — built-in and custom themes
GET    /themes/{theme_id}            — one theme
PUT    /themes/{theme_id}            — create or update a custom theme
DELETE /themes/{theme_id}            — delete a custom theme
PUT    /themes/{theme_id}/assets/{role}    — upload a background, font or sound
DELETE /themes/{theme_id}/assets/{role}    — remove one

Built-in themes cannot be changed or deleted. A profile chooses a theme with
``ProfileSettings.theme``; devices receive it in their manifest.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import NoReturn

import aiosqlite
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from kidsplay_models import (
    BUILTIN_THEMES_BY_ID,
    THEME_ID_PATTERN,
    ThemeAssetRole,
    ThemeColors,
    ThemeDefinition,
)
from kidsplay_server.database import (
    delete_custom_theme,
    delete_theme_asset,
    get_custom_theme,
    set_theme_asset,
    upsert_custom_theme,
)
from kidsplay_server.server_settings import load_server_settings
from kidsplay_server.theme_assets import (
    MAX_UPLOAD_BYTES,
    ThemeAssetError,
    kind_of,
    store_theme_asset,
)
from kidsplay_server.themes import list_themes, resolve_theme

from .deps import DBConn, Store

router = APIRouter(tags=["themes"])
logger = logging.getLogger(__name__)


class ThemeWrite(BaseModel):
    """Body of ``PUT /themes/{theme_id}``.

    Attributes:
        name: Display name.
        colors: The palette.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=40)
    colors: ThemeColors


def _error(status: int, message: str, code: str) -> NoReturn:
    raise HTTPException(
        status_code=status, detail={"detail": message, "error_code": code}
    )


def _check_id(theme_id: str) -> None:
    if not re.match(THEME_ID_PATTERN, theme_id):
        _error(
            422,
            "A theme id is 1-40 lowercase letters, digits and hyphens",
            "INVALID_THEME_ID",
        )
    if theme_id in BUILTIN_THEMES_BY_ID:
        _error(409, "Built-in themes cannot be changed", "THEME_BUILTIN")


async def _custom_or_404(db: aiosqlite.Connection, theme_id: str) -> ThemeDefinition:
    theme = await get_custom_theme(db, theme_id)
    if theme is None:
        _error(404, "Theme not found", "NOT_FOUND")
    return theme


@router.get("/themes", response_model=list[ThemeDefinition])
async def list_themes_endpoint(db: DBConn) -> list[ThemeDefinition]:
    """List every theme a profile can choose.

    Args:
        db: Database connection (injected).

    Returns:
        The built-in themes, then the custom ones by name.
    """
    return await list_themes(db)


@router.get("/themes/{theme_id}", response_model=ThemeDefinition)
async def get_theme_endpoint(theme_id: str, db: DBConn) -> ThemeDefinition:
    """Fetch one theme.

    Args:
        theme_id: The theme's id.
        db: Database connection (injected).

    Returns:
        The theme.

    Raises:
        HTTPException: 404 if there is no such theme.
    """
    theme = await resolve_theme(db, theme_id)
    if theme is None:
        _error(404, "Theme not found", "NOT_FOUND")
    return theme


@router.put("/themes/{theme_id}", response_model=ThemeDefinition)
async def put_theme_endpoint(
    theme_id: str, body: ThemeWrite, db: DBConn
) -> ThemeDefinition:
    """Create a custom theme, or replace the name and palette of one.

    Assets already uploaded to the theme are kept. Devices pick a change up at
    their next sync.

    Args:
        theme_id: Id for the theme (lowercase letters, digits, hyphens).
        body: Name and palette.
        db: Database connection (injected).

    Returns:
        The stored theme.

    Raises:
        HTTPException: 409 for a built-in id, 422 for an invalid id or color.
    """
    _check_id(theme_id)
    await upsert_custom_theme(db, theme_id, body.name, body.colors, datetime.now())
    await db.commit()
    logger.info("Saved theme %s (%r)", theme_id, body.name)
    return await _custom_or_404(db, theme_id)


@router.delete("/themes/{theme_id}", status_code=204)
async def delete_theme_endpoint(theme_id: str, db: DBConn) -> Response:
    """Delete a custom theme.

    Profiles that had chosen it show the default theme from their devices'
    next sync on. The asset files stay in the media store, as all store files
    do.

    Args:
        theme_id: The theme's id.
        db: Database connection (injected).

    Returns:
        204 No Content.

    Raises:
        HTTPException: 409 for a built-in theme, 404 if there is no such theme.
    """
    if theme_id in BUILTIN_THEMES_BY_ID:
        _error(409, "Built-in themes cannot be deleted", "THEME_BUILTIN")
    if not await delete_custom_theme(db, theme_id):
        _error(404, "Theme not found", "NOT_FOUND")
    await db.commit()
    logger.info("Deleted theme %s", theme_id)
    return Response(status_code=204)


@router.put("/themes/{theme_id}/assets/{role}", response_model=ThemeDefinition)
async def put_theme_asset_endpoint(
    theme_id: str, role: ThemeAssetRole, file: UploadFile, db: DBConn, store: Store
) -> ThemeDefinition:
    """Upload a theme's background image, font or UI sound.

    The file is validated (and a background re-encoded) before it is stored;
    see ``kidsplay_server.theme_assets`` for the rules. An asset of the same
    role is replaced; the old file stays in the store.

    Args:
        theme_id: The custom theme's id.
        role: ``background``, ``home_background``, ``font`` or a ``sound_*``.
        file: The upload (multipart/form-data).
        db: Database connection (injected).
        store: Media store (injected).

    Returns:
        The theme with its assets.

    Raises:
        HTTPException: 404 if there is no such custom theme, 422 if the file
            is not a usable one for that role.
    """
    _check_id(theme_id)
    await _custom_or_404(db, theme_id)
    # Read one byte past the limit so an oversized upload is refused without
    # buffering all of it.
    data = await file.read(MAX_UPLOAD_BYTES[kind_of(role)] + 1)
    quality = (await load_server_settings(db)).values.webp_quality
    try:
        # Pillow, FreeType, mutagen and the store copy all block: keep them off
        # the event loop so one upload does not stall every other request.
        asset, mime = await asyncio.to_thread(
            store_theme_asset, role, data, store, quality
        )
    except ThemeAssetError as exc:
        _error(422, str(exc), "INVALID_THEME_ASSET")
    await set_theme_asset(db, theme_id, asset, mime)
    await db.commit()
    logger.info("Theme %s: stored %s (%s)", theme_id, role.value, asset.content_hash)
    return await _custom_or_404(db, theme_id)


@router.delete("/themes/{theme_id}/assets/{role}", response_model=ThemeDefinition)
async def delete_theme_asset_endpoint(
    theme_id: str, role: ThemeAssetRole, db: DBConn
) -> ThemeDefinition:
    """Remove one asset from a custom theme (the file stays in the store).

    Args:
        theme_id: The custom theme's id.
        role: Which asset.
        db: Database connection (injected).

    Returns:
        The theme without it.

    Raises:
        HTTPException: 404 if the theme or the asset does not exist.
    """
    _check_id(theme_id)
    await _custom_or_404(db, theme_id)
    if not await delete_theme_asset(db, theme_id, role):
        _error(404, "The theme has no such asset", "NOT_FOUND")
    await db.commit()
    return await _custom_or_404(db, theme_id)
