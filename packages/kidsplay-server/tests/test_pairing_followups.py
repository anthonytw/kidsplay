"""Tests for the pairing follow-ups of #43 (server side).

* a key lost after delivery can be fetched again, briefly, until confirmed
* creating a request for a code in use looks exactly like creating a new one
* expired requests are swept on a timer and at startup
* the server has a stable identity that pairing and the manifest report
"""

import asyncio
import logging
import secrets
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from kidsplay_models import SERVER_ID_HEADER, short_server_id
from kidsplay_server import pairing
from kidsplay_server.api.app import create_app
from kidsplay_server.database import configure_conn, get_or_create_server_id, init_db

CODE = "ABCD-2345"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def admin(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=admin_headers,
    ) as c:
        yield c


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[datetime]]:
    state = [pairing.utcnow()]
    monkeypatch.setattr(pairing, "utcnow", lambda: state[0])
    yield state


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def body(secret: str, code: str = CODE) -> dict[str, object]:
    return {"code": code, "binding_secret": secret}


async def start_and_approve(
    anon: AsyncClient, admin: AsyncClient, code: str = CODE
) -> str:
    """Register ``code``, approve it, return the device's secret."""
    secret = new_secret()
    assert (
        await anon.post("/api/v1/pairing", json=body(secret, code))
    ).status_code == 201
    pid = (await admin.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
    r = await admin.post(
        "/api/v1/pairing/approve", json={"code": code, "profile_id": pid}
    )
    assert r.status_code == 200
    return secret


async def poll(anon: AsyncClient, secret: str, code: str = CODE) -> Response:
    return await anon.post("/api/v1/pairing/poll", json=body(secret, code))


async def confirm(anon: AsyncClient, secret: str, code: str = CODE) -> Response:
    return await anon.post("/api/v1/pairing/confirm", json=body(secret, code))


# ---------------------------------------------------------------------------
# A key lost after delivery
# ---------------------------------------------------------------------------


class TestRedelivery:
    async def test_same_secret_gets_the_same_key_again(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        """A lost response or failed write must not strand the device."""
        secret = await start_and_approve(anon_client, admin)
        first = (await poll(anon_client, secret)).json()
        second = await poll(anon_client, secret)
        third = await poll(anon_client, secret)
        assert first["status"] == "approved"
        assert second.status_code == third.status_code == 200
        assert second.json() == third.json() == first
        device = (await admin.get(f"/api/v1/devices/{first['device_id']}")).json()
        assert first["api_key"] == device["api_key"]
        assert len((await admin.get("/api/v1/devices")).json()) == 1

    async def test_wrong_secret_never_gets_it(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        key = (await poll(anon_client, secret)).json()["api_key"]
        for guess in (new_secret(), "x" * 40, secret + "x", secret[:-1]):
            r = await poll(anon_client, guess)
            assert r.status_code == 404
            assert key not in r.text

    async def test_window_closes_by_itself(
        self, anon_client: AsyncClient, admin: AsyncClient, clock: list[datetime]
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        assert (await poll(anon_client, secret)).json()["api_key"]
        clock[0] += pairing.REDELIVER_WINDOW - timedelta(seconds=1)
        assert (await poll(anon_client, secret)).status_code == 200
        clock[0] += timedelta(seconds=2)
        r = await poll(anon_client, secret)
        assert r.status_code == 410
        assert r.json()["error_code"] == "PAIRING_USED"
        assert "api_key" not in r.text

    async def test_window_is_counted_from_delivery_not_approval(
        self, anon_client: AsyncClient, admin: AsyncClient, clock: list[datetime]
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        clock[0] += timedelta(minutes=5)
        assert (await poll(anon_client, secret)).status_code == 200
        clock[0] += pairing.REDELIVER_WINDOW - timedelta(seconds=1)
        assert (await poll(anon_client, secret)).status_code == 200

    async def test_redelivery_does_not_extend_the_window(
        self, anon_client: AsyncClient, admin: AsyncClient, clock: list[datetime]
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        await poll(anon_client, secret)
        for _ in range(3):
            clock[0] += pairing.REDELIVER_WINDOW / 2 - timedelta(seconds=1)
            r = await poll(anon_client, secret)
            if r.status_code != 200:
                break
        assert r.status_code == 410

    async def test_confirm_closes_it_at_once(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        key = (await poll(anon_client, secret)).json()["api_key"]
        assert (await confirm(anon_client, secret)).status_code == 204
        r = await poll(anon_client, secret)
        assert r.status_code == 410
        assert r.json()["error_code"] == "PAIRING_USED"
        assert key not in r.text
        # Idempotent.
        assert (await confirm(anon_client, secret)).status_code == 204
        assert (await poll(anon_client, secret)).status_code == 410

    async def test_confirm_needs_the_secret(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        """Nobody else can close the window (or learn anything from trying)."""
        secret = await start_and_approve(anon_client, admin)
        await poll(anon_client, secret)
        r = await confirm(anon_client, new_secret())
        assert r.status_code == 404
        assert r.json()["error_code"] == "PAIRING_NOT_FOUND"
        unknown = await confirm(anon_client, secret, code="WXYZ7777")
        assert unknown.status_code == 404
        assert unknown.json() == r.json()
        # The real device can still fetch its key.
        assert (await poll(anon_client, secret)).status_code == 200

    async def test_confirm_before_delivery_changes_nothing(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        assert (await confirm(anon_client, secret)).status_code == 204
        assert (await poll(anon_client, secret)).json()["status"] == "approved"

    async def test_confirm_on_a_pending_request_does_not_expire_it(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        assert (await confirm(anon_client, secret)).status_code == 204
        assert (await poll(anon_client, secret)).json()["status"] == "pending"

    async def test_confirm_is_rate_limited_like_poll(
        self, anon_client: AsyncClient
    ) -> None:
        for _ in range(10):
            assert (await confirm(anon_client, new_secret())).status_code == 404
        assert (await confirm(anon_client, new_secret())).status_code == 429

    async def test_confirm_does_not_refund_wrong_guesses(
        self, anon_client: AsyncClient
    ) -> None:
        """A client with a request of its own must not be able to confirm its
        way to extra wrong guesses."""
        limit = pairing.PairingLimits.default().poll_failures._max_failures
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        wrong = 0
        for _ in range(limit // 2):
            assert (await poll(anon_client, new_secret())).status_code == 404
            wrong += 1
        for _ in range(limit):
            await confirm(anon_client, secret)
        while (await poll(anon_client, new_secret())).status_code == 404:
            wrong += 1
        assert wrong <= limit

    async def test_valid_confirms_spend_the_budget(
        self, anon_client: AsyncClient
    ) -> None:
        """Repeating confirm with the right secret is bounded like any other
        request; it is not a free, unlimited endpoint."""
        limit = pairing.PairingLimits.default().poll_failures._max_failures
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        for _ in range(limit):
            assert (await confirm(anon_client, secret)).status_code == 204
        assert (await confirm(anon_client, secret)).status_code == 429

    async def test_a_deleted_device_is_not_redelivered(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        got = (await poll(anon_client, secret)).json()
        await admin.delete(f"/api/v1/devices/{got['device_id']}")
        r = await poll(anon_client, secret)
        assert r.status_code == 403
        assert r.json()["error_code"] == "PAIRING_DENIED"
        assert got["api_key"] not in r.text

    async def test_the_code_stays_reserved_during_the_window(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        """A newcomer can't take the code, and so can't be handed the key."""
        secret = await start_and_approve(anon_client, admin)
        await poll(anon_client, secret)
        squatter = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(squatter))
        assert (await poll(anon_client, squatter)).status_code == 404
        assert (await poll(anon_client, secret)).status_code == 200

    async def test_concurrent_first_polls_do_not_break_anything(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        responses = await asyncio.gather(*(poll(anon_client, secret) for _ in range(4)))
        assert {r.status_code for r in responses} <= {200, 410}
        assert any(r.status_code == 200 for r in responses)
        keys = {r.json()["api_key"] for r in responses if r.status_code == 200}
        assert len(keys) == 1
        assert (await poll(anon_client, secret)).status_code == 200

    async def test_confirm_works_after_pairing_is_switched_off(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = await start_and_approve(anon_client, admin)
        await poll(anon_client, secret)
        r = await admin.put("/api/v1/server-settings", json={"pairing_enabled": False})
        assert r.status_code == 200
        assert (await confirm(anon_client, secret)).status_code == 204


# ---------------------------------------------------------------------------
# Code enumeration
# ---------------------------------------------------------------------------


class TestUniformCreate:
    async def test_a_code_in_use_looks_like_a_free_one(
        self, anon_client: AsyncClient
    ) -> None:
        free = await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        taken = await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        assert free.status_code == taken.status_code == 201
        assert set(free.json()) == set(taken.json())
        assert free.json()["code"] == taken.json()["code"]
        assert free.json()["server_id"] == taken.json()["server_id"]
        assert (
            free.json()["poll_interval_seconds"]
            == taken.json()["poll_interval_seconds"]
        )
        assert "in use" not in taken.text.lower()

    async def test_probing_costs_the_prober_wrong_guess_budget(
        self, anon_client: AsyncClient
    ) -> None:
        await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        for _ in range(9):
            r = await anon_client.post("/api/v1/pairing", json=body(new_secret()))
            assert r.status_code == 201
        # Ten probes used up the create budget (10 per window) ...
        r = await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        assert r.status_code == 429
        # ... and the wrong-guess budget that polls draw on: nine probes were
        # counted, so one more wrong poll fits and then the door is shut.
        assert (await poll(anon_client, new_secret())).status_code == 404
        assert (await poll(anon_client, new_secret())).status_code == 429

    async def test_probing_does_not_reveal_or_disturb_the_owner(
        self, anon_client: AsyncClient
    ) -> None:
        owner = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(owner))
        await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        assert (await poll(anon_client, owner)).json()["status"] == "pending"


# ---------------------------------------------------------------------------
# Sweeping expired requests
# ---------------------------------------------------------------------------


def rows(app: FastAPI) -> list[str]:
    return [
        r[0]
        for r in sqlite3.connect(app.state.db_path)
        .execute("SELECT code FROM pairing_requests ORDER BY code")
        .fetchall()
    ]


async def insert_old_rows(app: FastAPI, clock: datetime) -> None:
    """One request long past its retention, one that only just expired."""
    async with aiosqlite.connect(app.state.db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        for code, expired_ago in (
            ("OLDOLD22", pairing.RETAIN_EXPIRED + timedelta(hours=2)),
            ("NEWNEW33", timedelta(minutes=1)),
        ):
            await conn.execute(
                """
                INSERT INTO pairing_requests
                    (code, secret_hash, status, device_name, display_width,
                     display_height, client, created_at, expires_at)
                VALUES (?, ?, 'pending', '', 640, 480, 'x', ?, ?)
                """,
                (
                    code,
                    pairing.hash_secret(new_secret()),
                    pairing._iso(clock - expired_ago - pairing.CODE_LIFETIME),
                    pairing._iso(clock - expired_ago),
                ),
            )
        await conn.commit()


class TestSweep:
    async def test_purge_expired(self, app: FastAPI, clock: list[datetime]) -> None:
        await insert_old_rows(app, clock[0])
        async with aiosqlite.connect(app.state.db_path) as conn:
            await configure_conn(conn)
            # One is hours past its retention; the other only just expired
            # (kept for an hour so its device is told "expired", not "unknown").
            assert await pairing.purge_expired(conn, clock[0]) == 1
        assert rows(app) == ["NEWNEW33"]

    async def test_startup_sweeps_without_any_new_pairing(
        self, app: FastAPI, clock: list[datetime]
    ) -> None:
        await insert_old_rows(app, clock[0])
        assert "OLDOLD22" in rows(app)
        async with app.router.lifespan_context(app):
            for _ in range(50):
                if "OLDOLD22" not in rows(app):
                    break
                await asyncio.sleep(0.05)
        assert rows(app) == ["NEWNEW33"]

    async def test_timer_sweeps_while_the_server_runs(
        self, app: FastAPI, clock: list[datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pairing, "PURGE_INTERVAL_SECONDS", 0.05)
        async with app.router.lifespan_context(app):
            await insert_old_rows(app, clock[0])
            for _ in range(60):
                if "OLDOLD22" not in rows(app):
                    break
                await asyncio.sleep(0.05)
        assert "OLDOLD22" not in rows(app)

    async def test_loop_stops_at_shutdown_and_survives_a_bad_sweep(
        self,
        app: FastAPI,
        clock: list[datetime],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real = pairing.purge_expired
        calls = 0

        async def flaky(conn: aiosqlite.Connection, now: datetime) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return await real(conn, now)

        monkeypatch.setattr(pairing, "purge_expired", flaky)
        await insert_old_rows(app, clock[0])
        stop = asyncio.Event()
        with caplog.at_level(logging.ERROR, logger="kidsplay_server"):
            task = asyncio.create_task(
                pairing.run_purge_loop(app.state.db_path, stop, interval=0.02)
            )
            for _ in range(100):
                if "OLDOLD22" not in rows(app):
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(task, 2)
        assert "OLDOLD22" not in rows(app)
        assert calls >= 2
        assert "Could not remove expired pairing requests" in caplog.text


# ---------------------------------------------------------------------------
# Server identity
# ---------------------------------------------------------------------------


async def make_device(
    admin: AsyncClient,
) -> tuple[str, str]:
    pid = (await admin.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
    device = (
        await admin.post("/api/v1/devices", json={"name": "P", "profile_id": pid})
    ).json()
    return device["id"], device["api_key"]


class TestServerIdentity:
    async def test_created_once_and_stable(self, tmp_path: Path) -> None:
        async with aiosqlite.connect(tmp_path / "id.db") as conn:
            await configure_conn(conn)
            await init_db(conn)
            first = await get_or_create_server_id(conn)
            assert await get_or_create_server_id(conn) == first
        async with aiosqlite.connect(tmp_path / "id.db") as conn:
            await configure_conn(conn)
            assert await get_or_create_server_id(conn) == first
        async with aiosqlite.connect(tmp_path / "other.db") as conn:
            await configure_conn(conn)
            await init_db(conn)
            assert await get_or_create_server_id(conn) != first

    async def test_racing_creators_agree(self, tmp_path: Path) -> None:
        async with aiosqlite.connect(tmp_path / "r.db") as setup:
            await configure_conn(setup)
            await init_db(setup)

        async def one() -> str:
            async with aiosqlite.connect(tmp_path / "r.db") as conn:
                await configure_conn(conn)
                return await get_or_create_server_id(conn)

        ids = await asyncio.gather(*(one() for _ in range(6)))
        assert len(set(ids)) == 1

    async def test_pairing_reports_it_on_create_and_delivery(
        self, anon_client: AsyncClient, admin: AsyncClient, app: FastAPI
    ) -> None:
        secret = new_secret()
        created = (await anon_client.post("/api/v1/pairing", json=body(secret))).json()
        assert created["server_id"]
        pending = (await poll(anon_client, secret)).json()
        assert pending["server_id"] is None  # nothing to pin before approval
        pid = (await admin.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
        await admin.post(
            "/api/v1/pairing/approve", json={"code": CODE, "profile_id": pid}
        )
        delivered = (await poll(anon_client, secret)).json()
        assert delivered["server_id"] == created["server_id"]
        async with aiosqlite.connect(app.state.db_path) as conn:
            await configure_conn(conn)
            assert delivered["server_id"] == await get_or_create_server_id(conn)

    async def test_manifest_carries_it_on_200_and_304(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        device_id, api_key = await make_device(admin)
        auth = {"Authorization": f"Bearer {api_key}"}
        r = await anon_client.get(f"/api/v1/devices/{device_id}/manifest", headers=auth)
        assert r.status_code == 200
        server_id = r.headers[SERVER_ID_HEADER]
        assert server_id
        again = await anon_client.get(
            f"/api/v1/devices/{device_id}/manifest",
            headers={**auth, "If-None-Match": r.headers["etag"]},
        )
        assert again.status_code == 304
        assert again.headers[SERVER_ID_HEADER] == server_id

    async def test_manifest_error_does_not_carry_it(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        device_id, _ = await make_device(admin)
        r = await anon_client.get(f"/api/v1/devices/{device_id}/manifest")
        assert r.status_code == 401
        assert SERVER_ID_HEADER not in r.headers

    async def test_identity_does_not_change_the_manifest_hash(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        """Devices already paired keep their ETag: no re-download on upgrade."""
        device_id, api_key = await make_device(admin)
        auth = {"Authorization": f"Bearer {api_key}"}
        first = await anon_client.get(
            f"/api/v1/devices/{device_id}/manifest", headers=auth
        )
        assert SERVER_ID_HEADER not in first.json()
        assert first.json()["manifest_hash"] == first.headers["etag"]

    async def test_survives_a_restart(self, tmp_path: Path) -> None:
        ids = []
        for _ in range(2):
            app = create_app(tmp_path / "db.sqlite", tmp_path / "media")
            async with (
                app.router.lifespan_context(app),
                aiosqlite.connect(tmp_path / "db.sqlite") as conn,
            ):
                await configure_conn(conn)
                ids.append(await get_or_create_server_id(conn))
        assert ids[0] == ids[1]

    async def test_admin_page_shows_the_short_id(
        self, app: FastAPI, admin: AsyncClient
    ) -> None:
        async with aiosqlite.connect(app.state.db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            server_id = await get_or_create_server_id(conn)
        r = await admin.get("/devices")
        assert r.status_code == 200
        assert short_server_id(server_id) in r.text
        assert server_id not in r.text  # only the short form is shown


def test_short_server_id() -> None:
    assert short_server_id("3f2a9c1e-8b47-4d1a-9c55-0e6a7d2b1f90") == "3F2A-9C1E"
    assert short_server_id("3f2a9c1e8b47") == "3F2A-9C1E"
    assert short_server_id("ab") == "AB"
    assert short_server_id("") == ""
    assert short_server_id("zzzz") == ""
