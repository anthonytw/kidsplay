"""Tests for ``demo/seed.py``.

``seed`` and ``sync_device`` run against the real FastAPI app in-process
(``TestClient`` / ``ASGITransport``) with the real bundled media, so these
also prove the sample files ingest cleanly. No server process is started.
"""

import socket
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from demo import seed as seed_mod
from kidsplay_device.config import DeviceConfig
from kidsplay_server.api.app import create_app
from kidsplay_server.auth import AuthConfig
from kidsplay_server.processing import queue_worker


def _app(tmp_path: Path) -> FastAPI:
    """An app whose state lives in tmp_path, with the demo's admin password."""
    return create_app(
        tmp_path / "server.db",
        tmp_path / "server_media",
        AuthConfig(admin_password=seed_mod.DEMO_ADMIN_PASSWORD),
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A sync client, logged in as the demo admin."""
    with TestClient(_app(tmp_path)) as c:
        seed_mod.login(c)
        yield c


def test_seed_needs_login(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as c, pytest.raises(RuntimeError, match="401"):
        seed_mod.seed(c)


def test_login_rejects_wrong_password(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as c, pytest.raises(RuntimeError, match="401"):
        seed_mod.login(c, "not-the-password")


def test_server_env_turns_auth_on_with_demo_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIDSPLAY_AUTH", "disabled")
    monkeypatch.setenv("KIDSPLAY_COOKIE_SECURE", "1")
    env = seed_mod.server_env(tmp_path)
    assert env["KIDSPLAY_AUTH"] == "enabled"
    assert env["KIDSPLAY_ADMIN_PASSWORD"] == seed_mod.DEMO_ADMIN_PASSWORD
    assert "KIDSPLAY_COOKIE_SECURE" not in env


def test_running_server_refuses_a_port_in_use(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert not seed_mod.port_is_free(port)
        with (
            pytest.raises(RuntimeError, match="already in use"),
            seed_mod.running_server(tmp_path, port),
        ):
            pytest.fail("must not start against another listener")


def test_seed_creates_profiles_media_and_device(client: TestClient) -> None:
    device = seed_mod.seed(client)

    profiles = {p["name"]: p["id"] for p in client.get("/api/v1/profiles").json()}
    assert profiles == device.profile_ids
    assert set(profiles) == set(seed_mod.PROFILES)

    media = client.get("/api/v1/media").json()
    assert len(media) == 13
    assert {m["playlist_title"] for m in media} == {
        s.playlist_title for s in seed_mod.SAMPLES
    }

    registered = client.get(f"/api/v1/devices/{device.device_id}").json()
    assert registered["name"] == seed_mod.DEVICE_NAME
    assert registered["profile_id"] == device.profile_ids["Ada"]
    assert registered["api_key"] == device.api_key


def test_seed_assigns_per_profile(client: TestClient) -> None:
    device = seed_mod.seed(client)
    leo = client.get(
        "/api/v1/media", params={"profile_id": device.profile_ids["Leo"]}
    ).json()
    assert {m["playlist_title"] for m in leo} == {
        "Nursery Tunes",
        "Aesop's Fables",
        "Backyard",
    }


def test_seed_returns_after_the_audio_is_normalized(client: TestClient) -> None:
    """Ingest only queues the normalization; seed waits for it, so the device
    syncs the normalized audio."""
    seed_mod.seed(client)
    status = client.get("/api/v1/media/normalize").json()
    assert status["running"] is False
    assert status["failed"] == 0
    assert status["normalized"] > 0
    audio = [
        m
        for m in client.get("/api/v1/media").json()
        if m["media_type"] in ("music", "audiobook")
    ]
    assert audio and all(m["loudness_target_lufs"] is not None for m in audio)


def test_wait_for_normalization_gives_up() -> None:
    running = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"running": True, "failed": 0})
        ),
        base_url="http://x",
    )
    with pytest.raises(RuntimeError, match="did not finish"):
        seed_mod.wait_for_normalization(running, timeout=0.3)


def test_wait_for_normalization_reports_failures() -> None:
    failed = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"running": False, "failed": 1, "errors": ["x: boom"]}
            )
        ),
        base_url="http://x",
    )
    with pytest.raises(RuntimeError, match="x: boom"):
        seed_mod.wait_for_normalization(failed)


def test_seed_raises_on_api_error() -> None:
    failing = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, text="boom")),
        base_url="http://x",
    )
    with pytest.raises(RuntimeError, match="500: boom"):
        seed_mod.seed(failing)


def test_seed_twice_fails_instead_of_duplicating(client: TestClient) -> None:
    seed_mod.seed(client)
    # The server deduplicates by content hash, so nothing new is ingested.
    with pytest.raises(RuntimeError, match="ingest of"):
        seed_mod.seed(client)


def test_seed_raises_when_ingest_fails(client: TestClient, tmp_path: Path) -> None:
    empty = tmp_path / "empty-media"
    for sample in seed_mod.SAMPLES:
        (empty / sample.path).mkdir(parents=True)
    with pytest.raises(RuntimeError, match="ingest of"):
        seed_mod.seed(client, empty)


def test_device_config_keeps_state_under_home(tmp_path: Path) -> None:
    device = seed_mod.DemoDevice("dev-1", "key-1", {"Ada": "p1"})
    config = seed_mod.device_config("http://x:1", device, tmp_path)
    assert config.server_url == "http://x:1"
    assert config.device_id == "dev-1"
    assert config.api_key == "key-1"
    assert config.fullscreen is False
    assert tmp_path in config.media_root.parents
    assert tmp_path in config.db_path.parents


def test_write_device_home_is_loadable(tmp_path: Path) -> None:
    device = seed_mod.DemoDevice("dev-1", "key-1", {})
    config = seed_mod.device_config("http://x:1", device, tmp_path)
    path = seed_mod.write_device_home(config, tmp_path)
    assert path == tmp_path / ".kidsplay" / "config.json"
    assert DeviceConfig.load(path) == config


def test_sync_device_downloads_the_library(client: TestClient, tmp_path: Path) -> None:
    device = seed_mod.seed(client)
    config = seed_mod.device_config("http://testserver", device, tmp_path / "home")
    transport = httpx.ASGITransport(app=client.app)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    count = seed_mod.sync_device(config, http_client)
    # 13 items: 7 audio files + 6 photos, each with 3 thumbnails.
    assert count == 7 + 6 + 13 * 3


def test_sync_device_with_unreachable_server_is_empty(tmp_path: Path) -> None:
    device = seed_mod.DemoDevice("dev-1", "key-1", {})
    config = seed_mod.device_config("http://unreachable", device, tmp_path)
    failing = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )
    assert seed_mod.sync_device(config, failing) == 0


def test_free_port_is_bindable() -> None:
    port = seed_mod.free_port()
    assert 0 < port < 65536


def test_server_env_points_state_at_data_dir(tmp_path: Path) -> None:
    env = seed_mod.server_env(tmp_path)
    assert Path(env["KIDSPLAY_DB_PATH"]).is_relative_to(tmp_path)
    assert Path(env["KIDSPLAY_MEDIA_STORE"]).is_relative_to(tmp_path)
    assert env["KIDSPLAY_LOG_FILE"] == ""


def test_wait_for_server_returns_once_http_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = MagicMock(poll=MagicMock(return_value=None))
    monkeypatch.setattr(seed_mod.httpx, "get", MagicMock())
    seed_mod.wait_for_server("http://x", proc, timeout=1)


def test_wait_for_server_fails_fast_when_process_exits() -> None:
    proc = MagicMock(poll=MagicMock(return_value=3), returncode=3)
    with pytest.raises(RuntimeError, match="exited early"):
        seed_mod.wait_for_server("http://x", proc, timeout=5)


def test_wait_for_server_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = MagicMock(poll=MagicMock(return_value=None))
    monkeypatch.setattr(
        seed_mod.httpx, "get", MagicMock(side_effect=httpx.ConnectError("no"))
    )
    monkeypatch.setattr(seed_mod.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="did not start"):
        seed_mod.wait_for_server("http://x", proc, timeout=0.01)


def test_running_server_starts_and_stops_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = MagicMock()
    popen = MagicMock(return_value=proc)
    monkeypatch.setattr(seed_mod.subprocess, "Popen", popen)
    monkeypatch.setattr(seed_mod, "wait_for_server", MagicMock())

    with seed_mod.running_server(tmp_path, 8123) as url:
        assert url == "http://127.0.0.1:8123"
        cmd = popen.call_args.args[0]
        # Quiet by default: the real app factory, wrapped to hush its console.
        assert "demo.quiet_server:create_app" in cmd
        assert "--factory" in cmd
        assert cmd[cmd.index("--port") + 1] == "8123"
        env = popen.call_args.kwargs["env"]
        assert env["KIDSPLAY_DB_PATH"].startswith(str(tmp_path))
    proc.terminate.assert_called_once()


def test_running_server_not_quiet_uses_the_plain_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popen = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(seed_mod.subprocess, "Popen", popen)
    monkeypatch.setattr(seed_mod, "wait_for_server", MagicMock())

    with seed_mod.running_server(tmp_path, 8124, quiet=False):
        cmd = popen.call_args.args[0]
        assert "kidsplay_server.api.app:create_app_from_env" in cmd
        assert "--log-level" not in cmd


def test_every_seeded_sample_directory_exists() -> None:
    for sample in seed_mod.SAMPLES:
        assert (seed_mod.MEDIA_DIR / sample.path).is_dir(), sample.path


def test_seed_leaves_the_queue_empty(client: TestClient) -> None:
    """The README screenshots and the interactive demo show an empty queue."""
    seed_mod.seed(client)
    assert client.get("/api/v1/queue").json() == []


@pytest.fixture
def quick_queue_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the queue worker look for items every 0.1 s instead of every 10 s.

    A queued link does not wake the worker, so a test waiting on one would sit
    out the poll. Must run before ``client``: the worker starts with the app and
    reads the interval each time it goes to sleep."""
    monkeypatch.setattr(queue_worker, "_POLL_INTERVAL", 0.1)


@pytest.mark.usefixtures("quick_queue_poll")
def test_seed_queue_makes_a_failed_a_running_and_a_pending_item(
    client: TestClient,
) -> None:
    with seed_mod.stalled_source() as url:
        seed_mod.seed_queue(client, url)
        items = client.get("/api/v1/queue").json()
    by_status = {item["status"]: item for item in items}
    assert sorted(by_status) == ["failed", "pending", "running"]
    assert by_status["failed"]["last_error"]
    assert by_status["failed"]["log"]
    assert by_status["pending"]["media_type"] == "audiobook"


def test_stalled_source_accepts_connections_and_never_answers() -> None:
    with seed_mod.stalled_source() as url:
        host, port = url.split("/")[2].split(":")
        with socket.create_connection((host, int(port)), timeout=2) as conn:
            conn.settimeout(0.3)
            conn.sendall(b"GET / HTTP/1.0\r\n\r\n")
            with pytest.raises(TimeoutError):
                conn.recv(1)
    with pytest.raises(OSError):
        socket.create_connection((host, int(port)), timeout=1)


def test_wait_for_queue_status_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.get.return_value.is_error = False
    client.get.return_value.json.return_value = {"status": "pending"}
    ticks = iter([0.0, 100.0])
    monkeypatch.setattr(seed_mod.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="not failed"):
        seed_mod._wait_for_queue_status(client, "x", "failed", timeout=1.0)
