"""Tests for the device's pairing client, config writer and session.

The end-to-end tests run a real server app in-process (ASGI transport, no
sockets): device asks -> admin approves -> config written -> first sync.
"""

import asyncio
import json
import os
import stat
import threading
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_cli.main import cli
from kidsplay_device import pairing as device_pairing
from kidsplay_device.config import DeviceConfig
from kidsplay_device.database import get_photos, get_sync_state, init_db
from kidsplay_device.pairing import (
    CONFIG_KEYS,
    Credentials,
    PairingClient,
    PairingError,
    PairingProblem,
    PairingSession,
    Stage,
    generate_code,
    generate_secret,
    normalize_server_url,
    preset_pair_server,
    write_config,
)
from kidsplay_device.sync import SyncClient
from kidsplay_models import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    normalize_pairing_code,
    short_server_id,
)
from kidsplay_server import pairing as server_pairing
from kidsplay_server.api.app import create_app

from .test_sync import ingest_photo, make_png, seed_admin_token

SERVER = "http://127.0.0.1:8000"


class TestGenerators:
    def test_code_matches_the_contract(self) -> None:
        for _ in range(200):
            code = generate_code()
            assert len(code) == PAIRING_CODE_LENGTH
            assert set(code) <= set(PAIRING_CODE_ALPHABET)
            assert normalize_pairing_code(code) == code

    def test_codes_and_secrets_vary(self) -> None:
        assert len({generate_code() for _ in range(50)}) > 45
        secrets_ = {generate_secret() for _ in range(50)}
        assert len(secrets_) == 50
        assert all(len(s) >= 32 for s in secrets_)

    def test_credentials_repr_hides_the_key(self) -> None:
        creds = Credentials(device_id="d1", api_key="super-secret-key")
        assert "super-secret-key" not in repr(creds)
        assert "super-secret-key" not in str(creds)


class TestServerUrl:
    @pytest.mark.parametrize(
        ("typed", "url"),
        [
            ("192.168.1.20", "http://192.168.1.20:8000"),
            ("192.168.1.20:9000", "http://192.168.1.20:9000"),
            ("kidsplay.local", "http://kidsplay.local:8000"),
            (" 10.0.0.5:8000/ ", "http://10.0.0.5:8000"),
            ("http://nas.lan:8080", "http://nas.lan:8080"),
            ("https://kids.example.com", "https://kids.example.com"),
            ("HTTP://Host", "http://Host"),
        ],
    )
    def test_accepts(self, typed: str, url: str) -> None:
        assert normalize_server_url(typed) == url

    @pytest.mark.parametrize(
        "typed", ["", "   ", "http://", "a b", "host/path", "ho$t", "http:///x"]
    )
    def test_rejects(self, typed: str) -> None:
        assert normalize_server_url(typed) is None


def creds() -> Credentials:
    return Credentials(
        device_id="6f1c2d3e-0000-4000-8000-000000000001", api_key="k" * 32
    )


class TestWriteConfig:
    def test_keys_are_what_device_setup_writes(self, tmp_path: Path) -> None:
        """Same keys and defaults as ``kidsplay device setup`` (run for real)."""
        result = CliRunner().invoke(
            cli,
            [
                "--server", SERVER, "--token", "x",
                "device", "setup", "--device-id", "d", "--api-key", "k",
            ],
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        from_cli = json.loads(result.output)

        written = write_config(tmp_path / "config.json", SERVER, creds())
        on_disk = json.loads((tmp_path / "config.json").read_text())
        assert on_disk == written
        assert set(on_disk) == set(from_cli) == set(CONFIG_KEYS)
        for key in ("media_root", "db_path", "sync_interval_seconds"):
            assert on_disk[key] == from_cli[key]

    def test_config_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        write_config(path, SERVER, creds())
        config = DeviceConfig.load(path)
        assert (config.server_url, config.device_id) == (SERVER, creds().device_id)
        assert config.api_key == creds().api_key
        assert config.sync_transport == "http"

    def test_owner_only_permissions(self, tmp_path: Path) -> None:
        old_umask = os.umask(0)  # a permissive umask must not matter
        try:
            path = tmp_path / "sub" / "config.json"
            write_config(path, SERVER, creds())
        finally:
            os.umask(old_umask)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) & 0o022 == 0

    def test_no_temp_files_left(self, tmp_path: Path) -> None:
        write_config(tmp_path / "config.json", SERVER, creds())
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    def test_never_partially_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure while writing leaves the old file intact and no debris."""
        path = tmp_path / "config.json"
        path.write_text("OLD")
        os.chmod(path, 0o600)

        def boom(fd: int) -> None:
            raise OSError("disk fell out")

        monkeypatch.setattr(os, "fsync", boom)
        with pytest.raises(OSError, match="disk"):
            write_config(path, SERVER, creds())
        assert path.read_text() == "OLD"
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    def test_nothing_appears_at_the_destination_before_it_is_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config.json"
        seen: list[bool] = []
        real_replace = Path.replace

        def spying_replace(self: Path, target: Path) -> Path:
            seen.append(Path(target).exists())
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", spying_replace)
        write_config(path, SERVER, creds())
        assert seen == [False]


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> PairingClient:
    return PairingClient(
        SERVER,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": "x", "error_code": code})


class TestClientErrors:
    @pytest.mark.parametrize(
        ("status", "code", "problem"),
        [
            (410, "PAIRING_EXPIRED", PairingProblem.EXPIRED),
            (410, "PAIRING_USED", PairingProblem.USED),
            (403, "PAIRING_DENIED", PairingProblem.DECLINED),
            (403, "PAIRING_DISABLED", PairingProblem.DISABLED),
            (429, "RATE_LIMITED", PairingProblem.BUSY),
            (429, "TOO_MANY_PENDING", PairingProblem.BUSY),
            (404, "PAIRING_NOT_FOUND", PairingProblem.EXPIRED),
            (500, "", PairingProblem.UNKNOWN),
            (404, "", PairingProblem.UNREACHABLE),  # an older server, or not ours
        ],
    )
    async def test_poll_maps_errors(
        self, status: int, code: str, problem: PairingProblem
    ) -> None:
        client = mock_client(lambda r: error(status, code))
        with pytest.raises(PairingError) as raised:
            await client.poll("ABCD2345", "s" * 40)
        assert raised.value.problem is problem

    async def test_network_failure(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        with pytest.raises(PairingError) as raised:
            await mock_client(down).start("ABCD2345", "s" * 40)
        assert raised.value.problem is PairingProblem.UNREACHABLE

    async def test_html_error_page_is_not_a_crash(self) -> None:
        client = mock_client(lambda r: httpx.Response(502, text="<html>bad gateway"))
        with pytest.raises(PairingError) as raised:
            await client.start("ABCD2345", "s" * 40)
        assert raised.value.problem is PairingProblem.UNKNOWN

    async def test_code_in_use_is_not_an_error(self) -> None:
        client = mock_client(lambda r: error(409, "CODE_IN_USE"))
        assert await client.start("ABCD2345", "s" * 40) is False

    async def test_pending_returns_none(self) -> None:
        client = mock_client(lambda r: httpx.Response(200, json={"status": "pending"}))
        assert await client.poll("ABCD2345", "s" * 40) is None

    async def test_garbage_success_body(self) -> None:
        client = mock_client(lambda r: httpx.Response(200, text="not json"))
        with pytest.raises(PairingError):
            await client.poll("ABCD2345", "s" * 40)

    async def test_approved_without_key_is_refused(self) -> None:
        client = mock_client(lambda r: httpx.Response(200, json={"status": "approved"}))
        with pytest.raises(PairingError):
            await client.poll("ABCD2345", "s" * 40)

    async def test_secret_only_in_the_body(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"status": "pending"})

        secret = generate_secret()
        await mock_client(handler).poll("ABCD2345", secret)
        assert secret not in str(seen[0].url)
        assert secret not in json.dumps(dict(seen[0].headers))
        assert secret in seen[0].content.decode()


# ---------------------------------------------------------------------------
# End to end against a real server app
# ---------------------------------------------------------------------------


@pytest.fixture
def server_app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "server.db", tmp_path / "server_media")


@pytest.fixture
async def admin(server_app: FastAPI) -> AsyncIterator[AsyncClient]:
    token = await seed_admin_token(server_app.state.db_path)
    async with AsyncClient(
        transport=ASGITransport(app=server_app),
        base_url=SERVER,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield c


def make_session(server_app: FastAPI, config_path: Path) -> PairingSession:
    def factory(url: str) -> httpx.AsyncClient:
        return AsyncClient(transport=ASGITransport(app=server_app), base_url=url)

    return PairingSession(
        config_path,
        device_name="Test handheld",
        poll_interval=0.05,
        http_client_factory=factory,
    )


async def wait_for(check: Callable[[], bool], what: str, seconds: float = 10) -> None:
    for _ in range(int(seconds / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def start_and_wait_for_code(session: PairingSession) -> str:
    session.start(SERVER)
    await wait_for(lambda: session.state.stage is not Stage.CONNECTING, "the code")
    assert session.state.stage is Stage.WAITING, session.state
    return session.state.code


class TestEndToEnd:
    async def test_request_approve_config_first_sync(
        self, server_app: FastAPI, admin: AsyncClient, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "device" / "config.json"
        profile = (await admin.post("/api/v1/profiles", json={"name": "Leo"})).json()
        photo = make_png(tmp_path / "p.png", seed=4)
        await ingest_photo(admin, photo, profile_ids=[profile["id"]])

        session = make_session(server_app, config_path)
        code = await start_and_wait_for_code(session)

        # 1. The device shows a code; nothing is written and it has no key.
        assert len(code.replace("-", "")) == PAIRING_CODE_LENGTH
        assert not config_path.exists()
        waiting = (await admin.get("/api/v1/pairing/requests")).json()
        assert [(r["code"], r["device_name"]) for r in waiting] == [
            (code.replace("-", ""), "Test handheld")
        ]
        assert (await admin.get("/api/v1/devices")).json() == []
        await asyncio.sleep(0.3)  # several polls: still nothing
        assert not config_path.exists()
        assert session.state.stage is Stage.WAITING

        # 2. The parent approves.
        r = await admin.post(
            "/api/v1/pairing/approve",
            json={"code": code, "profile_id": profile["id"], "name": "Leo's player"},
        )
        assert r.status_code == 200

        # 3. The device writes its config.
        await wait_for(lambda: session.state.stage is Stage.DONE, "pairing to finish")
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        config = DeviceConfig.load(config_path)
        assert config.server_url == SERVER
        device = (await admin.get(f"/api/v1/devices/{config.device_id}")).json()
        assert device["name"] == "Leo's player"
        assert device["profile_id"] == profile["id"]
        assert config.api_key == device["api_key"]
        on_disk = json.loads(config_path.read_text())
        assert set(on_disk) == set(CONFIG_KEYS) | {"server_id"}
        assert config.server_id == on_disk["server_id"]
        assert config.server_id
        assert session.state.server_id == short_server_id(config.server_id)

        # 4. The first sync with that config succeeds.
        config.media_root = tmp_path / "device" / "media"
        config.db_path = tmp_path / "device" / "db.sqlite"
        http = AsyncClient(transport=ASGITransport(app=server_app), base_url=SERVER)
        async with http:
            await SyncClient(config, http_client=http).sync()
            manifest = (
                await http.get(
                    f"/api/v1/devices/{config.device_id}/manifest",
                    headers={"Authorization": f"Bearer {config.api_key}"},
                )
            ).json()
        conn = init_db(config.db_path)
        try:
            assert (
                get_sync_state(conn, "last_manifest_hash") == manifest["manifest_hash"]
            )
            assert get_sync_state(conn, "last_sync_at") is not None
            assert len(get_photos(conn)) == 1
        finally:
            conn.close()

    async def test_key_is_never_given_twice(
        self, server_app: FastAPI, admin: AsyncClient, tmp_path: Path
    ) -> None:
        session = make_session(server_app, tmp_path / "config.json")
        code = await start_and_wait_for_code(session)
        profile = (await admin.post("/api/v1/profiles", json={"name": "Leo"})).json()
        await admin.post(
            "/api/v1/pairing/approve", json={"code": code, "profile_id": profile["id"]}
        )
        await wait_for(lambda: session.state.stage is Stage.DONE, "pairing to finish")

        # Someone replays the code with a guessed secret, and with none valid.
        client = PairingClient(
            SERVER,
            http_client=AsyncClient(
                transport=ASGITransport(app=server_app), base_url=SERVER
            ),
        )
        with pytest.raises(PairingError) as raised:
            await client.poll(code, generate_secret())
        assert raised.value.problem is PairingProblem.EXPIRED  # "not found"
        assert not (tmp_path / "other.json").exists()

    async def test_reused_code_fails_clearly(
        self, server_app: FastAPI, admin: AsyncClient, tmp_path: Path
    ) -> None:
        """The device that already collected its key is told the code is used."""
        secret = generate_secret()
        http = AsyncClient(transport=ASGITransport(app=server_app), base_url=SERVER)
        client = PairingClient(SERVER, http_client=http)
        code = generate_code()
        assert await client.start(code, secret) is True
        profile = (await admin.post("/api/v1/profiles", json={"name": "Leo"})).json()
        await admin.post(
            "/api/v1/pairing/approve", json={"code": code, "profile_id": profile["id"]}
        )
        first = await client.poll(code, secret)
        assert first is not None
        assert await client.confirm(code, secret) is True
        with pytest.raises(PairingError) as raised:
            await client.poll(code, secret)
        assert raised.value.problem is PairingProblem.USED

    async def test_key_can_be_fetched_again_until_confirmed(
        self, server_app: FastAPI, admin: AsyncClient
    ) -> None:
        """A response lost on the way must not strand the device (#43)."""
        secret = generate_secret()
        http = AsyncClient(transport=ASGITransport(app=server_app), base_url=SERVER)
        client = PairingClient(SERVER, http_client=http)
        code = generate_code()
        assert await client.start(code, secret) is True
        profile = (await admin.post("/api/v1/profiles", json={"name": "Leo"})).json()
        await admin.post(
            "/api/v1/pairing/approve", json={"code": code, "profile_id": profile["id"]}
        )
        first = await client.poll(code, secret)
        again = await client.poll(code, secret)
        assert first is not None and again == first
        assert first.server_id == client.server_id
        # Someone without the secret gets nothing, and cannot close it either.
        assert await client.confirm(code, generate_secret()) is False
        assert await client.poll(code, secret) == first

    async def test_expired_code_fails_clearly(
        self,
        server_app: FastAPI,
        admin: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = make_session(server_app, tmp_path / "config.json")
        await start_and_wait_for_code(session)
        later = (
            server_pairing.utcnow()
            + server_pairing.CODE_LIFETIME
            + timedelta(seconds=1)
        )
        monkeypatch.setattr(server_pairing, "utcnow", lambda: later)
        await wait_for(lambda: session.state.stage is Stage.FAILED, "expiry")
        assert session.state.problem is PairingProblem.EXPIRED
        assert not (tmp_path / "config.json").exists()

    async def test_declined(
        self, server_app: FastAPI, admin: AsyncClient, tmp_path: Path
    ) -> None:
        session = make_session(server_app, tmp_path / "config.json")
        code = await start_and_wait_for_code(session)
        await admin.post("/api/v1/pairing/deny", json={"code": code})
        await wait_for(lambda: session.state.stage is Stage.FAILED, "the refusal")
        assert session.state.problem is PairingProblem.DECLINED
        assert not (tmp_path / "config.json").exists()

    async def test_pairing_disabled(
        self, server_app: FastAPI, admin: AsyncClient, tmp_path: Path
    ) -> None:
        await admin.put("/api/v1/server-settings", json={"pairing_enabled": False})
        session = make_session(server_app, tmp_path / "config.json")
        session.start(SERVER)
        await wait_for(lambda: session.state.stage is Stage.FAILED, "the refusal")
        assert session.state.problem is PairingProblem.DISABLED

    async def test_rate_limited(self, server_app: FastAPI, tmp_path: Path) -> None:
        http = AsyncClient(transport=ASGITransport(app=server_app), base_url=SERVER)
        client = PairingClient(SERVER, http_client=http)
        with pytest.raises(PairingError) as raised:
            for _ in range(50):
                await client.start(generate_code(), generate_secret())
        assert raised.value.problem is PairingProblem.BUSY

    async def test_unreachable_server(self, tmp_path: Path) -> None:
        def unreachable(url: str) -> httpx.AsyncClient:
            def refuse(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("refused")

            return httpx.AsyncClient(transport=httpx.MockTransport(refuse))

        session = PairingSession(
            tmp_path / "config.json", http_client_factory=unreachable
        )
        session.start("http://nowhere:8000")
        await wait_for(lambda: session.state.stage is Stage.FAILED, "the failure")
        assert session.state.problem is PairingProblem.UNREACHABLE

    async def test_taken_code_is_replaced(self, tmp_path: Path) -> None:
        """A 409 makes the device pick another code, not give up."""
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/pairing":
                attempts.append(json.loads(request.content)["code"])
                if len(attempts) < 3:
                    return error(409, "CODE_IN_USE")
                return httpx.Response(201, json={})
            return httpx.Response(200, json={"status": "pending"})

        session = PairingSession(
            tmp_path / "config.json",
            poll_interval=0.05,
            http_client_factory=lambda url: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
        session.start(SERVER)
        await wait_for(lambda: session.state.stage is Stage.WAITING, "the code")
        session.cancel()
        assert len(attempts) == 3
        assert len(set(attempts)) == 3
        assert session.state.code.replace("-", "") == attempts[-1]

    async def test_transport_errors_while_waiting_are_retried(
        self, tmp_path: Path
    ) -> None:
        """A dropped connection mid-wait must not throw the code away."""
        polls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/pairing":
                return httpx.Response(201, json={})
            if request.url.path == "/api/v1/pairing/confirm":
                return httpx.Response(204)
            polls.append(1)
            if len(polls) <= 3:
                raise httpx.ConnectError("wifi dropped")
            return httpx.Response(
                200,
                json={
                    "status": "approved",
                    "device_id": "6f1c2d3e-0000-4000-8000-000000000001",
                    "api_key": "k" * 32,
                },
            )

        session = PairingSession(
            tmp_path / "config.json",
            poll_interval=0.02,
            retry_delay=0.02,
            http_client_factory=lambda url: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
        session.start(SERVER)
        await wait_for(lambda: session.state.stage is Stage.DONE, "pairing to finish")
        assert len(polls) == 4
        assert (tmp_path / "config.json").exists()

    async def test_backoff_grows_between_transport_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        waits: list[float] = []

        async def record(
            self: PairingSession, cancel: threading.Event, seconds: float
        ) -> None:
            waits.append(seconds)
            await asyncio.sleep(0.001)

        monkeypatch.setattr(PairingSession, "_sleep", record)
        polls = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/pairing":
                return httpx.Response(201, json={})
            polls[0] += 1
            if polls[0] <= 7:
                raise httpx.ReadTimeout("slow")
            return httpx.Response(200, json={"status": "pending"})

        session = PairingSession(
            tmp_path / "config.json",
            poll_interval=3.0,
            retry_delay=1.0,
            http_client_factory=lambda url: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
        session.start(SERVER)
        await wait_for(lambda: len(waits) >= 8, "the retries")
        session.cancel()
        # Doubling up to the cap, then back to the normal poll interval.
        assert waits[:8] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 3.0]

    async def test_a_dead_network_gives_up_when_the_code_expires(
        self, tmp_path: Path
    ) -> None:
        now = [1000.0]
        polls = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/pairing":
                return httpx.Response(201, json={})
            polls[0] += 1
            raise httpx.ConnectError("no route")

        session = PairingSession(
            tmp_path / "config.json",
            poll_interval=0.02,
            retry_delay=0.02,
            clock=lambda: now[0],
            http_client_factory=lambda url: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
        session.start(SERVER)
        await wait_for(lambda: polls[0] >= 3, "retries")
        assert session.state.stage is Stage.WAITING
        now[0] += 601  # the code has expired
        await wait_for(lambda: session.state.stage is Stage.FAILED, "giving up")
        assert session.state.problem is PairingProblem.UNREACHABLE

    @pytest.mark.parametrize(
        ("status", "code", "problem"),
        [
            (410, "PAIRING_EXPIRED", PairingProblem.EXPIRED),
            (410, "PAIRING_USED", PairingProblem.USED),
            (403, "PAIRING_DENIED", PairingProblem.DECLINED),
            (403, "PAIRING_DISABLED", PairingProblem.DISABLED),
            (404, "PAIRING_NOT_FOUND", PairingProblem.EXPIRED),
        ],
    )
    async def test_an_answer_from_the_server_is_never_retried(
        self, tmp_path: Path, status: int, code: str, problem: PairingProblem
    ) -> None:
        polls = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/pairing":
                return httpx.Response(201, json={})
            polls[0] += 1
            return error(status, code)

        session = PairingSession(
            tmp_path / "config.json",
            poll_interval=0.02,
            retry_delay=0.02,
            http_client_factory=lambda url: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
        session.start(SERVER)
        await wait_for(lambda: session.state.stage is Stage.FAILED, "failure")
        await asyncio.sleep(0.15)
        assert polls[0] == 1
        assert session.state.problem is problem

    async def test_countdown_and_cancel(self, tmp_path: Path) -> None:
        now = [1000.0]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/pairing":
                return httpx.Response(201, json={})
            return httpx.Response(200, json={"status": "pending"})

        session = PairingSession(
            tmp_path / "config.json",
            poll_interval=0.05,
            clock=lambda: now[0],
            http_client_factory=lambda url: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
        session.start(SERVER)
        await wait_for(lambda: session.state.stage is Stage.WAITING, "the code")
        assert session.state.seconds_left == 600
        now[0] += 125
        assert session.state.seconds_left == 475
        session.cancel()
        threads_before = threading.active_count()
        session.join(5)
        assert threading.active_count() <= threads_before


def test_default_config_path_is_the_players() -> None:
    from kidsplay_device.config import DEFAULT_CONFIG_PATH

    assert device_pairing.DEFAULT_CONFIG_PATH == DEFAULT_CONFIG_PATH
    assert DEFAULT_CONFIG_PATH.name == "config.json"


_ = datetime  # keep the import used for type readers of the fixtures above


class TestPresetPairServer:
    """A server address preset in a file, so nobody types it on the device."""

    def test_reads_the_first_address_line(self, tmp_path: Path) -> None:
        f = tmp_path / "pair-server.txt"
        f.write_text("# our server\n\n  https://kidsplay.example.net/  \nhttp://x\n")
        assert preset_pair_server((f,)) == "https://kidsplay.example.net"

    def test_a_bare_ip_gets_the_usual_scheme_and_port(self, tmp_path: Path) -> None:
        f = tmp_path / "pair-server.txt"
        f.write_text("192.168.1.20\n")
        assert preset_pair_server((f,)) == "http://192.168.1.20:8000"

    def test_first_existing_file_wins(self, tmp_path: Path) -> None:
        home, boot = tmp_path / "home.txt", tmp_path / "boot.txt"
        boot.write_text("https://boot.example.net\n")
        assert preset_pair_server((home, boot)) == "https://boot.example.net"
        home.write_text("https://home.example.net\n")
        assert preset_pair_server((home, boot)) == "https://home.example.net"

    def test_windows_editor_bom_and_crlf(self, tmp_path: Path) -> None:
        f = tmp_path / "kidsplay-server.txt"
        f.write_bytes("\ufeffhttps://kidsplay.example.net\r\n".encode())
        assert preset_pair_server((f,)) == "https://kidsplay.example.net"

    def test_nothing_preset(self, tmp_path: Path) -> None:
        assert preset_pair_server((tmp_path / "missing.txt",)) is None
        empty = tmp_path / "empty.txt"
        empty.write_text("# only a comment\n\n")
        assert preset_pair_server((empty,)) is None

    def test_not_an_address_is_ignored_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        f = tmp_path / "pair-server.txt"
        f.write_text("ask dad for the address\n")
        assert preset_pair_server((f,)) is None
        assert "not an address" in caplog.text

    def test_unreadable_file_is_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = tmp_path / "pair-server.txt"
        directory.mkdir()  # reading a directory fails with an OSError
        boot = tmp_path / "boot.txt"
        boot.write_text("https://boot.example.net\n")
        assert preset_pair_server((directory, boot)) == "https://boot.example.net"
        assert "Cannot read" in caplog.text
