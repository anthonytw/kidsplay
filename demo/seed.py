"""Start a throwaway KidsPlay server and fill it with the bundled sample media.

Shared by ``python -m demo`` (the interactive demo) and ``demo.screenshots``.
Everything goes through the server's public REST API, exactly as the CLI or
web UI would, so the demo exercises the real ingest and sync paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from demo.quiet_server import QUEUE_POLL_ENV
from kidsplay_device.config import DeviceConfig
from kidsplay_device.sync import SyncClient

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = Path(__file__).parent / "media"

# The demo server is throwaway, bound to 127.0.0.1 and deleted on exit, so a
# fixed admin password is fine; the demo prints it for logging in to the web UI.
DEMO_ADMIN_PASSWORD = "kidsplay-demo"

PROFILES = ("Ada", "Leo")
DEVICE_NAME = "Ada's handheld"


@dataclass(frozen=True)
class Sample:
    """One directory of bundled media and who it is assigned to.

    Attributes:
        path: Directory relative to ``demo/media/``.
        media_type: ``music``, ``audiobook`` or ``photo``.
        playlist_title: Group shown in the web UI and on the device.
        profiles: Names (from ``PROFILES``) the media is assigned to.
    """

    path: str
    media_type: str
    playlist_title: str
    profiles: tuple[str, ...]


SAMPLES: tuple[Sample, ...] = (
    Sample("music/Nursery Tunes", "music", "Nursery Tunes", ("Ada", "Leo")),
    Sample("music/Bedtime Classics", "music", "Bedtime Classics", ("Ada",)),
    Sample("audiobooks/Aesop's Fables", "audiobook", "Aesop's Fables", ("Ada", "Leo")),
    Sample("photos/Day Trips", "photo", "Day Trips", ("Ada",)),
    Sample("photos/Backyard", "photo", "Backyard", ("Ada", "Leo")),
)


@dataclass(frozen=True)
class DemoDevice:
    """The registered demo device.

    Attributes:
        device_id: Server-assigned device UUID.
        api_key: The device's sync bearer token.
        profile_ids: Profile name to UUID, for every created profile.
    """

    device_id: str
    api_key: str
    profile_ids: dict[str, str]


def free_port() -> int:
    """Ask the OS for a free TCP port on localhost.

    Returns:
        A port number that was free a moment ago.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def server_env(data_dir: Path) -> dict[str, str]:
    """Environment for a server whose state lives entirely in ``data_dir``.

    Args:
        data_dir: Scratch directory for the database and media store.

    Returns:
        A copy of ``os.environ`` with the ``KIDSPLAY_*`` paths overridden
        and file logging turned off.
    """
    env = dict(os.environ)
    env["KIDSPLAY_DB_PATH"] = str(data_dir / "server" / "db.sqlite")
    env["KIDSPLAY_MEDIA_STORE"] = str(data_dir / "server" / "media")
    env["KIDSPLAY_LOG_FILE"] = ""
    # Auth on, with a known password, over plain HTTP on localhost; ignore any
    # KIDSPLAY_AUTH / KIDSPLAY_COOKIE_SECURE set for a real server.
    env["KIDSPLAY_AUTH"] = "enabled"
    env["KIDSPLAY_ADMIN_PASSWORD"] = DEMO_ADMIN_PASSWORD
    env.pop("KIDSPLAY_COOKIE_SECURE", None)
    env.pop("KIDSPLAY_SECRET_KEY_FILE", None)
    return env


def port_is_free(port: int) -> bool:
    """Return whether nothing is listening on ``127.0.0.1:port``.

    Args:
        port: TCP port to check.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_for_server(
    base_url: str, proc: subprocess.Popen[bytes], timeout: float = 60.0
) -> None:
    """Block until the server answers HTTP, or fail.

    Args:
        base_url: Server root, e.g. ``http://127.0.0.1:8000``.
        proc: The server process, checked so a crash fails fast.
        timeout: Seconds to wait before giving up.

    Raises:
        RuntimeError: If the process exits or the timeout passes.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        with contextlib.suppress(httpx.HTTPError):
            # Any HTTP answer means uvicorn is up and the lifespan has run.
            # The port was free before we started, but check our process is
            # still the one alive (it exits if it could not bind).
            httpx.get(f"{base_url}/static/htmx.min.js", timeout=2.0)
            if proc.poll() is None:
                return
        time.sleep(0.2)
    raise RuntimeError(f"server did not start within {timeout:.0f}s")


@contextlib.contextmanager
def running_server(
    data_dir: Path,
    port: int | None = None,
    *,
    quiet: bool = True,
    queue_poll: float | None = None,
) -> Iterator[str]:
    """Run ``uvicorn`` for the KidsPlay server in a child process.

    Args:
        data_dir: Scratch directory for all server state.
        port: Port to listen on (localhost only); a free one if ``None``.
        quiet: Only print warnings and errors on the server's console: uvicorn's
            and the app's own (:mod:`demo.quiet_server`).
        queue_poll: Seconds between the queue worker's looks for new items,
            instead of the server's default. For tests that wait on a queued
            link (it does not wake the worker); needs ``quiet``.

    Yields:
        The server's base URL.
    """
    if port is None:
        port = free_port()
    elif not port_is_free(port):
        # Never seed whatever else is listening there (possibly a real server).
        raise RuntimeError(f"port {port} is already in use; pick another --port")
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "demo.quiet_server:create_app"
        if quiet
        else "kidsplay_server.api.app:create_app_from_env",
        "--factory",
        "--app-dir",
        str(REPO_ROOT),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if quiet:
        cmd += ["--log-level", "warning"]
    env = server_env(data_dir)
    if queue_poll is not None:
        env[QUEUE_POLL_ENV] = str(queue_poll)
    proc = subprocess.Popen(cmd, env=env)
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_server(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _check(response: httpx.Response) -> dict:
    """Raise with the server's error body on failure, else return the JSON."""
    if response.is_error:
        raise RuntimeError(
            f"{response.request.method} {response.request.url} -> "
            f"{response.status_code}: {response.text}"
        )
    return response.json()


def login(client: httpx.Client, password: str = DEMO_ADMIN_PASSWORD) -> None:
    """Get an admin API token and send it on every later request of ``client``.

    Args:
        client: Client whose ``base_url`` is the server root.
        password: The server's admin password.

    Raises:
        RuntimeError: If the login fails.
    """
    body = _check(
        client.post(
            "/api/v1/auth/login", json={"password": password, "token_name": "demo"}
        )
    )
    client.headers["Authorization"] = f"Bearer {body['token']}"


def wait_for_normalization(client: httpx.Client, timeout: float = 600.0) -> None:
    """Wait until the server has loudness-normalized what was just ingested.

    Ingest returns as soon as the files are stored; the normalization runs
    afterwards on the server's queue. Waiting keeps the demo the way it was
    (the device syncs the normalized audio, not the originals).

    Args:
        client: Logged-in client for the server.
        timeout: Seconds to wait before giving up.

    Raises:
        RuntimeError: If normalization is still running after ``timeout``, or
            an item could not be normalized.
    """
    deadline = time.monotonic() + timeout
    while True:
        status = _check(client.get("/api/v1/media/normalize"))
        if not status["running"]:
            break
        if time.monotonic() > deadline:
            raise RuntimeError(f"loudness normalization did not finish: {status}")
        time.sleep(0.25)
    if status["failed"]:
        raise RuntimeError(f"loudness normalization failed: {status['errors']}")


def seed(client: httpx.Client, media_dir: Path = MEDIA_DIR) -> DemoDevice:
    """Create the demo profiles, ingest the samples and register a device.

    Args:
        client: Client whose ``base_url`` is the server root.
        media_dir: Root of the sample media (``demo/media/``).

    Returns:
        The registered device and the profile IDs.

    Raises:
        RuntimeError: If any API call or ingest fails.
    """
    profile_ids: dict[str, str] = {}
    for name in PROFILES:
        body = _check(client.post("/api/v1/profiles", json={"name": name}))
        profile_ids[name] = body["id"]

    for sample in SAMPLES:
        result = _check(
            client.post(
                "/api/v1/media/ingest",
                json={
                    "source_path": str((media_dir / sample.path).resolve()),
                    "media_type": sample.media_type,
                    "playlist_title": sample.playlist_title,
                    "profile_ids": [profile_ids[p] for p in sample.profiles],
                },
            )
        )
        if result["failed"] or not result["successful"]:
            raise RuntimeError(f"ingest of {sample.path} failed: {result}")

    wait_for_normalization(client)

    device = _check(
        client.post(
            "/api/v1/devices",
            json={"name": DEVICE_NAME, "profile_id": profile_ids[PROFILES[0]]},
        )
    )
    return DemoDevice(
        device_id=device["id"], api_key=device["api_key"], profile_ids=profile_ids
    )


@contextlib.contextmanager
def stalled_source() -> Iterator[str]:
    """Serve a URL that accepts a connection and never answers.

    An import from it stays "running" (the downloader waits five minutes), so
    the queue page has a running item, and whatever is queued behind it stays
    pending. The listener lives until the ``with`` block ends.

    Yields:
        A ``http://127.0.0.1:<port>/...`` URL.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept_and_hold() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            with contextlib.suppress(OSError):
                held.append(listener.accept()[0])

    thread = threading.Thread(target=accept_and_hold, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/slow-song.mp3"
    finally:
        stop.set()
        thread.join()
        for conn in held:
            conn.close()
        listener.close()


def _wait_for_queue_status(
    client: httpx.Client, item_id: str, status: str, timeout: float = 60.0
) -> None:
    """Poll a queue item until it has ``status``.

    Raises:
        RuntimeError: If it does not get there within ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    while True:
        item = _check(client.get(f"/api/v1/queue/{item_id}"))
        if item["status"] == status:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"queue item is {item['status']}, not {status}: {item}")
        time.sleep(0.2)


def seed_queue(client: httpx.Client, stalled_url: str) -> None:
    """Fill the import queue with a failed, a running and a pending item.

    Not part of :func:`seed`: the README screenshots and the interactive demo
    keep their queue empty. The failed item points at a closed port; the
    running and pending ones at ``stalled_source``.

    Args:
        client: Logged-in client for the server.
        stalled_url: URL from :func:`stalled_source`.

    Raises:
        RuntimeError: If an item does not reach the state it is seeded for.
    """
    closed = free_port()
    failed = _check(
        client.post(
            "/api/v1/queue",
            json={
                "url": f"http://127.0.0.1:{closed}/missing-song.mp3",
                "importer": "http",
                "media_type": "music",
                "playlist_title": "Road Trip",
                "max_retries": 0,
            },
        )
    )
    _wait_for_queue_status(client, failed["id"], "failed")
    running = _check(
        client.post(
            "/api/v1/queue",
            json={
                "url": stalled_url,
                "importer": "http",
                "media_type": "music",
                "playlist_title": "Road Trip",
                "max_retries": 0,
            },
        )
    )
    _wait_for_queue_status(client, running["id"], "running")
    _check(
        client.post(
            "/api/v1/queue",
            json={
                "url": stalled_url + "?second",
                "importer": "http",
                "media_type": "audiobook",
                "playlist_title": "Story Time",
                "max_retries": 0,
            },
        )
    )


def device_config(base_url: str, device: DemoDevice, home: Path) -> DeviceConfig:
    """Build a windowed device config that keeps all state under ``home``.

    Args:
        base_url: Server root URL.
        device: The registered demo device.
        home: Directory that stands in for the device user's home.

    Returns:
        The device configuration (not yet written to disk).
    """
    return DeviceConfig(
        server_url=base_url,
        device_id=device.device_id,
        api_key=device.api_key,
        media_root=home / ".kidsplay" / "media",
        db_path=home / ".kidsplay" / "db.sqlite",
        sync_interval_seconds=60,
        fullscreen=False,
        # The committed README screenshots show the Spanish screens. New
        # profiles are English, so pin the demo device with the local override.
        language="es",
    )


def write_device_home(config: DeviceConfig, home: Path) -> Path:
    """Write ``config.json`` where ``kidsplay-player`` looks for it.

    The player reads ``~/.kidsplay/config.json`` and ``~/.kidsplay/settings.json``,
    so running it with ``HOME=home`` keeps the demo away from any real device
    config on this machine.

    Args:
        config: The device configuration.
        home: The stand-in home directory.

    Returns:
        Path of the written ``config.json``.
    """
    path = home / ".kidsplay" / "config.json"
    config.save(path)
    return path


def sync_device(
    config: DeviceConfig, http_client: httpx.AsyncClient | None = None
) -> int:
    """Run one sync cycle so the player opens with a full library.

    Args:
        config: The device configuration.
        http_client: Client to sync through (tests pass an ASGI one); by
            default the device's own client talks to ``config.server_url``.

    Returns:
        How many media files are on the device afterwards.
    """
    asyncio.run(SyncClient(config, http_client=http_client).sync())
    if not config.media_root.exists():
        return 0
    return sum(1 for p in config.media_root.rglob("*") if p.is_file())
