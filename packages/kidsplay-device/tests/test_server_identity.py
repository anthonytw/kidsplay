"""Tests for the server-identity pin (#43): a device only syncs from the server
it paired with, learns one if it has none, and says so when that is not so.
"""

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pygame
import pytest

from kidsplay_device import pairing as device_pairing
from kidsplay_device.config import DeviceConfig
from kidsplay_device.database import (
    get_all_media_ids,
    get_last_server_time,
    get_profile_settings,
    get_sync_state,
    init_db,
    set_sync_state,
)
from kidsplay_device.discovery import FoundServer, server_from_info
from kidsplay_device.pairing import (
    Credentials,
    PairingClient,
    PairingProblem,
    PairingSession,
    Stage,
    config_payload,
    write_config,
)
from kidsplay_device.pairing_screen import found_label, problem_message
from kidsplay_device.sync import SyncClient
from kidsplay_models import SERVER_ID_HEADER, ProfileSettings

from .scenes import make_app

DATE = "Tue, 29 Sep 2026 19:30:00 GMT"
REAL = "3f2a9c1e-8b47-4d1a-9c55-0e6a7d2b1f90"
OTHER = "9d1e7b55-0c2a-4a3e-8f10-77aa2c4b9e01"


@pytest.fixture
def cfg(tmp_path: Path) -> DeviceConfig:
    return DeviceConfig(
        server_url="http://test",
        device_id=str(uuid.uuid4()),
        api_key="k",
        media_root=tmp_path / "media",
        db_path=tmp_path / "device.db",
    )


def manifest(cfg: DeviceConfig, **extra: object) -> dict[str, object]:
    return {
        "device_id": cfg.device_id,
        "profile_id": str(uuid.uuid4()),
        "manifest_hash": "hash-1",
        "files": [],
        "media": [],
        **extra,
    }


def server(
    cfg: DeviceConfig,
    identity: str | None,
    extra: dict[str, object] | None = None,
    *,
    not_modified: bool = False,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Date": DATE}
        if identity is not None:
            headers[SERVER_ID_HEADER] = identity
        if not_modified:
            return httpx.Response(304, headers=headers)
        return httpx.Response(
            200, headers=headers, content=json.dumps(manifest(cfg, **(extra or {})))
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )


def stored(cfg: DeviceConfig) -> dict[str, object]:
    """What a sync left in the device database."""
    conn = init_db(cfg.db_path)
    try:
        return {
            "hash": get_sync_state(conn, "last_manifest_hash"),
            "pin": get_sync_state(conn, "server_id"),
            "time": get_last_server_time(conn),
            "volume": get_profile_settings(conn).max_volume,
            "media": get_all_media_ids(conn),
        }
    finally:
        conn.close()


class TestSyncPin:
    async def test_paired_device_syncs_from_its_server(self, cfg: DeviceConfig) -> None:
        cfg.server_id = REAL
        flags: list[bool] = []
        async with server(cfg, REAL) as http:
            await SyncClient(cfg, http_client=http, on_identity=flags.append).sync()
        assert stored(cfg)["hash"] == "hash-1"
        assert flags == [False]

    async def test_a_different_server_is_refused_before_anything_is_stored(
        self, cfg: DeviceConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg.server_id = REAL
        flags: list[bool] = []
        seen: list[ProfileSettings] = []
        body: dict[str, object] = {
            "profile_settings": {"max_volume": 12},
            "sync_interval_seconds": 60,
        }
        async with server(cfg, OTHER, body) as http:
            client = SyncClient(
                cfg, http_client=http, on_settings=seen.append, on_identity=flags.append
            )
            with caplog.at_level(logging.WARNING, logger="kidsplay_device"):
                await client.sync()
        assert flags == [True]
        assert seen == []  # no settings pushed to the player
        state = stored(cfg)
        assert state["hash"] is None
        assert state["time"] is None  # its clock is not trusted either
        assert state["volume"] == ProfileSettings().max_volume
        assert client.sync_interval_seconds == cfg.sync_interval_seconds
        assert "3F2A-9C1E" in caplog.text and "9D1E-7B55" in caplog.text

    async def test_a_server_that_names_no_one_is_refused_too(
        self, cfg: DeviceConfig
    ) -> None:
        """Omitting the header must not be a way round the check."""
        cfg.server_id = REAL
        flags: list[bool] = []
        async with server(cfg, None) as http:
            await SyncClient(cfg, http_client=http, on_identity=flags.append).sync()
        assert flags == [True]
        assert stored(cfg)["hash"] is None

    async def test_a_304_from_the_wrong_server_changes_nothing(
        self, cfg: DeviceConfig
    ) -> None:
        cfg.server_id = REAL
        conn = init_db(cfg.db_path)
        set_sync_state(conn, "last_manifest_hash", "hash-1")
        conn.commit()
        conn.close()
        flags: list[bool] = []
        async with server(cfg, OTHER, not_modified=True) as http:
            await SyncClient(cfg, http_client=http, on_identity=flags.append).sync()
        assert flags == [True]
        assert stored(cfg)["time"] is None

    async def test_recovers_when_the_right_server_returns(
        self, cfg: DeviceConfig
    ) -> None:
        cfg.server_id = REAL
        flags: list[bool] = []
        async with server(cfg, OTHER) as wrong:
            client = SyncClient(cfg, http_client=wrong, on_identity=flags.append)
            await client.sync()
        async with server(cfg, REAL) as right:
            client = SyncClient(cfg, http_client=right, on_identity=flags.append)
            await client.sync()
        assert flags == [True, False]
        assert stored(cfg)["hash"] == "hash-1"

    async def test_a_persistent_wrong_server_is_reported_once(
        self, cfg: DeviceConfig
    ) -> None:
        cfg.server_id = REAL
        flags: list[bool] = []
        async with server(cfg, OTHER) as http:
            client = SyncClient(cfg, http_client=http, on_identity=flags.append)
            for _ in range(3):
                await client.sync()
        assert flags == [True]

    async def test_the_id_must_match_exactly(self, cfg: DeviceConfig) -> None:
        cfg.server_id = REAL
        for lookalike in (REAL.upper(), REAL + "0", REAL[:-1], f" {REAL}", ""):
            async with server(cfg, lookalike) as http:
                await SyncClient(cfg, http_client=http).sync()
            assert stored(cfg)["hash"] is None, lookalike


class TestLegacyDevices:
    """Devices set up before ids existed must keep working."""

    async def test_first_sight_of_an_id_is_remembered(self, cfg: DeviceConfig) -> None:
        assert cfg.server_id is None
        async with server(cfg, REAL) as http:
            await SyncClient(cfg, http_client=http).sync()
        assert stored(cfg)["pin"] == REAL
        assert stored(cfg)["hash"] == "hash-1"

    async def test_then_a_different_server_is_refused(self, cfg: DeviceConfig) -> None:
        async with server(cfg, REAL) as http:
            await SyncClient(cfg, http_client=http).sync()
        flags: list[bool] = []
        async with server(cfg, OTHER) as http:
            await SyncClient(cfg, http_client=http, on_identity=flags.append).sync()
        assert flags == [True]
        assert stored(cfg)["pin"] == REAL  # not re-pinned to the newcomer

    async def test_an_older_server_without_ids_still_works(
        self, cfg: DeviceConfig
    ) -> None:
        flags: list[bool] = []
        async with server(cfg, None) as http:
            await SyncClient(cfg, http_client=http, on_identity=flags.append).sync()
        assert stored(cfg)["hash"] == "hash-1"
        assert stored(cfg)["pin"] is None
        assert flags == [False]

    async def test_the_config_id_wins_over_a_remembered_one(
        self, cfg: DeviceConfig
    ) -> None:
        """Pairing again pins the new server even if an old one was learned."""
        conn = init_db(cfg.db_path)
        set_sync_state(conn, "server_id", OTHER)
        conn.commit()
        conn.close()
        cfg.server_id = REAL
        async with server(cfg, REAL) as http:
            await SyncClient(cfg, http_client=http).sync()
        assert stored(cfg)["hash"] == "hash-1"

    async def test_an_empty_header_is_not_remembered(self, cfg: DeviceConfig) -> None:
        async with server(cfg, "") as http:
            await SyncClient(cfg, http_client=http).sync()
        assert stored(cfg)["pin"] is None


class TestConfigFile:
    def test_server_id_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        config = DeviceConfig(
            server_url="http://x:8000",
            device_id="d",
            api_key="k",
            media_root=tmp_path / "m",
            db_path=tmp_path / "d.db",
            server_id=REAL,
        )
        config.save(path)
        assert json.loads(path.read_text())["server_id"] == REAL
        assert DeviceConfig.load(path).server_id == REAL

    def test_absent_or_junk_means_none(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        base = {
            "server_url": "http://x",
            "device_id": "d",
            "api_key": "k",
            "media_root": "m",
            "db_path": "d.db",
        }
        for value in (None, "", "  ", 5, ["x"]):
            path.write_text(json.dumps({**base, "server_id": value}))
            assert DeviceConfig.load(path).server_id is None
        path.write_text(json.dumps(base))
        assert DeviceConfig.load(path).server_id is None

    def test_unpinned_config_file_is_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        DeviceConfig(
            server_url="http://x",
            device_id="d",
            api_key="k",
            media_root=tmp_path / "m",
            db_path=tmp_path / "d.db",
        ).save(path)
        assert "server_id" not in json.loads(path.read_text())

    def test_pairing_writes_it_when_the_server_sent_one(self, tmp_path: Path) -> None:
        creds = Credentials("d", "k" * 32, server_id=REAL)
        assert config_payload("http://x:8000", creds)["server_id"] == REAL
        assert "server_id" not in config_payload(
            "http://x:8000", Credentials("d", "k" * 32)
        )
        write_config(tmp_path / "config.json", "http://x:8000", creds)
        assert DeviceConfig.load(tmp_path / "config.json").server_id == REAL


# ---------------------------------------------------------------------------
# Pairing: identity, confirm, saving
# ---------------------------------------------------------------------------

SERVER = "http://192.168.1.20:8000"
DEVICE_ID = "6f1c2d3e-0000-4000-8000-000000000001"


def pairing_handler(
    *,
    created_id: str | None = REAL,
    delivered_id: str | None = REAL,
    log: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request.url.path)
        if request.url.path == "/api/v1/pairing":
            return httpx.Response(201, json={"server_id": created_id})
        if request.url.path == "/api/v1/pairing/confirm":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "status": "approved",
                "device_id": DEVICE_ID,
                "api_key": "k" * 32,
                "server_id": delivered_id,
            },
        )

    return handler


def session_for(
    path: Path, handler: Callable[[httpx.Request], httpx.Response]
) -> PairingSession:
    return PairingSession(
        path,
        poll_interval=0.02,
        retry_delay=0.02,
        http_client_factory=lambda url: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )


async def finish(session: PairingSession) -> None:
    session.start(SERVER)
    await asyncio_wait(lambda: session.state.stage in (Stage.DONE, Stage.FAILED))


async def asyncio_wait(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


class TestPairingIdentity:
    async def test_the_id_is_shown_while_waiting_and_saved_with_the_key(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.json"
        session = session_for(path, pairing_handler())
        session.start(SERVER)
        await asyncio_wait(lambda: session.state.stage is Stage.DONE)
        assert DeviceConfig.load(path).server_id == REAL
        assert session.state.server_id == "3F2A-9C1E"

    async def test_older_server_without_ids_still_pairs(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        session = session_for(path, pairing_handler(created_id=None, delivered_id=None))
        await finish(session)
        assert session.state.stage is Stage.DONE
        assert DeviceConfig.load(path).server_id is None

    async def test_a_server_that_changes_identity_midway_is_refused(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.json"
        session = session_for(path, pairing_handler(delivered_id=OTHER))
        await finish(session)
        assert session.state.stage is Stage.FAILED
        assert session.state.problem is PairingProblem.WRONG_SERVER
        assert not path.exists()

    async def test_the_delivered_id_is_used_when_create_named_none(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.json"
        session = session_for(path, pairing_handler(created_id=None))
        await finish(session)
        assert DeviceConfig.load(path).server_id == REAL


class TestConfirmAndSave:
    async def test_confirms_only_after_the_config_is_on_disk(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.json"
        at_confirm: list[bool] = []
        log: list[str] = []
        base = pairing_handler(log=log)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/confirm"):
                at_confirm.append(path.exists())
            return base(request)

        session = session_for(path, handler)
        await finish(session)
        assert session.state.stage is Stage.DONE
        assert at_confirm == [True]
        assert log[-1] == "/api/v1/pairing/confirm"

    async def test_a_server_that_ignores_confirm_does_not_fail_pairing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.json"
        base = pairing_handler()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/confirm"):
                return httpx.Response(404, json={"error_code": "NOPE"})
            return base(request)

        session = session_for(path, handler)
        await finish(session)
        assert session.state.stage is Stage.DONE
        assert path.exists()

    async def test_a_dead_network_at_confirm_does_not_fail_pairing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.json"
        base = pairing_handler()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/confirm"):
                raise httpx.ConnectError("wifi dropped")
            return base(request)

        session = session_for(path, handler)
        await finish(session)
        assert session.state.stage is Stage.DONE

    async def test_a_failed_write_is_retried_with_the_key_still_in_hand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config.json"
        real = device_pairing.write_config
        attempts: list[int] = []

        def flaky(path: Path, url: str, creds: Credentials) -> dict[str, object]:
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("read-only file system")
            return real(path, url, creds)

        monkeypatch.setattr(device_pairing, "write_config", flaky)
        monkeypatch.setattr(device_pairing, "_SAVE_RETRY_SECONDS", 0.01)
        session = session_for(path, pairing_handler())
        await finish(session)
        assert session.state.stage is Stage.DONE
        assert len(attempts) == 3
        assert DeviceConfig.load(path).api_key == "k" * 32

    async def test_a_write_that_never_works_says_so_and_does_not_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log: list[str] = []

        def broken(path: Path, url: str, creds: Credentials) -> dict[str, object]:
            raise OSError("no space left on device")

        monkeypatch.setattr(device_pairing, "write_config", broken)
        monkeypatch.setattr(device_pairing, "_SAVE_RETRY_SECONDS", 0.01)
        session = session_for(tmp_path / "config.json", pairing_handler(log=log))
        await finish(session)
        assert session.state.stage is Stage.FAILED
        assert session.state.problem is PairingProblem.CANT_SAVE
        # The server is not told the key was saved: it can hand it out again.
        assert "/api/v1/pairing/confirm" not in log

    async def test_client_confirm_reports_failure_quietly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        client = PairingClient(
            SERVER,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert await client.confirm("ABCD2345", "s" * 40) is False

    def test_messages_for_the_new_problems(self) -> None:
        assert "save" in problem_message(PairingProblem.CANT_SAVE).lower()
        assert "server" in problem_message(PairingProblem.WRONG_SERVER).lower()

    async def test_poll_error_after_a_lost_response_is_retried_and_succeeds(
        self, tmp_path: Path
    ) -> None:
        """The response carrying the key is lost; the retry gets it again."""
        path = tmp_path / "config.json"
        polls: list[int] = []
        base = pairing_handler()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/poll"):
                polls.append(1)
                if len(polls) == 1:
                    raise httpx.ReadError("connection reset")
            return base(request)

        session = session_for(path, handler)
        await finish(session)
        assert session.state.stage is Stage.DONE
        assert len(polls) == 2


# ---------------------------------------------------------------------------
# What the parent and the child see
# ---------------------------------------------------------------------------


class TestDiscoveryLabels:
    def test_label_names_address_and_id(self) -> None:
        server = FoundServer("KidsPlay", "http://192.168.1.20:8000", "3F2A-9C1E")
        assert found_label(server) == "KidsPlay · 192.168.1.20:8000 · 3F2A-9C1E"

    def test_label_without_an_id(self) -> None:
        server = FoundServer("Home", "http://10.0.0.5:8000")
        assert found_label(server) == "Home · 10.0.0.5:8000"

    def test_announced_id_is_read_from_the_txt_record(self) -> None:
        class Info:
            name = "Home._kidsplay._tcp.local."
            port = 8000
            properties = {b"id": REAL.encode(), b"api": b"/api/v1"}

            def parsed_addresses(self, version: object = None) -> list[str]:
                return ["192.168.1.20"]

        found = server_from_info(Info())  # ty: ignore[invalid-argument-type]  # duck-typed ServiceInfo
        assert found == FoundServer("Home", "http://192.168.1.20:8000", "3F2A-9C1E")

    @pytest.mark.parametrize("raw", [None, b"", b"\xff\xfe", "notbytes", 12])
    def test_odd_txt_ids_never_break_discovery(self, raw: object) -> None:
        class Info:
            name = "Home._kidsplay._tcp.local."
            port = 8000
            properties = {b"id": raw}

            def parsed_addresses(self, version: object = None) -> list[str]:
                return ["192.168.1.20"]

        found = server_from_info(Info())  # ty: ignore[invalid-argument-type]  # duck-typed ServiceInfo
        assert found is not None
        assert found.server_id == ""


class TestHomeWarning:
    def test_home_says_so_when_the_server_is_not_the_one_it_paired_with(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        app = make_app(tmp_path, monkeypatch)
        try:
            surface = pygame.Surface((640, 480))
            home = app._get_view("home")
            home.draw(surface)
            plain = pygame.image.tobytes(surface, "RGB")
            assert not app.identity_warning

            app._on_identity_problem(True)
            assert app.identity_warning
            home.draw(surface)
            warned = pygame.image.tobytes(surface, "RGB")
            assert warned != plain

            app._on_identity_problem(False)
            home.draw(surface)
            assert pygame.image.tobytes(surface, "RGB") == plain
        finally:
            pygame.quit()


def test_state_file_layout_is_untouched(tmp_path: Path) -> None:
    """The learned id lives in the existing key/value table: no schema change."""
    conn = init_db(tmp_path / "db.sqlite")
    set_sync_state(conn, "server_id", REAL)
    conn.commit()
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "sync_state" in tables
    assert sqlite3.connect(tmp_path / "db.sqlite").execute(
        "SELECT value FROM sync_state WHERE key = 'server_id'"
    ).fetchone() == (REAL,)
