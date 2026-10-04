"""Tests for admin authentication on the JSON API.

Covers the ``/api/v1/auth`` routes, admin API tokens, 401s on every
management endpoint, the protected ``/docs``, and that device sync keeps
working with per-device keys only.
"""

import asyncio
import io
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from PIL import Image

from kidsplay_server.api.app import create_app

# Routes that do not require admin auth, as (method, path template).
PUBLIC_ROUTES = {
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/setup"),
    ("POST", "/setup"),
    ("POST", "/logout"),
    ("POST", "/api/v1/auth/login"),
    # Device-facing: per-device Bearer keys.
    ("GET", "/api/v1/devices/{device_id}/manifest"),
    ("GET", "/api/v1/sync/file/{content_hash}"),
    # On-device pairing: a device with no credentials is what is being set
    # up. Rate-limited per client; the API key goes only to the holder of the
    # device-generated binding secret (see api/pairing.py and test_pairing.py).
    # Approving, denying and listing requests stay admin-only.
    ("POST", "/api/v1/pairing"),
    ("POST", "/api/v1/pairing/poll"),
    # The device tells the server it has saved its key. Needs the binding
    # secret like poll does, and is rate-limited the same way.
    ("POST", "/api/v1/pairing/confirm"),
}


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def client(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=admin_headers,
    ) as c:
        yield c


def _concrete_path(template: str) -> str:
    """Fill path parameters with syntactically valid dummy values."""

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        return "a" * 64 if name == "content_hash" else str(uuid.uuid4())

    return re.sub(r"\{(\w+)(?::\w+)?\}", fill, template)


def _all_routes(app: FastAPI) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.extend((m, route.path) for m in sorted(route.methods))
    return routes


async def _create_device(client: AsyncClient) -> dict:
    r = await client.post("/api/v1/profiles", json={"name": "Leo"})
    assert r.status_code == 201
    r = await client.post(
        "/api/v1/devices", json={"name": "GameBoy", "profile_id": r.json()["id"]}
    )
    assert r.status_code == 201
    return r.json()


# ---------------------------------------------------------------------------
# Every management endpoint is protected
# ---------------------------------------------------------------------------


class TestUnauthenticated:
    async def test_every_non_public_route_requires_admin(
        self, anon_client: AsyncClient, app: FastAPI, admin_headers: dict[str, str]
    ) -> None:
        """Default-deny: any route not explicitly public rejects anonymous use."""
        checked = 0
        for method, template in _all_routes(app):
            if (method, template) in PUBLIC_ROUTES:
                continue
            path = _concrete_path(template)
            r = await anon_client.request(method, path)
            if template.startswith("/api/") or template == "/openapi.json":
                assert r.status_code == 401, (method, path, r.status_code)
                assert r.json()["error_code"] == "UNAUTHORIZED"
            else:
                assert r.status_code == 303, (method, path, r.status_code)
                assert r.headers["location"].startswith("/login?next=")
            checked += 1
        assert checked > 30

    async def test_public_routes_list_is_current(self, app: FastAPI) -> None:
        """Every allowlisted route still exists (no stale exemptions)."""
        assert set(_all_routes(app)) >= PUBLIC_ROUTES

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/profiles", "/api/v1/devices", "/api/v1/media", "/api/v1/queue"],
    )
    async def test_management_api_401(
        self, anon_client: AsyncClient, path: str
    ) -> None:
        r = await anon_client.get(path)
        assert r.status_code == 401
        assert r.json() == {
            "detail": "Admin authentication required",
            "error_code": "UNAUTHORIZED",
        }

    async def test_mutation_rejected_without_side_effect(
        self, anon_client: AsyncClient, client: AsyncClient
    ) -> None:
        r = await anon_client.post("/api/v1/profiles", json={"name": "Mallory"})
        assert r.status_code == 401
        assert (await client.get("/api/v1/profiles")).json() == []

    async def test_docs_redirect_to_login(self, anon_client: AsyncClient) -> None:
        for path in ("/docs", "/redoc"):
            r = await anon_client.get(path)
            assert r.status_code == 303
            assert r.headers["location"] == f"/login?next=%2F{path[1:]}"

    async def test_static_files_public(self, anon_client: AsyncClient) -> None:
        r = await anon_client.get("/static/htmx.min.js")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Admin API tokens
# ---------------------------------------------------------------------------


class TestAdminTokens:
    async def test_token_grants_access(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/profiles")
        assert r.status_code == 200

    async def test_docs_and_openapi_with_token(self, client: AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/redoc")).status_code == 200
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        assert "/api/v1/profiles" in r.json()["paths"]
        schemes = r.json()["components"]["securitySchemes"]
        assert schemes["HTTPBearer"]["scheme"] == "bearer"

    async def test_status(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/auth/status")
        assert r.status_code == 200
        assert r.json() == {"auth_enabled": True, "method": "token"}

    async def test_create_list_revoke(
        self, client: AsyncClient, anon_client: AsyncClient
    ) -> None:
        r = await client.post("/api/v1/auth/tokens", json={"name": "script"})
        assert r.status_code == 201
        created = r.json()
        assert created["name"] == "script"
        assert created["token"].startswith("kpa_")

        listed = (await client.get("/api/v1/auth/tokens")).json()
        assert [t["name"] for t in listed] == ["tests", "script"]
        assert all("token" not in t for t in listed)

        new_auth = {"Authorization": f"Bearer {created['token']}"}
        r = await anon_client.get("/api/v1/profiles", headers=new_auth)
        assert r.status_code == 200

        r = await client.delete(f"/api/v1/auth/tokens/{created['id']}")
        assert r.status_code == 204
        r = await anon_client.get("/api/v1/profiles", headers=new_auth)
        assert r.status_code == 401

    async def test_revoke_unknown_token_404(self, client: AsyncClient) -> None:
        r = await client.delete(f"/api/v1/auth/tokens/{uuid.uuid4()}")
        assert r.status_code == 404

    async def test_create_token_requires_name(self, client: AsyncClient) -> None:
        r = await client.post("/api/v1/auth/tokens", json={"name": ""})
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer kpa_bogus",
            "Bearer ",
            "Basic YWRtaW46cGFzc3dvcmQ=",
            "kpa_no_scheme",
        ],
    )
    async def test_bad_authorization_header_401(
        self, anon_client: AsyncClient, admin_headers: dict[str, str], header: str
    ) -> None:
        r = await anon_client.get("/api/v1/profiles", headers={"Authorization": header})
        assert r.status_code == 401

    async def test_device_key_is_not_admin(
        self, client: AsyncClient, anon_client: AsyncClient
    ) -> None:
        device = await _create_device(client)
        r = await anon_client.get(
            "/api/v1/profiles",
            headers={"Authorization": f"Bearer {device['api_key']}"},
        )
        assert r.status_code == 401

    async def test_token_requests_skip_csrf(self, client: AsyncClient) -> None:
        """Bearer tokens are not ambient credentials, so no CSRF header needed."""
        r = await client.post("/api/v1/profiles", json={"name": "Leo"})
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# POST /auth/login (CLI password → token exchange)
# ---------------------------------------------------------------------------


class TestLoginForToken:
    async def test_setup_required(self, anon_client: AsyncClient) -> None:
        r = await anon_client.post(
            "/api/v1/auth/login", json={"password": "whatever-password"}
        )
        assert r.status_code == 409
        assert r.json()["error_code"] == "SETUP_REQUIRED"

    async def test_success_returns_working_token(
        self,
        anon_client: AsyncClient,
        admin_headers: dict[str, str],
        admin_password: str,
    ) -> None:
        r = await anon_client.post(
            "/api/v1/auth/login",
            json={"password": admin_password, "token_name": "my laptop"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "my laptop"
        r = await anon_client.get(
            "/api/v1/auth/status",
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        assert r.status_code == 200

    async def test_wrong_password(
        self, anon_client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        r = await anon_client.post(
            "/api/v1/auth/login", json={"password": "wrong-password"}
        )
        assert r.status_code == 401
        assert r.json()["error_code"] == "UNAUTHORIZED"

    async def test_throttled_after_repeated_failures(
        self,
        anon_client: AsyncClient,
        admin_headers: dict[str, str],
        admin_password: str,
    ) -> None:
        for _ in range(10):
            r = await anon_client.post(
                "/api/v1/auth/login", json={"password": "wrong-password"}
            )
            assert r.status_code == 401
        r = await anon_client.post(
            "/api/v1/auth/login", json={"password": admin_password}
        )
        assert r.status_code == 429
        assert r.json()["error_code"] == "RATE_LIMITED"

    async def test_throttle_holds_under_concurrent_attempts(
        self, anon_client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """Parallel guesses can't all slip past the limit while hashes run."""
        responses = await asyncio.gather(
            *(
                anon_client.post("/api/v1/auth/login", json={"password": f"guess-{i}"})
                for i in range(30)
            )
        )
        codes = [r.status_code for r in responses]
        assert codes.count(401) == 10
        assert codes.count(429) == 20


# ---------------------------------------------------------------------------
# Device sync is unaffected
# ---------------------------------------------------------------------------


def _png(path: Path) -> Path:
    buf = io.BytesIO()
    Image.new("RGB", (120, 90), color=(10, 200, 30)).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


class TestSyncUnaffected:
    async def test_device_key_syncs_without_admin_credentials(
        self, client: AsyncClient, anon_client: AsyncClient, tmp_path: Path
    ) -> None:
        device = await _create_device(client)
        r = await client.post(
            "/api/v1/media/ingest",
            json={
                "source_path": str(_png(tmp_path / "p.png")),
                "media_type": "photo",
                "playlist_title": "Pics",
                "profile_ids": [device["profile_id"]],
            },
        )
        assert r.status_code == 200

        device_auth = {"Authorization": f"Bearer {device['api_key']}"}
        r = await anon_client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=device_auth
        )
        assert r.status_code == 200
        files = r.json()["files"]
        assert files
        r = await anon_client.get(
            f"/api/v1/sync/file/{files[0]['content_hash']}", headers=device_auth
        )
        assert r.status_code == 200
        # No session cookie is needed or set for device sync.
        assert "set-cookie" not in r.headers

    async def test_admin_token_is_not_a_device_key(self, client: AsyncClient) -> None:
        device = await _create_device(client)
        r = await client.get(f"/api/v1/devices/{device['id']}/manifest")
        assert r.status_code == 401


# The public routes a handheld uses; behind a single sign-on proxy these are
# the ones the proxy must leave open (docs/DEVELOPMENT.md).
_ACCOUNT_ROUTES = {
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/setup"),
    ("POST", "/setup"),
    ("POST", "/logout"),
    ("POST", "/api/v1/auth/login"),
}
DEVICE_ROUTES = PUBLIC_ROUTES - _ACCOUNT_ROUTES
_DEVELOPMENT_MD = Path(__file__).resolve().parents[3] / "docs" / "DEVELOPMENT.md"


def _sso_section() -> str:
    text = _DEVELOPMENT_MD.read_text()
    start = text.index("#### With single sign-on")
    return text[start : text.index("\n### ", start)]


class TestSingleSignOnDocs:
    """The SSO proxy recipe must open exactly the device routes, no more."""

    def test_table_lists_every_device_route(self) -> None:
        section = _sso_section()
        for method, template in DEVICE_ROUTES:
            assert f"| `{method}` | `{template}` |" in section, (method, template)

    def test_caddy_example_opens_device_routes_and_nothing_admin(
        self, app: FastAPI
    ) -> None:
        section = _sso_section()
        found_regex = re.search(r"path_regexp (\S+)", section)
        found_posts = re.search(r"\n\s+path (/api/[^\n]+)", section)
        assert found_regex and found_posts, "Caddy example not found"
        regex = re.compile(found_regex.group(1))
        posts = set(found_posts.group(1).split())

        def opened(method: str, path: str) -> bool:
            if method == "GET":
                return bool(regex.match(path))
            return method == "POST" and path in posts

        for method, template in _all_routes(app):
            path = _concrete_path(template)
            expected = (method, template) in DEVICE_ROUTES
            assert opened(method, path) is expected, (method, template)
