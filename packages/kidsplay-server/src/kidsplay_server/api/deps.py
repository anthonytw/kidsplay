"""Shared FastAPI dependency functions.

Import ``DBConn``, ``DBPath``, ``Store``, ``Importers`` and
``Backfill`` in route modules and use them
as ``Annotated`` type hints so FastAPI injects the right values per request.

Example usage in a route::

    from kidsplay_server.api.deps import DBConn, Store

    @router.get("/items")
    async def list_items(db: DBConn, store: Store) -> list[Item]:
        return await get_items(db)
"""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated

import aiosqlite
from fastapi import Depends, Header, HTTPException, Request

from kidsplay_models.device import Device
from kidsplay_server.auth import init_auth_db
from kidsplay_server.database import configure_conn, get_device_by_api_key, init_db
from kidsplay_server.importers import ImporterRegistry
from kidsplay_server.processing.loudness_backfill import LoudnessBackfill
from kidsplay_server.storage import MediaStore


async def get_db(request: Request) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Yield a configured aiosqlite connection for the duration of the request.

    Opens a fresh connection from ``app.state.db_path``, configures it (foreign
    keys on, row factory), and ensures the schema exists via ``init_db`` and
    ``init_auth_db`` (both idempotent — ``CREATE TABLE IF NOT EXISTS``). The
    caller must commit explicitly; the connection is closed when the request
    completes.

    Args:
        request: FastAPI request object (injected by the framework).

    Yields:
        Configured aiosqlite connection.
    """
    db_path: Path = request.app.state.db_path
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await init_auth_db(conn)
        yield conn


def get_db_path(request: Request) -> Path:
    """Return the raw database path from app state.

    Used by endpoints that delegate to pipeline functions (``ingest_file``,
    ``ingest_directory``) which manage their own connections.

    Args:
        request: FastAPI request object (injected by the framework).

    Returns:
        Path to the SQLite database file.
    """
    db_path: Path = request.app.state.db_path
    return db_path


async def get_authenticated_device(
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> Device:
    """Resolve and authenticate the calling device from its Bearer token.

    Device-facing sync endpoints require ``Authorization: Bearer {api_key}``
    (see ``docs/API.md``). The token is matched against the ``devices`` table.

    Args:
        db: Database connection (injected).
        authorization: Raw ``Authorization`` header value.

    Returns:
        The authenticated ``Device``.

    Raises:
        HTTPException: 401 if the header is missing/malformed or the key is
            not recognised.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "detail": "Missing or malformed Authorization header",
                "error_code": "UNAUTHORIZED",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    api_key = authorization.removeprefix("Bearer ").strip()
    device = await get_device_by_api_key(db, api_key)
    if device is None:
        raise HTTPException(
            status_code=401,
            detail={"detail": "Invalid API key", "error_code": "UNAUTHORIZED"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return device


def get_store(request: Request) -> MediaStore:
    """Return the shared MediaStore from app state.

    Args:
        request: FastAPI request object (injected by the framework).

    Returns:
        The application-level MediaStore.
    """
    store: MediaStore = request.app.state.media_store
    return store


def get_importers(request: Request) -> ImporterRegistry:
    """Return the importers installed for this app.

    Args:
        request: FastAPI request object (injected by the framework).

    Returns:
        The application-level ImporterRegistry.
    """
    registry: ImporterRegistry = request.app.state.importers
    return registry


# Annotated type aliases for use as function parameter types in route handlers.
def get_backfill(request: Request) -> LoudnessBackfill:
    """Return the app's loudness backfill runner.

    Args:
        request: FastAPI request object (injected by the framework).

    Returns:
        The application-level LoudnessBackfill.
    """
    backfill: LoudnessBackfill = request.app.state.loudness_backfill
    return backfill


DBConn = Annotated[aiosqlite.Connection, Depends(get_db)]
DBPath = Annotated[Path, Depends(get_db_path)]
Store = Annotated[MediaStore, Depends(get_store)]
Importers = Annotated[ImporterRegistry, Depends(get_importers)]
Backfill = Annotated[LoudnessBackfill, Depends(get_backfill)]
AuthDevice = Annotated[Device, Depends(get_authenticated_device)]
