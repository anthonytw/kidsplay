"""Tests for browser admin auth: first-run setup, login/logout, CSRF,
disabled mode and the ``KIDSPLAY_ADMIN_PASSWORD`` pre-seed.
"""

import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from kidsplay_server.api.app import create_app
from kidsplay_server.auth import AuthConfig

PASSWORD = "a-good-long-password"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A cookie-keeping client with no credentials, like a fresh browser."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def csrf_from(response: Response) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    assert match, "page has no CSRF meta tag"
    return match.group(1)


@pytest.fixture
def setup_code(app: FastAPI) -> str:
    """The one-time code the server would print in its log."""
    code = app.state.setup_code.code
    assert code
    return code


async def do_setup(
    browser: AsyncClient, code: str, password: str = PASSWORD
) -> Response:
    page = await browser.get("/setup")
    assert page.status_code == 200
    return await browser.post(
        "/setup",
        data={
            "password": password,
            "password_confirm": password,
            "csrf_token": csrf_from(page),
            "setup_code": code,
        },
    )


async def do_login(
    browser: AsyncClient, password: str = PASSWORD, next_url: str = "/"
) -> Response:
    page = await browser.get("/login", params={"next": next_url})
    assert page.status_code == 200
    return await browser.post(
        "/login",
        data={"password": password, "csrf_token": csrf_from(page), "next": next_url},
    )


async def logged_in_csrf(browser: AsyncClient) -> str:
    page = await browser.get("/")
    assert page.status_code == 200
    return csrf_from(page)


# ---------------------------------------------------------------------------
# First-run setup
# ---------------------------------------------------------------------------


class TestFirstRunSetup:
    async def test_pages_redirect_to_setup_when_no_password(
        self, browser: AsyncClient
    ) -> None:
        r = await browser.get("/media")
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2Fmedia"
        r = await browser.get("/login")
        assert r.status_code == 303
        assert r.headers["location"] == "/setup"

    async def test_setup_sets_password_and_logs_in(
        self, browser: AsyncClient, setup_code: str
    ) -> None:
        r = await do_setup(browser, setup_code)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert (await browser.get("/")).status_code == 200
        r = await browser.get("/api/v1/auth/status")
        assert r.json() == {"auth_enabled": True, "method": "session"}

    async def test_setup_closed_once_password_set(
        self, browser: AsyncClient, setup_code: str
    ) -> None:
        await do_setup(browser, setup_code)
        r = await browser.get("/setup")
        assert r.status_code == 303
        assert r.headers["location"] == "/login"

        # A second setup POST cannot replace the password.
        csrf = await logged_in_csrf(browser)
        r = await browser.post(
            "/setup",
            data={
                "password": "attacker-password",
                "password_confirm": "attacker-password",
                "csrf_token": csrf,
                "setup_code": setup_code,
            },
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/login"
        r = await browser.post(
            "/api/v1/auth/login", json={"password": "attacker-password"}
        )
        assert r.status_code == 401

    async def test_setup_rejects_short_password(
        self, browser: AsyncClient, setup_code: str
    ) -> None:
        r = await do_setup(browser, setup_code, password="short")
        assert r.status_code == 400
        assert "at least 8 characters" in r.text
        assert (await browser.get("/setup")).status_code == 200

    async def test_setup_rejects_mismatch(
        self, browser: AsyncClient, setup_code: str
    ) -> None:
        page = await browser.get("/setup")
        r = await browser.post(
            "/setup",
            data={
                "password": PASSWORD,
                "password_confirm": PASSWORD + "x",
                "csrf_token": csrf_from(page),
                "setup_code": setup_code,
            },
        )
        assert r.status_code == 400
        assert "do not match" in r.text

    async def test_setup_requires_csrf(self, browser: AsyncClient) -> None:
        await browser.get("/setup")
        r = await browser.post(
            "/setup",
            data={"password": PASSWORD, "password_confirm": PASSWORD},
        )
        assert r.status_code == 403
        assert r.json()["error_code"] == "CSRF_FAILED"
        assert (await browser.get("/setup")).status_code == 200


# ---------------------------------------------------------------------------
# Login and logout
# ---------------------------------------------------------------------------


@pytest.fixture
async def configured(browser: AsyncClient, setup_code: str) -> None:
    """Complete setup, then drop the resulting session."""
    await do_setup(browser, setup_code)
    browser.cookies.clear()


@pytest.mark.usefixtures("configured")
class TestLoginLogout:
    async def test_login_page_renders_without_nav(self, browser: AsyncClient) -> None:
        r = await browser.get("/login")
        assert r.status_code == 200
        assert 'name="password"' in r.text
        assert 'href="/devices"' not in r.text

    async def test_login_redirects_to_next(self, browser: AsyncClient) -> None:
        r = await do_login(browser, next_url="/devices?x=1")
        assert r.status_code == 303
        assert r.headers["location"] == "/devices?x=1"
        assert (await browser.get("/devices")).status_code == 200

    @pytest.mark.parametrize(
        "evil",
        ["//evil.example/", "https://evil.example/", "/\\evil.example", "evil"],
    )
    async def test_next_cannot_leave_site(
        self, browser: AsyncClient, evil: str
    ) -> None:
        r = await do_login(browser, next_url=evil)
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    async def test_wrong_password(self, browser: AsyncClient) -> None:
        r = await do_login(browser, password="wrong-password")
        assert r.status_code == 401
        assert "Incorrect password" in r.text
        assert (await browser.get("/")).status_code == 303

    async def test_login_throttled(self, browser: AsyncClient) -> None:
        for _ in range(10):
            assert (await do_login(browser, password="nope-nope")).status_code == 401
        r = await do_login(browser)
        assert r.status_code == 429

    async def test_login_requires_csrf(self, browser: AsyncClient) -> None:
        await browser.get("/login")
        r = await browser.post("/login", data={"password": PASSWORD})
        assert r.status_code == 403
        r = await browser.post(
            "/login", data={"password": PASSWORD, "csrf_token": "forged"}
        )
        assert r.status_code == 403
        assert (await browser.get("/")).status_code == 303

    async def test_session_cookie_attributes(self, browser: AsyncClient) -> None:
        r = await do_login(browser)
        cookie = r.headers["set-cookie"].lower()
        assert cookie.startswith("kidsplay_session=")
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "secure" not in cookie

    async def test_login_rotates_session(self, browser: AsyncClient) -> None:
        """Login issues a new CSRF token, so a pre-login one is useless."""
        before = csrf_from(await browser.get("/login"))
        await do_login(browser)
        after = await logged_in_csrf(browser)
        assert before != after

    async def test_logged_in_login_page_redirects(self, browser: AsyncClient) -> None:
        await do_login(browser)
        r = await browser.get("/login", params={"next": "/queue"})
        assert r.status_code == 303
        assert r.headers["location"] == "/queue"

    async def test_logout_revokes_session_server_side(
        self, browser: AsyncClient
    ) -> None:
        await do_login(browser)
        csrf = await logged_in_csrf(browser)
        stolen = dict(browser.cookies)

        r = await browser.post("/logout", data={"csrf_token": csrf})
        assert r.status_code == 303
        assert r.headers["location"] == "/login"
        assert (await browser.get("/")).status_code == 303

        # Replaying the old cookie does not work either.
        browser.cookies.clear()
        browser.cookies.update(stolen)
        assert (await browser.get("/")).status_code == 303
        assert (await browser.get("/api/v1/profiles")).status_code == 401

    async def test_logout_requires_csrf(self, browser: AsyncClient) -> None:
        await do_login(browser)
        r = await browser.post("/logout")
        assert r.status_code == 403
        assert (await browser.get("/")).status_code == 200

    async def test_forged_cookie_rejected(self, browser: AsyncClient) -> None:
        browser.cookies.set("kidsplay_session", "eyJzaWQiOiAiYWJjIn0=.forged.sig")
        assert (await browser.get("/")).status_code == 303

    async def test_session_survives_restart(
        self, browser: AsyncClient, tmp_path: Path
    ) -> None:
        """The signing key is persisted, so a restart keeps people logged in."""
        await do_login(browser)
        restarted = create_app(tmp_path / "test.db", tmp_path / "media")
        async with AsyncClient(
            transport=ASGITransport(app=restarted),
            base_url="http://test",
            cookies=browser.cookies,
        ) as c:
            assert (await c.get("/")).status_code == 200

    async def test_htmx_request_gets_hx_redirect(self, browser: AsyncClient) -> None:
        r = await browser.get("/queue", headers={"HX-Request": "true"})
        assert r.status_code == 401
        assert r.headers["hx-redirect"] == "/login?next=%2Fqueue"

    async def test_pages_include_csrf_for_htmx_and_logout(
        self, browser: AsyncClient
    ) -> None:
        await do_login(browser)
        r = await browser.get("/profiles")
        csrf = csrf_from(r)
        assert f'hx-headers=\'{{"X-CSRF-Token": "{csrf}"}}\'' in r.text
        assert 'action="/logout"' in r.text

    async def test_tokens_page(self, browser: AsyncClient) -> None:
        await do_login(browser)
        csrf = await logged_in_csrf(browser)
        r = await browser.post(
            "/api/v1/auth/tokens",
            json={"name": "from-web"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201
        page = await browser.get("/tokens")
        assert page.status_code == 200
        assert "from-web" in page.text
        assert r.json()["token"] not in page.text


# ---------------------------------------------------------------------------
# CSRF on cookie-authenticated API calls (the web UI's fetch/HTMX requests)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("configured")
class TestCsrf:
    async def test_unsafe_request_without_token_rejected(
        self, browser: AsyncClient
    ) -> None:
        await do_login(browser)
        r = await browser.post("/api/v1/profiles", json={"name": "Leo"})
        assert r.status_code == 403
        assert r.json()["error_code"] == "CSRF_FAILED"
        # Nothing was created.
        assert (await browser.get("/api/v1/profiles")).json() == []

    async def test_wrong_token_rejected(self, browser: AsyncClient) -> None:
        await do_login(browser)
        r = await browser.post(
            "/api/v1/profiles",
            json={"name": "Leo"},
            headers={"X-CSRF-Token": "not-the-token"},
        )
        assert r.status_code == 403

    async def test_other_sessions_token_rejected(
        self, browser: AsyncClient, app: FastAPI
    ) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as other:
            await do_login(other)
            other_csrf = await logged_in_csrf(other)
        await do_login(browser)
        r = await browser.post(
            "/api/v1/profiles",
            json={"name": "Leo"},
            headers={"X-CSRF-Token": other_csrf},
        )
        assert r.status_code == 403

    async def test_valid_token_accepted(self, browser: AsyncClient) -> None:
        await do_login(browser)
        csrf = await logged_in_csrf(browser)
        headers = {"X-CSRF-Token": csrf}
        r = await browser.post(
            "/api/v1/profiles", json={"name": "Leo"}, headers=headers
        )
        assert r.status_code == 201
        r = await browser.delete(f"/api/v1/profiles/{r.json()['id']}", headers=headers)
        assert r.status_code == 204

    async def test_safe_methods_need_no_token(self, browser: AsyncClient) -> None:
        await do_login(browser)
        assert (await browser.get("/api/v1/profiles")).status_code == 200

    async def test_web_post_route_checks_csrf(self, browser: AsyncClient) -> None:
        await do_login(browser)
        r = await browser.post("/web/photo-proxy", json={"url": ""})
        assert r.status_code == 403
        csrf = await logged_in_csrf(browser)
        r = await browser.post(
            "/web/photo-proxy", json={"url": ""}, headers={"X-CSRF-Token": csrf}
        )
        assert r.status_code == 400  # past auth: "url required"


# ---------------------------------------------------------------------------
# KIDSPLAY_AUTH=disabled
# ---------------------------------------------------------------------------


@pytest.fixture
def disabled_app(tmp_path: Path) -> FastAPI:
    return create_app(
        tmp_path / "test.db", tmp_path / "media", AuthConfig(disabled=True)
    )


class TestDisabledMode:
    async def test_everything_open_without_credentials(
        self, disabled_app: FastAPI
    ) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=disabled_app), base_url="http://test"
        ) as c:
            assert (await c.get("/")).status_code == 200
            assert (await c.get("/docs")).status_code == 200
            r = await c.post("/api/v1/profiles", json={"name": "Leo"})
            assert r.status_code == 201
            r = await c.get("/api/v1/auth/status")
            assert r.json() == {"auth_enabled": False, "method": "disabled"}

    async def test_login_redirects_home_and_no_logout_link(
        self, disabled_app: FastAPI
    ) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=disabled_app), base_url="http://test"
        ) as c:
            r = await c.get("/login")
            assert r.status_code == 303
            assert r.headers["location"] == "/"
            assert 'action="/logout"' not in (await c.get("/")).text

    async def test_device_sync_still_needs_device_key(
        self, disabled_app: FastAPI
    ) -> None:
        async with AsyncClient(
            transport=ASGITransport(app=disabled_app), base_url="http://test"
        ) as c:
            profile = (await c.post("/api/v1/profiles", json={"name": "L"})).json()
            device = (
                await c.post(
                    "/api/v1/devices",
                    json={"name": "GB", "profile_id": profile["id"]},
                )
            ).json()
            r = await c.get(f"/api/v1/devices/{device['id']}/manifest")
            assert r.status_code == 401
            r = await c.get(
                f"/api/v1/devices/{device['id']}/manifest",
                headers={"Authorization": f"Bearer {device['api_key']}"},
            )
            assert r.status_code == 200

    async def test_startup_warning(
        self, disabled_app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kidsplay_server.api.app"):
            async with disabled_app.router.lifespan_context(disabled_app):
                pass
        assert "KIDSPLAY_AUTH=disabled" in caplog.text
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    async def test_no_warning_when_enabled(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kidsplay_server.api.app"):
            async with app.router.lifespan_context(app):
                pass
        assert "KIDSPLAY_AUTH=disabled" not in caplog.text


# ---------------------------------------------------------------------------
# KIDSPLAY_ADMIN_PASSWORD pre-seed and cookie settings
# ---------------------------------------------------------------------------


class TestStartupConfig:
    async def test_admin_password_preseeded(self, tmp_path: Path) -> None:
        app = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(admin_password=PASSWORD),
        )
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
        ):
            assert (await c.get("/setup")).status_code == 303
            r = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
            assert r.status_code == 201

    async def test_preseed_never_overwrites(self, tmp_path: Path) -> None:
        first = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(admin_password=PASSWORD),
        )
        async with first.router.lifespan_context(first):
            pass
        second = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(admin_password="a-different-password"),
        )
        async with (
            second.router.lifespan_context(second),
            AsyncClient(
                transport=ASGITransport(app=second), base_url="http://test"
            ) as c,
        ):
            r = await c.post(
                "/api/v1/auth/login", json={"password": "a-different-password"}
            )
            assert r.status_code == 401
            r = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
            assert r.status_code == 201

    async def test_secret_key_location(self, tmp_path: Path) -> None:
        create_app(tmp_path / "test.db", tmp_path / "media")
        assert (tmp_path / "session_secret.key").exists()
        custom = tmp_path / "keys" / "custom.key"
        create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(secret_key_path=custom),
        )
        assert custom.exists()

    async def test_cookie_secure(self, tmp_path: Path) -> None:
        app = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(admin_password=PASSWORD, cookie_secure=True),
        )
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c,
        ):
            r = await do_login(c)
            assert r.status_code == 303
            assert "secure" in r.headers["set-cookie"].lower()
