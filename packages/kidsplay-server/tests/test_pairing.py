"""Tests for on-device pairing: request, admin approval, one-time key delivery.

Covers the flow end to end and the security properties: single use, expiry,
binding secret, disabled pairing, rate limits, admin-only approval with CSRF,
and that nothing sensitive is stored in the clear or logged.
"""

import asyncio
import logging
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from kidsplay_server import pairing
from kidsplay_server.api.app import create_app
from kidsplay_server.pairing import (
    CODE_ALPHABET,
    CODE_LENGTH,
    CODE_LIFETIME,
    PairingLimits,
)

CODE = "ABCD-2345"
CODE_KEY = "ABCD2345"


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
    """A movable clock: ``clock[0]`` is now; assign to travel in time."""
    state = [pairing.utcnow()]
    monkeypatch.setattr(pairing, "utcnow", lambda: state[0])
    yield state


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def body(secret: str, code: str = CODE, **extra: object) -> dict[str, object]:
    return {"code": code, "binding_secret": secret, **extra}


async def profile_id(admin: AsyncClient) -> str:
    r = await admin.post("/api/v1/profiles", json={"name": "Leo"})
    return r.json()["id"]


async def approve(
    admin: AsyncClient, pid: str, code: str = CODE, name: str | None = "Leo's Player"
) -> Response:
    return await admin.post(
        "/api/v1/pairing/approve", json={"code": code, "profile_id": pid, "name": name}
    )


class TestCodes:
    def test_normalize(self) -> None:
        assert pairing.normalize_code("abcd-2345") == CODE_KEY
        assert pairing.normalize_code(" ab cd 23 45 ") == CODE_KEY
        assert pairing.normalize_code("ABCD-234") is None
        assert pairing.normalize_code("ABCD-23450") is None
        # Ambiguous characters are not in the alphabet.
        assert pairing.normalize_code("ABCD-0O1I") is None
        assert pairing.normalize_code("") is None

    def test_format(self) -> None:
        assert pairing.format_code(CODE_KEY) == CODE

    def test_alphabet_has_no_lookalikes(self) -> None:
        assert not set("01OIL") & set(CODE_ALPHABET)
        assert len(set(CODE_ALPHABET)) == len(CODE_ALPHABET)

    def test_hash_is_not_the_secret(self) -> None:
        secret = new_secret()
        assert pairing.hash_secret(secret) != secret
        assert len(pairing.hash_secret(secret)) == 64

    def test_guessing_math(self) -> None:
        """The numbers quoted in the docs: about 8.5e11 codes, 39.6 bits."""
        assert len(CODE_ALPHABET) ** CODE_LENGTH == 852_891_037_441


class TestFlow:
    async def test_request_approve_collect(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = new_secret()
        r = await anon_client.post(
            "/api/v1/pairing",
            json=body(
                secret,
                code=CODE.lower(),
                device_name="Handheld",
                display_width=800,
                display_height=600,
            ),
        )
        assert r.status_code == 201
        assert r.json()["code"] == CODE_KEY
        assert "binding_secret" not in r.text
        assert r.json()["poll_interval_seconds"] == pairing.POLL_INTERVAL_SECONDS

        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 200
        assert r.json() == {
            "status": "pending",
            "device_id": None,
            "api_key": None,
            "server_id": None,
        }

        pid = await profile_id(admin)
        r = await approve(admin, pid, code="abcd 2345")
        assert r.status_code == 200
        assert r.json()["name"] == "Leo's Player"
        assert "api_key" not in r.text

        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 200
        got = r.json()
        assert got["status"] == "approved"
        device = (await admin.get(f"/api/v1/devices/{got['device_id']}")).json()
        assert got["api_key"] == device["api_key"]
        assert device["profile_id"] == pid
        assert (device["display_width"], device["display_height"]) == (800, 600)

        # The delivered key is a working device credential.
        r = await anon_client.get(
            f"/api/v1/devices/{got['device_id']}/manifest",
            headers={"Authorization": f"Bearer {got['api_key']}"},
        )
        assert r.status_code == 200

    async def test_device_suggested_name_is_the_default(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        await anon_client.post(
            "/api/v1/pairing", json=body(new_secret(), device_name="Blue one")
        )
        r = await approve(admin, await profile_id(admin), name=None)
        assert r.json()["name"] == "Blue one"

    async def test_unapproved_never_gets_a_key(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        for _ in range(3):
            r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
            assert r.json()["api_key"] is None
        assert (await admin.get("/api/v1/devices")).json() == []

    async def test_key_is_delivered_once(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        """Once the device confirms, nothing can read the key through pairing."""
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        await approve(admin, await profile_id(admin))
        first = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert first.json()["api_key"]
        confirmed = await anon_client.post("/api/v1/pairing/confirm", json=body(secret))
        assert confirmed.status_code == 204
        again = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert again.status_code == 410
        assert again.json()["error_code"] == "PAIRING_USED"
        assert first.json()["api_key"] not in again.text

    async def test_wrong_secret_gets_nothing_and_cannot_burn_the_code(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        await approve(admin, await profile_id(admin))
        wrong = await anon_client.post("/api/v1/pairing/poll", json=body(new_secret()))
        assert wrong.status_code == 404
        assert wrong.json()["error_code"] == "PAIRING_NOT_FOUND"
        assert "api_key" not in wrong.text
        # The real device still collects afterwards.
        ok = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert ok.json()["status"] == "approved"

    async def test_unknown_code_looks_like_wrong_secret(
        self, anon_client: AsyncClient
    ) -> None:
        await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        wrong = await anon_client.post("/api/v1/pairing/poll", json=body(new_secret()))
        unknown = await anon_client.post(
            "/api/v1/pairing/poll", json=body(new_secret(), code="ZZZZ-9999")
        )
        assert (wrong.status_code, wrong.json()) == (
            unknown.status_code,
            unknown.json(),
        )

    async def test_denied(self, anon_client: AsyncClient, admin: AsyncClient) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        r = await admin.post("/api/v1/pairing/deny", json={"code": CODE})
        assert r.status_code == 204
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 403
        assert r.json()["error_code"] == "PAIRING_DENIED"
        assert (await approve(admin, await profile_id(admin))).status_code == 409

    async def test_device_deleted_before_collecting(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        device_id = (await approve(admin, await profile_id(admin))).json()["device_id"]
        await admin.delete(f"/api/v1/devices/{device_id}")
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 403
        assert "api_key" not in r.text


class TestExpiryAndReuse:
    async def test_expired_before_approval(
        self, anon_client: AsyncClient, admin: AsyncClient, clock: list[datetime]
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        clock[0] += CODE_LIFETIME + timedelta(seconds=1)
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 410
        assert r.json()["error_code"] == "PAIRING_EXPIRED"
        r = await approve(admin, await profile_id(admin))
        assert r.status_code == 410
        assert (await admin.get("/api/v1/pairing/requests")).json() == []

    async def test_valid_just_inside_the_window(
        self, anon_client: AsyncClient, admin: AsyncClient, clock: list[datetime]
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        clock[0] += CODE_LIFETIME - timedelta(seconds=1)
        assert (await approve(admin, await profile_id(admin))).status_code == 200
        # Approval near the end still leaves time to collect.
        clock[0] += timedelta(seconds=30)
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.json()["status"] == "approved"

    async def test_approved_but_uncollected_expires(
        self, anon_client: AsyncClient, admin: AsyncClient, clock: list[datetime]
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        await approve(admin, await profile_id(admin))
        clock[0] += CODE_LIFETIME + pairing.COLLECT_GRACE + timedelta(seconds=1)
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 410
        assert "api_key" not in r.text

    async def test_live_code_cannot_be_taken_over(
        self, anon_client: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        intruder = new_secret()
        r = await anon_client.post("/api/v1/pairing", json=body(intruder))
        # Answered like a new request (see test_pairing_followups), but nothing
        # was stored for the intruder's secret...
        assert r.status_code == 201
        r = await anon_client.post("/api/v1/pairing/poll", json=body(intruder))
        assert r.status_code == 404
        # ...and the original holder is unaffected.
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.json()["status"] == "pending"

    async def test_finished_or_expired_code_can_be_reused(
        self, anon_client: AsyncClient, admin: AsyncClient, clock: list[datetime]
    ) -> None:
        old = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(old))
        await approve(admin, await profile_id(admin))
        await anon_client.post("/api/v1/pairing/poll", json=body(old))
        # Delivered but not yet expired: still taken. The newcomer's request is
        # not stored, so its secret gets nothing.
        squatter = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(squatter))
        r = await anon_client.post("/api/v1/pairing/poll", json=body(squatter))
        assert r.status_code == 404
        clock[0] += CODE_LIFETIME + pairing.COLLECT_GRACE + timedelta(seconds=1)
        fresh = new_secret()
        assert (
            await anon_client.post("/api/v1/pairing", json=body(fresh))
        ).status_code == 201
        # The old secret no longer works; the new one does.
        r = await anon_client.post("/api/v1/pairing/poll", json=body(old))
        assert r.status_code == 404
        r = await anon_client.post("/api/v1/pairing/poll", json=body(fresh))
        assert r.json()["status"] == "pending"

    async def test_old_expired_rows_are_purged(
        self, anon_client: AsyncClient, app: FastAPI, clock: list[datetime]
    ) -> None:
        await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        clock[0] += CODE_LIFETIME + pairing.RETAIN_EXPIRED + timedelta(minutes=1)
        await anon_client.post(
            "/api/v1/pairing", json=body(new_secret(), code="WXYZ7777")
        )
        rows = (
            sqlite3.connect(app.state.db_path)
            .execute("SELECT code FROM pairing_requests")
            .fetchall()
        )
        assert rows == [("WXYZ7777",)]

    async def test_racing_registrations_of_one_code(
        self, anon_client: AsyncClient
    ) -> None:
        """Exactly one is stored; the rest get a clean answer, never a 500."""
        secrets_ = [new_secret() for _ in range(4)]
        responses = await asyncio.gather(
            *(anon_client.post("/api/v1/pairing", json=body(s)) for s in secrets_)
        )
        assert [r.status_code for r in responses] == [201] * 4
        polls = [
            await anon_client.post("/api/v1/pairing/poll", json=body(s))
            for s in secrets_
        ]
        assert sorted(r.status_code for r in polls) == [200] + [404] * 3

    async def test_second_approval_is_refused(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        pid = await profile_id(admin)
        assert (await approve(admin, pid)).status_code == 200
        r = await approve(admin, pid)
        assert r.status_code == 409
        assert r.json()["error_code"] == "PAIRING_NOT_PENDING"
        assert len((await admin.get("/api/v1/devices")).json()) == 1


class TestValidation:
    @pytest.mark.parametrize(
        "code", ["", "ABC", "ABCD-234O", "ABCD-23456", "😀😀😀😀😀😀😀😀"]
    )
    async def test_bad_code(self, anon_client: AsyncClient, code: str) -> None:
        r = await anon_client.post("/api/v1/pairing", json=body(new_secret(), code))
        assert r.status_code == 422

    async def test_short_secret_refused(self, anon_client: AsyncClient) -> None:
        r = await anon_client.post("/api/v1/pairing", json=body("short"))
        assert r.status_code == 422

    async def test_unknown_profile(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        r = await approve(admin, "00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404
        # Still pending: the parent can retry with a real profile.
        assert (await approve(admin, await profile_id(admin))).status_code == 200

    async def test_approve_unknown_code(self, admin: AsyncClient) -> None:
        r = await approve(admin, await profile_id(admin), code="ZZZZ-9999")
        assert r.status_code == 404

    async def test_approval_needs_the_exact_code(
        self, admin: AsyncClient, anon_client: AsyncClient
    ) -> None:
        """A near miss approves nothing; the request stays waiting."""
        await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        pid = await profile_id(admin)
        for wrong in ("ABCD-2346", "ABCD-234", "ABCE-2345"):
            assert (await approve(admin, pid, code=wrong)).status_code in (404, 422)
        pending = (await admin.get("/api/v1/pairing/requests")).json()
        assert [p["code"] for p in pending] == [CODE_KEY]
        assert (await approve(admin, pid)).status_code == 200


class TestDisabled:
    async def disable(self, admin: AsyncClient) -> None:
        r = await admin.put("/api/v1/server-settings", json={"pairing_enabled": False})
        assert r.status_code == 200
        assert r.json()["values"]["pairing_enabled"] is False

    async def test_default_is_enabled(self, admin: AsyncClient) -> None:
        r = await admin.get("/api/v1/server-settings")
        assert r.json()["values"]["pairing_enabled"] is True

    async def test_disabled_refuses_everything(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        pid = await profile_id(admin)
        await self.disable(admin)

        r = await anon_client.post(
            "/api/v1/pairing", json=body(new_secret(), "WXYZ7777")
        )
        assert (r.status_code, r.json()["error_code"]) == (403, "PAIRING_DISABLED")
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert (r.status_code, r.json()["error_code"]) == (403, "PAIRING_DISABLED")
        r = await approve(admin, pid)
        assert (r.status_code, r.json()["error_code"]) == (403, "PAIRING_DISABLED")
        assert (await admin.get("/api/v1/devices")).json() == []

    async def test_disabling_stops_an_approved_pairing_from_delivering(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        await approve(admin, await profile_id(admin))
        await self.disable(admin)
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 403
        assert "api_key" not in r.text

    async def test_env_can_pin_it_off(
        self,
        anon_client: AsyncClient,
        admin: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_PAIRING_ENABLED", "false")
        r = await anon_client.post("/api/v1/pairing", json=body(new_secret()))
        assert r.status_code == 403
        r = await admin.put("/api/v1/server-settings", json={"pairing_enabled": True})
        assert r.status_code == 409


class TestRateLimits:
    async def test_create_is_limited_per_client(self, anon_client: AsyncClient) -> None:
        limit = PairingLimits.default().create._max_failures
        codes = [
            "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            for _ in range(limit + 1)
        ]
        statuses = [
            (
                await anon_client.post("/api/v1/pairing", json=body(new_secret(), code))
            ).status_code
            for code in codes
        ]
        assert statuses[:limit] == [201] * limit
        assert statuses[limit] == 429

    async def test_attempts_are_counted_before_any_work(
        self, anon_client: AsyncClient, app: FastAPI
    ) -> None:
        """Even invalid requests use up the budget, and a blocked client is
        refused before the body is looked at."""
        limit = PairingLimits.default().create._max_failures
        for _ in range(limit):
            r = await anon_client.post("/api/v1/pairing", json={"code": "x"})
            assert r.status_code == 422
        r = await anon_client.post("/api/v1/pairing", json={"code": "x"})
        assert r.status_code == 429
        assert r.json()["error_code"] == "RATE_LIMITED"

    async def test_repeated_wrong_secrets_lock_the_client_out(
        self, anon_client: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        limit = PairingLimits.default().poll_failures._max_failures
        for _ in range(limit):
            r = await anon_client.post("/api/v1/pairing/poll", json=body(new_secret()))
            assert r.status_code == 404
        r = await anon_client.post("/api/v1/pairing/poll", json=body(new_secret()))
        assert r.status_code == 429
        # Blocked even with the right secret: the client is throttled, not the code.
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 429

    async def test_wrong_polls_are_counted_before_the_lookup(
        self, anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrent wrong guesses can't all get past the limit while the
        database lookups are in flight: at most the budget reaches the DB."""
        limit = PairingLimits.default().poll_failures._max_failures
        lookups = 0
        real_poll = pairing.poll

        async def counting_poll(
            conn: aiosqlite.Connection, *, code: str, secret: str, now: datetime
        ) -> tuple[pairing.PollOutcome, pairing.DeliveredKey | None]:
            nonlocal lookups
            lookups += 1
            return await real_poll(conn, code=code, secret=secret, now=now)

        monkeypatch.setattr(pairing, "poll", counting_poll)
        responses = await asyncio.gather(
            *(
                anon_client.post("/api/v1/pairing/poll", json=body(new_secret()))
                for _ in range(300)
            )
        )
        statuses = [r.status_code for r in responses]
        assert lookups == limit
        assert statuses.count(404) == limit
        assert statuses.count(429) == 300 - limit

    async def test_a_successful_poll_does_not_erase_earlier_wrong_guesses(
        self, anon_client: AsyncClient
    ) -> None:
        """Polling a request of its own between guesses must not reset the
        counter, or an attacker could guess forever."""
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        limit = PairingLimits.default().poll_failures._max_failures
        for _ in range(limit):
            r = await anon_client.post("/api/v1/pairing/poll", json=body(new_secret()))
            assert r.status_code == 404
        r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        assert r.status_code == 429

    async def test_normal_polling_is_not_limited(
        self, anon_client: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        # 10 minutes at one poll per 3 seconds is 200 polls, spread over time;
        # a burst of 40 is well inside the per-minute budget.
        for _ in range(40):
            r = await anon_client.post("/api/v1/pairing/poll", json=body(secret))
            assert r.status_code == 200

    async def test_pending_table_is_bounded(
        self, anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, app: FastAPI
    ) -> None:
        monkeypatch.setattr(pairing, "MAX_PENDING", 2)
        codes = ["AAAAAAAA", "BBBBBBBB", "CCCCCCCC"]
        statuses = [
            (
                await anon_client.post("/api/v1/pairing", json=body(new_secret(), c))
            ).status_code
            for c in codes
        ]
        assert statuses == [201, 201, 429]

    async def test_limits_are_per_client(self, app: FastAPI) -> None:
        limit = PairingLimits.default().create._max_failures
        transports = [
            ASGITransport(app=app, client=(ip, 1234)) for ip in ("10.0.0.1", "10.0.0.2")
        ]
        async with (
            AsyncClient(transport=transports[0], base_url="http://t") as a,
            AsyncClient(transport=transports[1], base_url="http://t") as b,
        ):
            for _ in range(limit):
                await a.post("/api/v1/pairing", json={"code": "x"})
            assert (
                await a.post("/api/v1/pairing", json={"code": "x"})
            ).status_code == 429
            assert (
                await b.post("/api/v1/pairing", json={"code": "x"})
            ).status_code == 422


class TestAdminOnly:
    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/api/v1/pairing/requests", None),
            ("POST", "/api/v1/pairing/approve", {"code": CODE, "profile_id": "x"}),
            ("POST", "/api/v1/pairing/deny", {"code": CODE}),
        ],
    )
    async def test_anonymous_refused(
        self, anon_client: AsyncClient, method: str, path: str, payload: object
    ) -> None:
        r = await anon_client.request(method, path, json=payload)
        assert r.status_code == 401

    async def test_device_key_is_not_admin(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        await approve(admin, await profile_id(admin))
        key = (
            await anon_client.post("/api/v1/pairing/poll", json=body(secret))
        ).json()["api_key"]
        await anon_client.post("/api/v1/pairing", json=body(new_secret(), "WXYZ7777"))
        r = await anon_client.post(
            "/api/v1/pairing/approve",
            headers={"Authorization": f"Bearer {key}"},
            json={"code": "WXYZ7777", "profile_id": await profile_id(admin)},
        )
        assert r.status_code == 401

    async def test_session_approval_needs_csrf(
        self, app: FastAPI, admin_password: str, admin_headers: dict[str, str]
    ) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as browser:
            page = await browser.get("/login")
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
            assert token
            r = await browser.post(
                "/login",
                data={"password": admin_password, "csrf_token": token.group(1)},
            )
            assert r.status_code == 303
            # Logging in issues a fresh CSRF token.
            page = await browser.get("/devices")
            csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
            assert csrf
            pid = (
                await browser.post(
                    "/api/v1/profiles",
                    json={"name": "Leo"},
                    headers={"X-CSRF-Token": csrf.group(1)},
                )
            ).json()["id"]
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as device:
                await device.post("/api/v1/pairing", json=body(new_secret()))
            forged = await browser.post(
                "/api/v1/pairing/approve", json={"code": CODE, "profile_id": pid}
            )
            assert forged.status_code == 403
            assert forged.json()["error_code"] == "CSRF_FAILED"
            ok = await browser.post(
                "/api/v1/pairing/approve",
                json={"code": CODE, "profile_id": pid},
                headers={"X-CSRF-Token": csrf.group(1)},
            )
            assert ok.status_code == 200

    async def test_list_pending(
        self, anon_client: AsyncClient, admin: AsyncClient
    ) -> None:
        await anon_client.post(
            "/api/v1/pairing", json=body(new_secret(), device_name="Blue")
        )
        rows = (await admin.get("/api/v1/pairing/requests")).json()
        assert [(r["code"], r["device_name"]) for r in rows] == [(CODE_KEY, "Blue")]
        assert "secret" not in str(rows).lower()


class TestSecretsAtRest:
    async def test_only_a_hash_of_the_secret_is_stored(
        self, anon_client: AsyncClient, admin: AsyncClient, app: FastAPI
    ) -> None:
        secret = new_secret()
        await anon_client.post("/api/v1/pairing", json=body(secret))
        raw = Path(app.state.db_path).read_bytes()
        assert secret.encode() not in raw
        row = (
            sqlite3.connect(app.state.db_path)
            .execute("SELECT secret_hash FROM pairing_requests")
            .fetchone()
        )
        assert row[0] == pairing.hash_secret(secret)

    async def test_nothing_sensitive_is_logged(
        self,
        anon_client: AsyncClient,
        admin: AsyncClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        # Production quiets these (logging_setup.configure_logging); at DEBUG
        # aiosqlite logs every bound SQL parameter, for any query.
        noisy_loggers = [logging.getLogger(n) for n in ("aiosqlite", "httpx")]
        previous = [log.level for log in noisy_loggers]
        for log in noisy_loggers:
            log.setLevel(logging.WARNING)
        try:
            secret = new_secret()
            await anon_client.post("/api/v1/pairing", json=body(secret))
            await anon_client.post("/api/v1/pairing/poll", json=body(new_secret()))
            await approve(admin, await profile_id(admin))
            key = (
                await anon_client.post("/api/v1/pairing/poll", json=body(secret))
            ).json()["api_key"]
            await anon_client.post("/api/v1/pairing/poll", json=body(secret))
            assert "Pairing" in caplog.text or "pairing" in caplog.text
            for sensitive in (secret, key, CODE_KEY, CODE):
                assert sensitive not in caplog.text
        finally:
            for log, level in zip(noisy_loggers, previous, strict=True):
                log.setLevel(level)
