"""Shared fixtures for kidsplay-cli tests.

Strategy: spin up the real FastAPI app in a background thread using uvicorn
so the CLI's httpx client can hit a real HTTP server on a random port.  This
exercises the full stack — CLI → HTTP → FastAPI → SQLite — without any mocking.

The server has admin auth enabled with a pre-seeded password. A token is
obtained once per session through ``POST /api/v1/auth/login`` and handed to
every test via ``KIDSPLAY_TOKEN``, exactly as a user would configure it.
"""

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from rich.console import Console

from kidsplay_cli import auth, devices, importers, media
from kidsplay_server.api.app import create_app
from kidsplay_server.auth import AuthConfig

ADMIN_PASSWORD = "cli-test-admin-password"


@pytest.fixture(autouse=True)
def consoles_follow_the_test_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test fresh module consoles that read ``COLUMNS`` when printing.

    A rich ``Console`` built while ``COLUMNS`` is set keeps that width for good.
    The CLI's consoles are built at import, so a ``COLUMNS`` already in the test
    process's environment (pytest-xdist workers on CI have one) froze them at 80
    and cut the names the tests look for. Without it, they take the
    ``COLUMNS=200`` each test passes to ``CliRunner``.
    """
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)
    for module in (auth, devices, importers, media):
        monkeypatch.setattr(module, "console", Console())
    monkeypatch.setattr(devices, "err_console", Console(stderr=True))


class _Server(threading.Thread):
    """Uvicorn server running in a daemon thread."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        super().__init__(daemon=True)
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)

    def run(self) -> None:
        self._server.run()

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="session")
def server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start a real uvicorn server and return its base URL.

    Session-scoped: one server for the entire test session, sharing a single
    SQLite DB and media store in a temporary directory.
    """
    base = tmp_path_factory.mktemp("server")
    app = create_app(
        base / "test.db", base / "media", AuthConfig(admin_password=ADMIN_PASSWORD)
    )

    # Pick an ephemeral port.
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    srv = _Server(app, "127.0.0.1", port)
    srv.start()

    # Wait until the server is accepting connections.
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/login", timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("Test server did not start in time")

    yield url

    srv.stop()


@pytest.fixture(scope="session")
def admin_password() -> str:
    """The admin password the test server was seeded with."""
    return ADMIN_PASSWORD


@pytest.fixture(scope="session")
def admin_token(server_url: str) -> str:
    """An admin API token for the test server, created via the login API."""
    r = httpx.post(
        f"{server_url}/api/v1/auth/login",
        json={"password": ADMIN_PASSWORD, "token_name": "cli-tests"},
        timeout=10,
    )
    r.raise_for_status()
    token: str = r.json()["token"]
    return token


@pytest.fixture(autouse=True)
def _cli_auth_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, admin_token: str
) -> None:
    """Authenticate CLI invocations and keep credentials out of ~/.config."""
    monkeypatch.setenv("KIDSPLAY_TOKEN", admin_token)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
