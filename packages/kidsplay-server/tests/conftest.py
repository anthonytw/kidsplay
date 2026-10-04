"""Shared fixtures for kidsplay-server tests."""

import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_server.auth import (
    create_admin_token,
    init_auth_db,
    set_initial_admin_password,
)
from kidsplay_server.database import configure_conn, init_db
from kidsplay_server.storage import MediaStore

_TEST_ADMIN_PASSWORD = "correct horse battery staple"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Yield a configured, initialised SQLite connection in a temp directory."""
    async with aiosqlite.connect(tmp_path / "test.db") as conn:
        await configure_conn(conn)
        await init_db(conn)
        yield conn


@pytest.fixture
def store(tmp_path: Path) -> MediaStore:
    """Return a MediaStore rooted in a temp directory."""
    return MediaStore(tmp_path / "media")


async def seed_admin(db_path: Path) -> str:
    """Set the admin password and create an admin API token in ``db_path``.

    Args:
        db_path: The app's SQLite database.

    Returns:
        The admin API token, for an ``Authorization: Bearer`` header.
    """
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await init_auth_db(conn)
        await set_initial_admin_password(conn, _TEST_ADMIN_PASSWORD)
        token = await create_admin_token(conn, "tests")
        await conn.commit()
    return token.token


@pytest.fixture
def admin_password() -> str:
    """The admin password that ``admin_headers`` sets."""
    return _TEST_ADMIN_PASSWORD


@pytest.fixture
async def admin_headers(app: FastAPI) -> dict[str, str]:
    """Seed the admin account for the test module's ``app``; return its headers.

    Sets the password to ``admin_password`` and creates an admin API token.
    Management endpoints require admin auth, so API test clients send these
    headers by default.
    """
    token = await seed_admin(app.state.db_path)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers_for() -> Callable[[FastAPI], Awaitable[dict[str, str]]]:
    """Return a function that seeds the admin account for any app.

    For tests with more than one app; ``admin_headers`` covers the module's
    ``app`` fixture.
    """

    async def seed(app: FastAPI) -> dict[str, str]:
        return {"Authorization": f"Bearer {await seed_admin(app.state.db_path)}"}

    return seed


@pytest.fixture
async def anon_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An httpx client for the test module's ``app`` that sends no credentials."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


MakeTone = Callable[..., Path]


@pytest.fixture
def clipped_master(tmp_path: Path) -> Path:
    """Write a loud, hard-clipped signal that needs peak limiting at -16 LUFS.

    Seeded white noise with periodic 9x bursts, hard-clipped at full scale: a
    high peak-to-loudness ratio built from clipped, broadband content, like the
    loud music masters of #61 whose MP3 output overshot the true-peak ceiling
    however often it was re-encoded. Requires ffmpeg on PATH.
    """
    source = tmp_path / "clipped-master.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
        + [
            "anoisesrc=d=15:c=white:r=44100:a=0.2:seed=1,volume=6dB,"
            "aeval='val(0)*(1+8*lt(mod(t,0.3),0.01))':c=same,"
            "aeval='clip(val(0),-1,1)':c=same",
            str(source),
        ],
        check=True,
    )
    return source


@pytest.fixture
def make_tone(tmp_path: Path) -> MakeTone:
    """Return a function that writes a test tone with ffmpeg.

    Loudness tests need real audio at known levels, generated per test so
    no audio files are committed. Requires ffmpeg on PATH (CI installs it).

    The returned function takes ``name``, ``volume_db`` (gain applied to
    ffmpeg's default sine, which measures about -21.75 LUFS at 0 dB), and
    optional ``seconds``, ``frequency``, ``sample_rate`` and ``spike``
    (add a short full-scale 3 kHz burst, so a plain gain cannot meet the
    peak ceiling). The file format follows the suffix of ``name``.
    """

    def make(
        name: str,
        volume_db: float,
        *,
        seconds: float = 5.0,
        frequency: int = 440,
        sample_rate: int = 44100,
        spike: bool = False,
    ) -> Path:
        path = tmp_path / name
        sine = f"sine=frequency={frequency}:sample_rate={sample_rate}"
        args = ["-f", "lavfi", "-i", f"{sine}:duration={seconds}"]
        if spike:
            burst = (
                f"sine=frequency=3000:sample_rate={sample_rate}:duration={seconds},"
                "volume=enable='between(t,1,1.05)':volume=8,"
                "volume=enable='not(between(t,1,1.05))':volume=0"
            )
            args += [
                "-f",
                "lavfi",
                "-i",
                burst,
                "-filter_complex",
                f"[0]volume={volume_db}dB[a];[a][1]amix=inputs=2:normalize=0",
            ]
        else:
            args += ["-af", f"volume={volume_db}dB"]
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
            + args
            + [str(path)],
            check=True,
        )
        return path

    return make
