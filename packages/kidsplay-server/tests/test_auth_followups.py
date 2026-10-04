"""Tests for the admin-auth follow-ups of #23.

* argon2 runs on a worker thread, not on the event loop
* disabled mode refuses cross-site writes from browsers
* an env-seeded password obeys the setup page's length rules
* the log stream notices a logout
* change password / log out all sessions
"""

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from kidsplay_models.auth import MAX_PASSWORD_LENGTH
from kidsplay_server import auth
from kidsplay_server.api.app import create_app
from kidsplay_server.auth import (
    MIN_PASSWORD_LENGTH,
    AuthConfig,
    change_admin_password,
    check_admin_password,
    create_admin_token,
    create_session,
    delete_sessions,
    init_auth_db,
    is_admin_token_valid,
    is_session_valid,
    set_initial_admin_password,
)
from kidsplay_server.database import configure_conn, init_db
from kidsplay_server.logging_setup import LogBroadcaster, LogEntry
from kidsplay_server.web.routes import stream_log_events

PASSWORD = "a-good-long-password"
ZERO_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(tmp_path / "test.db") as c:
        await configure_conn(c)
        await init_db(c)
        await init_auth_db(c)
        yield c


# ---------------------------------------------------------------------------
# argon2 off the event loop
# ---------------------------------------------------------------------------


class TestPasswordHashingOffTheLoop:
    async def test_verify_runs_on_a_worker_thread(
        self, conn: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await set_initial_admin_password(conn, PASSWORD)
        real = auth.verify_password
        seen: list[int] = []

        def spy(password_hash: str, password: str) -> bool:
            seen.append(threading.get_ident())
            return real(password_hash, password)

        monkeypatch.setattr(auth, "verify_password", spy)
        assert await check_admin_password(conn, PASSWORD)
        assert not await check_admin_password(conn, "wrong")
        assert len(seen) == 2
        assert threading.get_ident() not in seen

    async def test_hashing_runs_on_a_worker_thread(
        self, conn: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = auth.hash_password
        seen: list[int] = []

        def spy(password: str) -> str:
            seen.append(threading.get_ident())
            return real(password)

        monkeypatch.setattr(auth, "hash_password", spy)
        assert await set_initial_admin_password(conn, PASSWORD)
        await change_admin_password(conn, "another-good-password")
        assert len(seen) == 2
        assert threading.get_ident() not in seen
        assert await check_admin_password(conn, "another-good-password")

    async def test_slow_hash_does_not_stall_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A login flood must not freeze device sync and the log stream."""
        app = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(admin_password=PASSWORD),
        )
        real = auth.verify_password

        def slow(password_hash: str, password: str) -> bool:
            time.sleep(0.4)  # what a slow argon2 does to whoever runs it
            return real(password_hash, password)

        monkeypatch.setattr(auth, "verify_password", slow)
        worst_gap = 0.0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal worst_gap
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                worst_gap = max(worst_gap, now - last)
                last = now

        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
        ):
            tick = asyncio.create_task(ticker())
            r = await c.post("/api/v1/auth/login", json={"password": "wrong"})
            stop.set()
            await tick
        assert r.status_code == 401
        assert worst_gap < 0.2, f"event loop was blocked for {worst_gap:.2f}s"

    async def test_concurrent_hashes_are_bounded(
        self, conn: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each argon2 run takes ~64 MiB: a flood must not run dozens at once."""
        await set_initial_admin_password(conn, PASSWORD)
        real = auth.verify_password
        lock = threading.Lock()
        running = 0
        peak = 0

        def counting(password_hash: str, password: str) -> bool:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1
            return real(password_hash, password)

        monkeypatch.setattr(auth, "verify_password", counting)
        await asyncio.gather(*(check_admin_password(conn, "x") for _ in range(12)))
        assert 1 <= peak <= auth._HASH_THREADS


# ---------------------------------------------------------------------------
# Disabled mode: cross-site writes
# ---------------------------------------------------------------------------


@pytest.fixture
async def open_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """A client for a server behind an authenticating proxy (auth disabled)."""
    app = create_app(
        tmp_path / "test.db", tmp_path / "media", AuthConfig(disabled=True)
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://kids.home") as c,
    ):
        yield c


class TestDisabledModeCsrf:
    """Behind e.g. Authelia the proxy's cookie rides along with any request a
    page on a sibling site makes, so browsers' own headers must decide."""

    async def write(self, c: AsyncClient, headers: dict[str, str]) -> int:
        r = await c.post("/api/v1/profiles", json={"name": "Leo"}, headers=headers)
        return r.status_code

    @pytest.mark.parametrize("site", ["same-origin", "none", "SAME-ORIGIN"])
    async def test_same_origin_browsers_may_write(
        self, open_client: AsyncClient, site: str
    ) -> None:
        assert await self.write(open_client, {"Sec-Fetch-Site": site}) == 201

    @pytest.mark.parametrize("site", ["cross-site", "same-site", "", "weird"])
    async def test_other_sites_may_not(
        self, open_client: AsyncClient, site: str
    ) -> None:
        headers = {"Sec-Fetch-Site": site}
        assert await self.write(open_client, headers) == 403
        r = await open_client.get("/api/v1/profiles")
        assert r.json() == []

    async def test_error_shape(self, open_client: AsyncClient) -> None:
        r = await open_client.post(
            "/api/v1/profiles",
            json={"name": "Leo"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert r.status_code == 403
        assert r.json()["error_code"] == "CSRF_FAILED"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/v1/profiles"),
            ("PUT", "/api/v1/server-settings"),
            ("PATCH", f"/api/v1/devices/{ZERO_ID}"),
            ("DELETE", f"/api/v1/profiles/{ZERO_ID}"),
        ],
    )
    async def test_every_unsafe_method_is_checked(
        self, open_client: AsyncClient, method: str, path: str
    ) -> None:
        r = await open_client.request(
            method, path, headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert r.status_code == 403
        assert r.json()["error_code"] == "CSRF_FAILED"
        # Same request from our own pages gets past the check (and is judged
        # on its merits: no body, unknown id, ...).
        r = await open_client.request(
            method, path, headers={"Sec-Fetch-Site": "same-origin"}
        )
        assert r.status_code != 403

    async def test_the_fetch_metadata_header_wins_over_origin(
        self, open_client: AsyncClient
    ) -> None:
        headers = {"Sec-Fetch-Site": "cross-site", "Origin": "http://kids.home"}
        assert await self.write(open_client, headers) == 403

    async def test_old_browsers_are_judged_by_origin(
        self, open_client: AsyncClient
    ) -> None:
        assert await self.write(open_client, {"Origin": "http://kids.home"}) == 201
        assert await self.write(open_client, {"Origin": "HTTP://KIDS.HOME"}) == 201
        assert await self.write(open_client, {"Origin": "http://evil.example"}) == 403
        # A sibling under the same parent domain is not us.
        assert await self.write(open_client, {"Origin": "http://evil.home"}) == 403
        assert await self.write(open_client, {"Origin": "http://kids.home:81"}) == 403
        assert await self.write(open_client, {"Origin": "null"}) == 403
        assert await self.write(open_client, {"Origin": ""}) == 403
        assert await self.write(open_client, {"Origin": "not a url"}) == 403
        assert await self.write(open_client, {"Origin": "http://kids.home@evil"}) == 403

    async def test_default_ports_do_not_matter(self, open_client: AsyncClient) -> None:
        headers = {"Origin": "http://kids.home", "Host": "kids.home:80"}
        assert await self.write(open_client, headers) == 201
        headers = {"Origin": "https://kids.home", "Host": "kids.home:443"}
        assert await self.write(open_client, headers) == 201
        headers = {"Origin": "http://kids.home", "Host": "kids.home:8000"}
        assert await self.write(open_client, headers) == 403

    async def test_origin_may_match_the_proxys_forwarded_host(
        self, open_client: AsyncClient
    ) -> None:
        """Proxies that rewrite ``Host`` still pass their own name along."""
        headers = {
            "Origin": "https://kidsplay.example.com",
            "X-Forwarded-Host": "kidsplay.example.com",
        }
        assert await self.write(open_client, headers) == 201
        headers = {"Origin": "https://evil.example", "X-Forwarded-Host": "kidsplay"}
        assert await self.write(open_client, headers) == 403

    async def test_non_browser_clients_are_unaffected(
        self, open_client: AsyncClient
    ) -> None:
        """The CLI and scripts send neither header."""
        assert await self.write(open_client, {}) == 201

    async def test_reads_are_never_blocked(self, open_client: AsyncClient) -> None:
        headers = {"Sec-Fetch-Site": "cross-site"}
        assert (
            await open_client.get("/api/v1/profiles", headers=headers)
        ).status_code == 200
        assert (await open_client.get("/", headers=headers)).status_code == 200

    async def test_web_pages_that_write_are_covered_too(
        self, open_client: AsyncClient
    ) -> None:
        r = await open_client.post(
            "/web/photo-proxy",
            json={"url": "http://example.invalid/x.png"},
            headers={"Sec-Fetch-Site": "same-site"},
        )
        assert r.status_code == 403

    async def test_enabled_mode_is_unchanged(self, tmp_path: Path) -> None:
        """With auth on, the CSRF token (not fetch metadata) is the defence."""
        app = create_app(tmp_path / "e.db", tmp_path / "media")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post(
                "/api/v1/profiles",
                json={"name": "Leo"},
                headers={"Sec-Fetch-Site": "cross-site"},
            )
            assert r.status_code == 401


# ---------------------------------------------------------------------------
# The env-seeded password
# ---------------------------------------------------------------------------


class TestSeededPasswordRules:
    async def start(self, tmp_path: Path, password: str) -> FastAPI:
        app = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(admin_password=password),
        )
        async with app.router.lifespan_context(app):
            pass
        return app

    @pytest.mark.parametrize("password", ["short", "x" * (MIN_PASSWORD_LENGTH - 1)])
    async def test_too_short_seed_is_refused_on_a_fresh_install(
        self, tmp_path: Path, password: str
    ) -> None:
        with pytest.raises(RuntimeError, match="KIDSPLAY_ADMIN_PASSWORD"):
            await self.start(tmp_path, password)
        async with aiosqlite.connect(tmp_path / "test.db") as conn:
            await configure_conn(conn)
            await init_auth_db(conn)
            async with conn.execute("SELECT COUNT(*) FROM admin_account") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0

    async def test_too_long_seed_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="longer than"):
            await self.start(tmp_path, "x" * (MAX_PASSWORD_LENGTH + 1))

    async def test_minimum_length_seed_works(self, tmp_path: Path) -> None:
        await self.start(tmp_path, "x" * MIN_PASSWORD_LENGTH)
        async with aiosqlite.connect(tmp_path / "test.db") as conn:
            await configure_conn(conn)
            assert await check_admin_password(conn, "x" * MIN_PASSWORD_LENGTH)

    async def test_existing_install_with_a_short_leftover_still_starts(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Upgrading must not stop a server whose password is already set."""
        await self.start(tmp_path, PASSWORD)
        with caplog.at_level("WARNING", logger="kidsplay_server"):
            await self.start(tmp_path, "short")
        assert "KIDSPLAY_ADMIN_PASSWORD is shorter" in caplog.text
        async with aiosqlite.connect(tmp_path / "test.db") as conn:
            await configure_conn(conn)
            assert await check_admin_password(conn, PASSWORD)


# ---------------------------------------------------------------------------
# The log stream re-checks the login
# ---------------------------------------------------------------------------


def stream_request(
    app: FastAPI,
    *,
    session: dict[str, object] | None = None,
    authorization: str | None = None,
) -> Request:
    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/logs/stream",
        "headers": headers,
        "query_string": b"",
        "app": app,
        "session": session or {},
    }

    async def receive() -> dict[str, object]:
        await asyncio.Event().wait()  # the browser never disconnects
        raise AssertionError("unreachable")

    return Request(scope, receive)


async def collect(stream: AsyncIterator[str], limit: float = 3.0) -> list[str]:
    frames: list[str] = []

    async def run() -> None:
        async for frame in stream:
            frames.append(frame)

    await asyncio.wait_for(run(), timeout=limit)
    return frames


def entry(text: str) -> LogEntry:
    return LogEntry(ts="t", level="INFO", logger="x", message=text)


@pytest.fixture
async def stream_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = create_app(tmp_path / "test.db", tmp_path / "media")
    async with app.router.lifespan_context(app):
        yield app


async def admin_session(app: FastAPI) -> str:
    async with aiosqlite.connect(app.state.db_path) as conn:
        await configure_conn(conn)
        await set_initial_admin_password(conn, PASSWORD)
        sid = await create_session(conn)
        await conn.commit()
    return sid


class TestLogStreamRecheck:
    async def test_stream_ends_after_the_session_is_revoked(
        self, stream_app: FastAPI
    ) -> None:
        sid = await admin_session(stream_app)
        request = stream_request(stream_app, session={"sid": sid})
        broadcaster = LogBroadcaster()
        stream = stream_log_events(
            request, broadcaster, recheck_seconds=0.05, keepalive_seconds=0.05
        )
        first = await asyncio.wait_for(anext(stream), 2)
        assert first.startswith(": keepalive")

        # Logout (or password change, or expiry) removes the session row.
        async with aiosqlite.connect(stream_app.state.db_path) as conn:
            await configure_conn(conn)
            await delete_sessions(conn)
            await conn.commit()

        rest = await collect(stream)
        assert rest[-1].startswith("event: auth-expired")
        assert not broadcaster._subscribers  # unsubscribed

    async def test_no_entries_are_sent_after_revocation(
        self, stream_app: FastAPI
    ) -> None:
        sid = await admin_session(stream_app)
        request = stream_request(stream_app, session={"sid": sid})
        broadcaster = LogBroadcaster()
        stream = stream_log_events(
            request, broadcaster, recheck_seconds=0.05, keepalive_seconds=10
        )
        task = asyncio.create_task(collect(stream))
        await asyncio.sleep(0.02)
        broadcaster.add_entry(entry("still logged in"))
        await asyncio.sleep(0.2)
        async with aiosqlite.connect(stream_app.state.db_path) as conn:
            await configure_conn(conn)
            await delete_sessions(conn)
            await conn.commit()
        frames = await task
        assert any("still logged in" in f for f in frames)
        assert frames[-1].startswith("event: auth-expired")

    async def test_chatty_logs_cannot_starve_the_recheck(
        self, stream_app: FastAPI
    ) -> None:
        sid = await admin_session(stream_app)
        request = stream_request(stream_app, session={"sid": sid})
        broadcaster = LogBroadcaster()
        async with aiosqlite.connect(stream_app.state.db_path) as conn:
            await configure_conn(conn)
            await delete_sessions(conn)
            await conn.commit()

        async def chatter() -> None:
            for i in range(400):
                broadcaster.add_entry(entry(f"line {i}"))
                await asyncio.sleep(0.005)

        stream = stream_log_events(
            request, broadcaster, recheck_seconds=0.1, keepalive_seconds=10
        )
        noise = asyncio.create_task(chatter())
        frames = await collect(stream)
        noise.cancel()
        assert frames[-1].startswith("event: auth-expired")
        assert len(frames) < 100

    async def test_a_valid_session_keeps_streaming(self, stream_app: FastAPI) -> None:
        sid = await admin_session(stream_app)
        request = stream_request(stream_app, session={"sid": sid})
        broadcaster = LogBroadcaster()
        stream = stream_log_events(
            request, broadcaster, recheck_seconds=0.02, keepalive_seconds=10
        )
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.15)  # several re-checks pass
        assert not task.done()
        broadcaster.add_entry(entry("hello"))
        assert "hello" in await asyncio.wait_for(task, 1)
        await stream.aclose()

    async def test_session_missing_from_the_start_ends_immediately(
        self, stream_app: FastAPI
    ) -> None:
        await admin_session(stream_app)
        request = stream_request(stream_app, session={})
        stream = stream_log_events(
            request, LogBroadcaster(), recheck_seconds=0.02, keepalive_seconds=10
        )
        frames = await collect(stream)
        assert frames == ["event: auth-expired\ndata: {}\n\n"]

    async def test_bearer_token_streams_end_when_revoked(
        self, stream_app: FastAPI
    ) -> None:
        async with aiosqlite.connect(stream_app.state.db_path) as conn:
            await configure_conn(conn)
            await set_initial_admin_password(conn, PASSWORD)
            token = await create_admin_token(conn, "curl")
            await conn.commit()
        request = stream_request(stream_app, authorization=f"Bearer {token.token}")
        stream = stream_log_events(
            request, LogBroadcaster(), recheck_seconds=0.02, keepalive_seconds=0.02
        )
        assert (await asyncio.wait_for(anext(stream), 1)).startswith(": keepalive")
        async with aiosqlite.connect(stream_app.state.db_path) as conn:
            await configure_conn(conn)
            await conn.execute("DELETE FROM admin_tokens")
            await conn.commit()
        frames = await collect(stream)
        assert frames[-1].startswith("event: auth-expired")

    async def test_checking_a_token_does_not_count_as_using_it(
        self, conn: aiosqlite.Connection
    ) -> None:
        token = await create_admin_token(conn, "curl")
        assert await is_admin_token_valid(conn, token.token)
        assert not await is_admin_token_valid(conn, token.token + "x")
        assert not await is_admin_token_valid(conn, "nonsense")
        async with conn.execute("SELECT last_used_at FROM admin_tokens") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] is None

    async def test_disabled_auth_streams_on(self, tmp_path: Path) -> None:
        app = create_app(
            tmp_path / "test.db", tmp_path / "media", AuthConfig(disabled=True)
        )
        async with app.router.lifespan_context(app):
            request = stream_request(app)
            stream = stream_log_events(
                request, LogBroadcaster(), recheck_seconds=0.02, keepalive_seconds=0.05
            )
            for _ in range(3):
                assert (await asyncio.wait_for(anext(stream), 1)).startswith(
                    ": keepalive"
                )
            await stream.aclose()


# ---------------------------------------------------------------------------
# Change password, log out everywhere
# ---------------------------------------------------------------------------


@pytest.fixture
async def account_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = create_app(
        tmp_path / "test.db", tmp_path / "media", AuthConfig(admin_password=PASSWORD)
    )
    async with app.router.lifespan_context(app):
        yield app


def browser(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def log_in(c: AsyncClient, password: str = PASSWORD) -> str:
    """Log in through the web form; return the session's CSRF token."""
    import re

    page = await c.get("/login")
    token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert token
    r = await c.post(
        "/login", data={"password": password, "csrf_token": token.group(1)}
    )
    assert r.status_code == 303
    home = await c.get("/")
    match = re.search(r'name="csrf-token" content="([^"]+)"', home.text)
    assert match
    return match.group(1)


async def api_token(c: AsyncClient) -> dict[str, str]:
    r = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
    return {"Authorization": f"Bearer {r.json()['token']}"}


class TestChangePassword:
    async def change(
        self,
        c: AsyncClient,
        csrf: str,
        current: str = PASSWORD,
        new: str = "a-brand-new-password",
    ) -> int:
        r = await c.put(
            "/api/v1/auth/password",
            json={"current_password": current, "new_password": new},
            headers={"X-CSRF-Token": csrf},
        )
        return r.status_code

    async def test_changes_the_password(self, account_app: FastAPI) -> None:
        async with browser(account_app) as c:
            csrf = await log_in(c)
            assert await self.change(c, csrf) == 204
            other = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
            assert other.status_code == 401
            ok = await c.post(
                "/api/v1/auth/login", json={"password": "a-brand-new-password"}
            )
            assert ok.status_code == 201

    async def test_requires_the_current_password(self, account_app: FastAPI) -> None:
        async with browser(account_app) as c:
            csrf = await log_in(c)
            r = await c.put(
                "/api/v1/auth/password",
                json={"current_password": "wrong", "new_password": "a-new-password"},
                headers={"X-CSRF-Token": csrf},
            )
            assert r.status_code == 403
            assert r.json()["error_code"] == "WRONG_PASSWORD"
            ok = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
            assert ok.status_code == 201

    async def test_enforces_the_minimum_length(self, account_app: FastAPI) -> None:
        async with browser(account_app) as c:
            csrf = await log_in(c)
            assert await self.change(c, csrf, new="short") == 422
            ok = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
            assert ok.status_code == 201

    async def test_requires_login_and_csrf(self, account_app: FastAPI) -> None:
        async with browser(account_app) as anon:
            r = await anon.put(
                "/api/v1/auth/password",
                json={"current_password": PASSWORD, "new_password": "a-new-password"},
            )
            assert r.status_code == 401
        async with browser(account_app) as c:
            await log_in(c)
            r = await c.put(
                "/api/v1/auth/password",
                json={"current_password": PASSWORD, "new_password": "a-new-password"},
            )
            assert r.status_code == 403
            assert r.json()["error_code"] == "CSRF_FAILED"
            ok = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
            assert ok.status_code == 201

    async def test_other_sessions_end_but_this_one_stays(
        self, account_app: FastAPI
    ) -> None:
        async with browser(account_app) as here, browser(account_app) as there:
            csrf = await log_in(here)
            await log_in(there)
            assert (await there.get("/api/v1/auth/status")).status_code == 200
            assert await self.change(here, csrf) == 204
            assert (await here.get("/api/v1/auth/status")).status_code == 200
            assert (await there.get("/api/v1/auth/status")).status_code == 401

    async def test_api_tokens_survive(self, account_app: FastAPI) -> None:
        async with browser(account_app) as c:
            headers = await api_token(c)
            csrf = await log_in(c)
            assert await self.change(c, csrf) == 204
            r = await c.get("/api/v1/auth/status", headers=headers)
            assert r.status_code == 200

    async def test_tokens_are_revoked_only_when_asked(
        self, account_app: FastAPI
    ) -> None:
        async with browser(account_app) as c:
            headers = await api_token(c)
            csrf = await log_in(c)
            for flag in (False, None):
                body: dict[str, object] = {
                    "current_password": PASSWORD,
                    "new_password": PASSWORD,
                }
                if flag is not None:
                    body["revoke_tokens"] = flag
                r = await c.put(
                    "/api/v1/auth/password", json=body, headers={"X-CSRF-Token": csrf}
                )
                assert r.status_code == 204
                status = await c.get("/api/v1/auth/status", headers=headers)
                assert status.status_code == 200
            r = await c.put(
                "/api/v1/auth/password",
                json={
                    "current_password": PASSWORD,
                    "new_password": PASSWORD,
                    "revoke_tokens": True,
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert r.status_code == 204
            revoked = await c.get("/api/v1/auth/status", headers=headers)
            assert revoked.status_code == 401
            # The browser session that made the change is still good.
            assert (await c.get("/api/v1/auth/status")).status_code == 200

    async def test_revoking_tokens_needs_csrf_and_the_password(
        self, account_app: FastAPI
    ) -> None:
        async with browser(account_app) as c:
            headers = await api_token(c)
            csrf = await log_in(c)
            no_csrf = await c.put(
                "/api/v1/auth/password",
                json={
                    "current_password": PASSWORD,
                    "new_password": "a-new-password",
                    "revoke_tokens": True,
                },
            )
            assert no_csrf.status_code == 403
            wrong = await c.put(
                "/api/v1/auth/password",
                json={
                    "current_password": "wrong",
                    "new_password": "a-new-password",
                    "revoke_tokens": True,
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert wrong.status_code == 403
            ok = await c.get("/api/v1/auth/status", headers=headers)
            assert ok.status_code == 200

    async def test_a_token_can_change_the_password_and_ends_all_sessions(
        self, account_app: FastAPI
    ) -> None:
        async with browser(account_app) as c, browser(account_app) as tab:
            await log_in(tab)
            headers = await api_token(c)
            r = await c.put(
                "/api/v1/auth/password",
                json={"current_password": PASSWORD, "new_password": "token-changed-pw"},
                headers=headers,
            )
            assert r.status_code == 204
            assert (await tab.get("/api/v1/auth/status")).status_code == 401

    async def test_guessing_the_current_password_is_throttled(
        self, account_app: FastAPI
    ) -> None:
        """A stolen session must not be a free password oracle."""
        async with browser(account_app) as c:
            csrf = await log_in(c)
            for _ in range(10):
                assert await self.change(c, csrf, current="wrong") == 403
            assert await self.change(c, csrf, current="wrong") == 429
            # Even the right password waits.
            assert await self.change(c, csrf) == 429
            ok = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
            assert ok.status_code == 429

    async def test_refused_when_auth_is_disabled(self, tmp_path: Path) -> None:
        app = create_app(
            tmp_path / "test.db", tmp_path / "media", AuthConfig(disabled=True)
        )
        async with app.router.lifespan_context(app), browser(app) as c:
            r = await c.put(
                "/api/v1/auth/password",
                json={"current_password": "x", "new_password": "y" * 10},
            )
            assert r.status_code == 409
            assert r.json()["error_code"] == "AUTH_DISABLED"


class TestLogOutEverywhere:
    async def test_ends_every_session_including_this_one(
        self, account_app: FastAPI
    ) -> None:
        async with browser(account_app) as here, browser(account_app) as there:
            csrf = await log_in(here)
            await log_in(there)
            r = await here.post(
                "/api/v1/auth/sessions/revoke", headers={"X-CSRF-Token": csrf}
            )
            assert r.status_code == 200
            assert r.json() == {"revoked": 2}
            assert (await here.get("/api/v1/auth/status")).status_code == 401
            assert (await there.get("/api/v1/auth/status")).status_code == 401

    async def test_tokens_are_not_affected(self, account_app: FastAPI) -> None:
        async with browser(account_app) as c:
            headers = await api_token(c)
            csrf = await log_in(c)
            await c.post("/api/v1/auth/sessions/revoke", headers={"X-CSRF-Token": csrf})
            assert (
                await c.get("/api/v1/auth/status", headers=headers)
            ).status_code == 200

    async def test_needs_admin_and_csrf(self, account_app: FastAPI) -> None:
        async with browser(account_app) as anon:
            r = await anon.post("/api/v1/auth/sessions/revoke")
            assert r.status_code == 401
        async with browser(account_app) as c:
            await log_in(c)
            r = await c.post("/api/v1/auth/sessions/revoke")
            assert r.status_code == 403
            assert (await c.get("/api/v1/auth/status")).status_code == 200


class TestAccountPage:
    async def test_page_has_both_controls(self, account_app: FastAPI) -> None:
        async with browser(account_app) as c:
            await log_in(c)
            r = await c.get("/account")
            assert r.status_code == 200
            assert 'id="password-form"' in r.text
            assert "logoutEverywhere()" in r.text
            assert 'href="/account"' in r.text  # in the nav

    async def test_page_needs_login(self, account_app: FastAPI) -> None:
        async with browser(account_app) as c:
            r = await c.get("/account")
            assert r.status_code == 303
            assert r.headers["location"].startswith("/login")

    async def test_page_in_disabled_mode_explains_itself(self, tmp_path: Path) -> None:
        app = create_app(
            tmp_path / "test.db", tmp_path / "media", AuthConfig(disabled=True)
        )
        async with app.router.lifespan_context(app), browser(app) as c:
            r = await c.get("/account")
            assert r.status_code == 200
            assert "password-form" not in r.text
            assert "KIDSPLAY_AUTH=disabled" in r.text
            assert 'href="/account"' not in r.text


class TestSessionHelpers:
    async def test_delete_sessions_can_keep_one(
        self, conn: aiosqlite.Connection
    ) -> None:
        keep = await create_session(conn)
        gone = await create_session(conn)
        assert await delete_sessions(conn, keep_session_id=keep) == 1
        assert await is_session_valid(conn, keep)
        assert not await is_session_valid(conn, gone)
        assert await delete_sessions(conn) == 1
        assert not await is_session_valid(conn, keep)
