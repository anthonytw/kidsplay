"""Tests for trusted-proxy handling: client identity, scheme, Secure cookie.

Covers ``kidsplay_server.proxy`` on its own, then through the real app: the
admin login throttle and the pairing throttles must give each forwarded client
its own budget behind a trusted proxy and ignore forwarded headers from anyone
else.
"""

import ipaddress
import re
import secrets
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.types import Message, Receive, Scope, Send

from kidsplay_server.api.app import create_app
from kidsplay_server.auth import AuthConfig
from kidsplay_server.proxy import (
    ProxyHeadersMiddleware,
    client_from_forwarded_for,
    parse_ip,
    parse_trusted_proxies,
    throttle_key,
)

PASSWORD = "a-good-long-password"
PROXY = "172.18.0.1"
TRUSTED = parse_trusted_proxies("172.18.0.0/16")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseTrustedProxies:
    def test_empty(self) -> None:
        assert parse_trusted_proxies("") == ()
        assert parse_trusted_proxies("  ,  ") == ()

    def test_addresses_and_networks(self) -> None:
        nets = parse_trusted_proxies("10.0.0.1, 172.16.0.0/12 ::1 fd00::/8")
        assert [str(n) for n in nets] == [
            "10.0.0.1/32",
            "172.16.0.0/12",
            "::1/128",
            "fd00::/8",
        ]

    def test_accepts_a_list(self) -> None:
        assert [str(n) for n in parse_trusted_proxies(["10.0.0.1", " "])] == [
            "10.0.0.1/32"
        ]

    @pytest.mark.parametrize(
        "value", ["*", "0.0.0.0/0", "::/0", "proxy.example", "10.0.0.1/33", "1.2.3"]
    )
    def test_refuses_wildcards_and_garbage(self, value: str) -> None:
        with pytest.raises(ValueError, match="KIDSPLAY_TRUSTED_PROXIES"):
            parse_trusted_proxies(value)


class TestParseIp:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1.2.3.4", "1.2.3.4"),
            (" 1.2.3.4 ", "1.2.3.4"),
            ("1.2.3.4:5678", "1.2.3.4"),
            ("2001:db8::1", "2001:db8::1"),
            ("[2001:db8::1]", "2001:db8::1"),
            ("[2001:db8::1]:443", "2001:db8::1"),
            ("::ffff:1.2.3.4", "1.2.3.4"),
            ("fe80::1%eth0", "fe80::1"),
            ("[fe80::1%25eth0]", "fe80::1"),
        ],
    )
    def test_valid(self, text: str, expected: str) -> None:
        assert parse_ip(text) == ipaddress.ip_address(expected)

    @pytest.mark.parametrize(
        "text",
        ["", "unknown", "_hidden", "1.2.3", "1.2.3.4.5", "evil.example", "1.2.3.4:x"]
        + ["a" * 100, "1.2.3.4, 5.6.7.8", "1.2.3.4\r\nX-Evil: 1"],
    )
    def test_invalid(self, text: str) -> None:
        assert parse_ip(text) is None


class TestThrottleKey:
    def test_ipv4_is_itself(self) -> None:
        assert throttle_key("192.168.1.5") == "192.168.1.5"

    def test_ipv6_shares_a_budget_per_64(self) -> None:
        a = throttle_key("2001:db8:1:2:aaaa::1")
        b = throttle_key("2001:db8:1:2:bbbb:cccc:dddd:eeee")
        assert a == b == "2001:db8:1:2::/64"
        assert throttle_key("2001:db8:1:3::1") != a

    def test_link_local_zone_is_not_part_of_the_key(self) -> None:
        """Uvicorn reports a link-local peer as ``fe80::1%eth0``; the zone names
        our interface, so it must not change the client's throttle key."""
        assert throttle_key("fe80::1%eth0") == throttle_key("fe80::2%wlan0")
        assert throttle_key("fe80::1%eth0") == "fe80::/64"

    def test_mapped_ipv4_is_ipv4(self) -> None:
        assert throttle_key("::ffff:10.1.2.3") == "10.1.2.3"

    def test_unknown(self) -> None:
        assert throttle_key(None) == "unknown"
        assert throttle_key("") == "unknown"
        assert throttle_key("testclient") == "testclient"


class TestClientFromForwardedFor:
    def pick(self, chain: str) -> str | None:
        found = client_from_forwarded_for(chain, TRUSTED)
        return None if found is None else str(found)

    def test_single_hop(self) -> None:
        assert self.pick("203.0.113.9") == "203.0.113.9"

    def test_rightmost_untrusted_hop_wins_over_spoofed_prefix(self) -> None:
        """The client can write the left of the chain; the proxy wrote the right."""
        assert self.pick("6.6.6.6, 203.0.113.9") == "203.0.113.9"
        assert self.pick("6.6.6.6, 203.0.113.9, 172.18.0.7") == "203.0.113.9"

    def test_skips_trusted_hops(self) -> None:
        assert self.pick("203.0.113.9, 172.18.0.5, 172.18.0.6") == "203.0.113.9"

    def test_all_trusted_uses_leftmost(self) -> None:
        assert self.pick("172.18.0.9, 172.18.0.5") == "172.18.0.9"

    @pytest.mark.parametrize(
        "chain",
        ["", " , ", "unknown", "203.0.113.9, garbage", "garbage, 172.18.0.5"],
    )
    def test_garbage_is_never_used(self, chain: str) -> None:
        assert self.pick(chain) is None

    def test_a_spoofed_trusted_looking_left_hop_does_not_matter(self) -> None:
        assert self.pick("172.18.0.99, 203.0.113.9") == "203.0.113.9"


# ---------------------------------------------------------------------------
# The middleware
# ---------------------------------------------------------------------------


async def _echo(scope: Scope, receive: Receive, send: Send) -> None:
    body = f"{scope['client'][0]}|{scope['scheme']}".encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def _echo_client(peer: str, trusted: str = "172.18.0.0/16") -> AsyncClient:
    app = ProxyHeadersMiddleware(_echo, parse_trusted_proxies(trusted))
    return AsyncClient(
        transport=ASGITransport(app=app, client=(peer, 4321)), base_url="http://test"
    )


class TestProxyHeadersMiddleware:
    async def test_trusted_peer_forwards_client(self) -> None:
        async with _echo_client(PROXY) as c:
            r = await c.get("/", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.text == "203.0.113.9|http"

    async def test_untrusted_peer_is_not_believed(self) -> None:
        async with _echo_client("198.51.100.7") as c:
            r = await c.get(
                "/",
                headers={
                    "X-Forwarded-For": "203.0.113.9",
                    "X-Forwarded-Proto": "https",
                },
            )
        assert r.text == "198.51.100.7|http"

    async def test_no_trusted_proxies_does_nothing(self) -> None:
        async with _echo_client(PROXY, trusted="") as c:
            r = await c.get("/", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.text == f"{PROXY}|http"

    async def test_spoofed_left_hop_ignored(self) -> None:
        async with _echo_client(PROXY) as c:
            r = await c.get("/", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
        assert r.text == "203.0.113.9|http"

    async def test_second_header_line_cannot_hide_the_real_hop(self) -> None:
        """Two ``X-Forwarded-For`` lines are one chain, in order."""
        async with _echo_client(PROXY) as c:
            r = await c.get(
                "/",
                headers=[
                    ("X-Forwarded-For", "203.0.113.9"),
                    ("X-Forwarded-For", "6.6.6.6"),
                ],
            )
        # 6.6.6.6 is the last hop the proxy saw, so it is the client.
        assert r.text == "6.6.6.6|http"
        async with _echo_client(PROXY) as c:
            r = await c.get(
                "/",
                headers=[
                    ("X-Forwarded-For", "6.6.6.6"),
                    ("X-Forwarded-For", "203.0.113.9"),
                ],
            )
        assert r.text == "203.0.113.9|http"

    async def test_garbage_falls_back_to_the_proxy(self) -> None:
        async with _echo_client(PROXY) as c:
            r = await c.get("/", headers={"X-Forwarded-For": "not-an-ip"})
        assert r.text == f"{PROXY}|http"

    async def test_ipv6_client_with_port_and_brackets(self) -> None:
        async with _echo_client(PROXY) as c:
            r = await c.get("/", headers={"X-Forwarded-For": "[2001:db8::7]:5555"})
        assert r.text == "2001:db8::7|http"

    async def test_proto_https_from_trusted_proxy(self) -> None:
        async with _echo_client(PROXY) as c:
            r = await c.get("/", headers={"X-Forwarded-Proto": "https"})
        assert r.text == f"{PROXY}|https"

    async def test_proto_uses_nearest_proxy_and_rejects_junk(self) -> None:
        async with _echo_client(PROXY) as c:
            r = await c.get("/", headers={"X-Forwarded-Proto": "https, http"})
            assert r.text.endswith("|http")
            r = await c.get("/", headers={"X-Forwarded-Proto": "javascript"})
            assert r.text.endswith("|http")

    async def test_ipv4_mapped_peer_is_matched(self) -> None:
        async with _echo_client("::ffff:172.18.0.1") as c:
            r = await c.get("/", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.text == "203.0.113.9|http"

    async def test_ipv6_peer_does_not_match_ipv4_network(self) -> None:
        async with _echo_client("2001:db8::1") as c:
            r = await c.get("/", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.text == "2001:db8::1|http"

    async def test_lifespan_and_missing_client_pass_through(self) -> None:
        seen: list[Scope] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(scope)

        mw = ProxyHeadersMiddleware(app, TRUSTED)

        async def receive() -> Message:
            return {"type": "lifespan.startup"}

        async def send(message: Message) -> None:
            return None

        await mw({"type": "lifespan"}, receive, send)
        scope: Scope = {
            "type": "http",
            "client": None,
            "scheme": "http",
            "headers": [(b"x-forwarded-for", b"1.2.3.4")],
        }
        await mw(scope, receive, send)
        assert seen[1]["client"] is None

    async def test_websocket_scheme(self) -> None:
        seen: list[Scope] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(scope)

        async def receive() -> Message:
            return {"type": "websocket.connect"}

        async def send(message: Message) -> None:
            return None

        scope: Scope = {
            "type": "websocket",
            "client": (PROXY, 1),
            "scheme": "ws",
            "headers": [(b"x-forwarded-proto", b"https")],
        }
        await ProxyHeadersMiddleware(app, TRUSTED)(scope, receive, send)
        assert seen[0]["scheme"] == "wss"


# ---------------------------------------------------------------------------
# Through the real app
# ---------------------------------------------------------------------------


def _app(tmp_path: Path, cookie_secure: bool | None = None) -> FastAPI:
    config = AuthConfig(
        admin_password=PASSWORD,
        cookie_secure=cookie_secure,
        trusted_proxies=parse_trusted_proxies("172.18.0.0/16"),
    )
    return create_app(tmp_path / "test.db", tmp_path / "media", config)


def _client(app: FastAPI, peer: str, scheme: str = "http") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=(peer, 5555)),
        base_url=f"{scheme}://test",
    )


async def _api_login(c: AsyncClient, password: str, xff: str | None) -> Response:
    headers = {"X-Forwarded-For": xff} if xff else {}
    return await c.post(
        "/api/v1/auth/login", json={"password": password}, headers=headers
    )


class TestLoginThrottleBehindProxy:
    async def test_forwarded_clients_have_separate_budgets(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            for _ in range(10):
                assert (await _api_login(c, "wrong", "203.0.113.1")).status_code == 401
            assert (await _api_login(c, "wrong", "203.0.113.1")).status_code == 429
            # Another person behind the same proxy is not locked out...
            assert (await _api_login(c, "wrong", "203.0.113.2")).status_code == 401
            # ...and can still log in.
            assert (await _api_login(c, PASSWORD, "203.0.113.2")).status_code == 201
            # The locked-out client stays locked out, even with the password.
            assert (await _api_login(c, PASSWORD, "203.0.113.1")).status_code == 429

    async def test_without_trusted_proxies_the_proxy_address_is_shared(
        self, tmp_path: Path
    ) -> None:
        """The pre-fix behaviour, kept when no proxy is configured."""
        app = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(admin_password=PASSWORD),
        )
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            for i in range(10):
                await _api_login(c, "wrong", f"203.0.113.{i}")
            assert (await _api_login(c, "wrong", "203.0.113.99")).status_code == 429

    async def test_spoofed_forwarded_for_from_untrusted_peer_is_ignored(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, "198.51.100.7") as c:
            for i in range(10):
                r = await _api_login(c, "wrong", f"203.0.113.{i}")
                assert r.status_code == 401
            # Rotating the header did not buy a fresh budget.
            assert (await _api_login(c, "wrong", "203.0.113.200")).status_code == 429

    async def test_spoofed_left_hop_does_not_dodge_the_throttle(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            for i in range(10):
                # The proxy appends the real client (203.0.113.1) on the right.
                r = await _api_login(c, "wrong", f"10.9.9.{i}, 203.0.113.1")
                assert r.status_code == 401
            r = await _api_login(c, "wrong", "10.9.9.99, 203.0.113.1")
            assert r.status_code == 429

    async def test_ipv6_rotation_within_a_64_shares_a_budget(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            for i in range(10):
                addr = f"2001:db8:1:2:{secrets.randbits(16):x}::{i + 1:x}"
                assert (await _api_login(c, "wrong", addr)).status_code == 401
            r = await _api_login(c, "wrong", "2001:db8:1:2:ffff::ff")
            assert r.status_code == 429

    async def test_web_login_uses_the_forwarded_client_too(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)

        async def attempt(c: AsyncClient, xff: str, password: str) -> Response:
            page = await c.get("/login", headers={"X-Forwarded-For": xff})
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
            assert token
            return await c.post(
                "/login",
                data={"password": password, "csrf_token": token.group(1)},
                headers={"X-Forwarded-For": xff},
            )

        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            for _ in range(10):
                assert (await attempt(c, "203.0.113.1", "wrong")).status_code == 401
            assert (await attempt(c, "203.0.113.1", "wrong")).status_code == 429
            assert (await attempt(c, "203.0.113.2", PASSWORD)).status_code == 303


class TestUnusualPeers:
    async def test_link_local_ipv6_client_can_log_in(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        async with (
            app.router.lifespan_context(app),
            _client(app, "fe80::1%eth0") as c,
        ):
            assert (await _api_login(c, "wrong", None)).status_code == 401
            assert (await _api_login(c, PASSWORD, None)).status_code == 201

    async def test_missing_client_address_shares_one_budget(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)
        transport = ASGITransport(app=app, client=("", 0))
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=transport, base_url="http://test") as c,
        ):
            for _ in range(10):
                assert (await _api_login(c, "wrong", None)).status_code == 401
            assert (await _api_login(c, "wrong", None)).status_code == 429


class TestPairingThrottleBehindProxy:
    def body(self) -> dict[str, object]:
        code = "".join(secrets.choice("23456789ABCDEFGH") for _ in range(8))
        return {"code": code, "binding_secret": secrets.token_urlsafe(32)}

    async def test_create_budget_is_per_forwarded_client(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            noisy = {"X-Forwarded-For": "203.0.113.1"}
            for _ in range(10):
                r = await c.post("/api/v1/pairing", json=self.body(), headers=noisy)
                assert r.status_code == 201
            r = await c.post("/api/v1/pairing", json=self.body(), headers=noisy)
            assert r.status_code == 429
            # A device elsewhere behind the same proxy can still pair.
            quiet = {"X-Forwarded-For": "203.0.113.2"}
            r = await c.post("/api/v1/pairing", json=self.body(), headers=quiet)
            assert r.status_code == 201

    async def test_poll_failures_are_per_forwarded_client(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            junk = {"code": "ABCD2345", "binding_secret": "x" * 40}
            noisy = {"X-Forwarded-For": "203.0.113.1"}
            for _ in range(10):
                r = await c.post("/api/v1/pairing/poll", json=junk, headers=noisy)
                assert r.status_code == 404
            r = await c.post("/api/v1/pairing/poll", json=junk, headers=noisy)
            assert r.status_code == 429
            quiet = {"X-Forwarded-For": "203.0.113.2"}
            r = await c.post("/api/v1/pairing/poll", json=junk, headers=quiet)
            assert r.status_code == 404

    async def test_spoofing_does_not_dodge_the_pairing_throttle(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, "198.51.100.7") as c:
            junk = {"code": "ABCD2345", "binding_secret": "x" * 40}
            for i in range(10):
                headers = {"X-Forwarded-For": f"203.0.113.{i}"}
                r = await c.post("/api/v1/pairing/poll", json=junk, headers=headers)
                assert r.status_code == 404
            headers = {"X-Forwarded-For": "203.0.113.250"}
            r = await c.post("/api/v1/pairing/poll", json=junk, headers=headers)
            assert r.status_code == 429

    async def test_admin_sees_the_real_client_address(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            headers = {"X-Forwarded-For": "203.0.113.77"}
            r = await c.post("/api/v1/pairing", json=self.body(), headers=headers)
            assert r.status_code == 201
            login = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
            token = login.json()["token"]
            listing = await c.get(
                "/api/v1/pairing/requests", headers={"Authorization": f"Bearer {token}"}
            )
            assert listing.json()[0]["client"] == "203.0.113.77"


class TestSecureCookie:
    async def _cookie(self, c: AsyncClient, headers: dict[str, str]) -> str:
        page = await c.get("/login", headers=headers)
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
        assert token
        r = await c.post(
            "/login",
            data={"password": PASSWORD, "csrf_token": token.group(1)},
            headers=headers,
        )
        assert r.status_code == 303
        cookies = [
            v
            for k, v in r.headers.multi_items()
            if k == "set-cookie" and v.startswith("kidsplay_session=")
        ]
        assert cookies
        return cookies[-1].lower()

    async def test_https_through_trusted_proxy_is_secure_without_config(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)
        # The client's own URL is https (like a browser); the app sees plain
        # http from the proxy plus X-Forwarded-Proto.
        async with (
            app.router.lifespan_context(app),
            _client(app, PROXY, scheme="https") as c,
        ):
            cookie = await self._cookie(c, {"X-Forwarded-Proto": "https"})
        assert "; secure" in cookie
        assert "httponly" in cookie

    async def test_plain_http_is_not_secure(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            cookie = await self._cookie(c, {})
        assert "secure" not in cookie

    async def test_forwarded_proto_from_untrusted_peer_is_ignored(
        self, tmp_path: Path
    ) -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app), _client(app, "198.51.100.7") as c:
            cookie = await self._cookie(c, {"X-Forwarded-Proto": "https"})
        assert "secure" not in cookie

    async def test_direct_https_is_secure(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        async with (
            app.router.lifespan_context(app),
            _client(app, "192.168.1.5", scheme="https") as c,
        ):
            cookie = await self._cookie(c, {})
        assert "; secure" in cookie

    async def test_env_override_forces_secure(self, tmp_path: Path) -> None:
        app = _app(tmp_path, cookie_secure=True)
        async with (
            app.router.lifespan_context(app),
            _client(app, "192.168.1.5", scheme="https") as c,
        ):
            assert "; secure" in await self._cookie(c, {})

    async def test_env_override_can_forbid_secure(self, tmp_path: Path) -> None:
        app = _app(tmp_path, cookie_secure=False)
        async with app.router.lifespan_context(app), _client(app, PROXY) as c:
            cookie = await self._cookie(c, {"X-Forwarded-Proto": "https"})
        assert "secure" not in cookie

    async def test_secure_is_not_added_twice(self, tmp_path: Path) -> None:
        app = _app(tmp_path, cookie_secure=True)
        async with (
            app.router.lifespan_context(app),
            _client(app, PROXY, scheme="https") as c,
        ):
            cookie = await self._cookie(c, {})
        assert cookie.count("secure") == 1
