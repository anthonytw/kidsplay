"""Shared fixtures for kidsplay-importer-ytdlp tests."""

from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI

from kidsplay_server.auth import (
    create_admin_token,
    init_auth_db,
    set_initial_admin_password,
)
from kidsplay_server.database import configure_conn, init_db


async def seed_admin(db_path: Path) -> str:
    """Set up the server's admin account and return an admin API token."""
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await init_auth_db(conn)
        await set_initial_admin_password(conn, "ytdlp-tests-password")
        token = await create_admin_token(conn, "ytdlp-tests")
        await conn.commit()
    return token.token


@pytest.fixture
async def admin_headers(app: FastAPI) -> dict[str, str]:
    """Admin ``Authorization`` headers for the test module's ``app``."""
    return {"Authorization": f"Bearer {await seed_admin(app.state.db_path)}"}
