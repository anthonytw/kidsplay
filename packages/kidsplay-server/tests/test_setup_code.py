"""Tests for the one-time first-run setup code (#22).

``/setup`` must not be claimable by whoever reaches it first: it needs the
code the server prints in its log, once, and the seeded-password and
all-in-one paths must keep working without one.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from kidsplay_models import PAIRING_CODE_ALPHABET
from kidsplay_server import auth
from kidsplay_server.allinone import initialize_server
from kidsplay_server.api.app import create_app
from kidsplay_server.auth import (
    SETUP_CODE_LENGTH,
    SETUP_CODE_MAX_FAILURES,
    AuthConfig,
    SetupCode,
    init_auth_db,
    is_admin_configured,
    setup_code_banner,
)
from kidsplay_server.database import configure_conn, init_db
from kidsplay_server.proxy import parse_trusted_proxies

PASSWORD = "a-good-long-password"


# ---------------------------------------------------------------------------
# SetupCode
# ---------------------------------------------------------------------------


class TestSetupCode:
    def test_shape(self) -> None:
        code = SetupCode().code
        assert code is not None
        left, dash, right = code.partition("-")
        assert dash == "-"
        assert len(left) + len(right) == SETUP_CODE_LENGTH
        assert set(left + right) <= set(PAIRING_CODE_ALPHABET)

    def test_codes_differ(self) -> None:
        assert len({SetupCode().code for _ in range(20)}) == 20

    def test_accepts_the_code_as_typed(self) -> None:
        setup = SetupCode()
        code = setup.code
        assert code
        assert setup.verify(code)
        assert setup.verify(code.lower())
        assert setup.verify(code.replace("-", ""))
        assert setup.verify(f"  {code[:3]} {code[3:]}\n")

    @pytest.mark.parametrize(
        "guess",
        ["", " ", "-", "AAAAA-AAAAA", "ААААА-АА", "x" * 500],
    )
    def test_rejects_other_input(self, guess: str) -> None:
        setup = SetupCode()
        assert not setup.verify(guess)

    def test_rejects_prefixes_and_extensions(self) -> None:
        setup = SetupCode()
        code = setup.code
        assert code
        assert not setup.verify(code[:-1])
        assert not setup.verify(code + "A")
        assert not setup.verify("")

    def test_cleared_code_is_worthless(self) -> None:
        setup = SetupCode()
        code = setup.code
        assert code
        setup.clear()
        assert setup.code is None
        assert not setup.verify(code)
        # Nor does an empty guess or a run of dashes match "nothing".
        assert not setup.verify("")
        assert not setup.verify("-" * SETUP_CODE_LENGTH)
        assert not setup.verify("----------")

    def test_comparison_is_constant_time_and_always_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[bytes, bytes]] = []
        real = auth.hmac.compare_digest

        def spy(a: bytes, b: bytes) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(auth.hmac, "compare_digest", spy)
        setup = SetupCode()
        code = setup.code
        assert code
        setup.verify("short")
        setup.verify(code)
        setup.clear()
        setup.verify(code)
        assert len(calls) == 3

    def test_rotates_after_too_many_wrong_guesses(self) -> None:
        rotated: list[str] = []
        setup = SetupCode(on_rotate=rotated.append)
        old = setup.code
        assert old
        for _ in range(SETUP_CODE_MAX_FAILURES - 1):
            assert not setup.verify("WRONG-GUESS")
        assert setup.code == old
        assert not setup.verify("WRONG-GUESS")
        assert setup.code != old
        assert rotated == [setup.code]
        # The old code no longer works; the new one does.
        assert not setup.verify(old)
        assert setup.code
        assert setup.verify(setup.code)

    def test_a_correct_guess_is_not_a_failure(self) -> None:
        setup = SetupCode()
        code = setup.code
        assert code
        for _ in range(SETUP_CODE_MAX_FAILURES - 1):
            setup.verify("WRONG-GUESS")
        assert setup.verify(code)
        assert setup.verify(code)


def test_banner_shows_the_code_in_a_tidy_box() -> None:
    banner = setup_code_banner("ABCDE-FGHJK")
    assert "ABCDE-FGHJK" in banner
    box = [line for line in banner.splitlines() if line]
    assert len({len(line) for line in box}) == 1
    assert "KIDSPLAY_ADMIN_PASSWORD" in banner


# ---------------------------------------------------------------------------
# /setup
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _csrf(response: Response) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    assert match
    return match.group(1)


async def post_setup(
    browser: AsyncClient,
    code: str | None,
    password: str = PASSWORD,
    confirm: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    # Either page carries a CSRF token: /setup, or /login once setup is done.
    page = await browser.get("/setup", follow_redirects=True)
    data = {
        "password": password,
        "password_confirm": password if confirm is None else confirm,
        "csrf_token": _csrf(page),
    }
    if code is not None:
        data["setup_code"] = code
    return await browser.post("/setup", data=data, headers=headers)


async def assert_no_admin(app: FastAPI) -> None:
    """No admin password exists: setup has not happened."""
    async with aiosqlite.connect(app.state.db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await init_auth_db(conn)
        assert not await is_admin_configured(conn)


class TestSetupNeedsTheCode:
    async def test_page_asks_for_the_code_but_never_shows_it(
        self, app: FastAPI, browser: AsyncClient
    ) -> None:
        r = await browser.get("/setup")
        assert r.status_code == 200
        assert 'name="setup_code"' in r.text
        assert "docker compose logs" in r.text
        code = app.state.setup_code.code
        assert code
        assert code not in r.text
        assert code.replace("-", "") not in r.text

    @pytest.mark.parametrize("code", [None, "", "   ", "AAAAA-AAAAA"])
    async def test_without_the_right_code_setup_is_rejected(
        self, app: FastAPI, browser: AsyncClient, code: str | None
    ) -> None:
        r = await post_setup(browser, code)
        assert r.status_code == 403
        assert "setup code" in r.text.lower()
        real = app.state.setup_code.code
        assert real not in r.text
        await assert_no_admin(app)
        # And no session was started.
        assert (await browser.get("/api/v1/auth/status")).status_code == 401

    async def test_the_right_code_sets_the_password_once(
        self, app: FastAPI, browser: AsyncClient
    ) -> None:
        code = app.state.setup_code.code
        assert code
        r = await post_setup(browser, code)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        assert (await browser.get("/api/v1/auth/status")).json()["method"] == "session"

        # The code is spent: a second visitor with the same code gets nowhere,
        # and cannot replace the password.
        assert app.state.setup_code.code is None
        assert not app.state.setup_code.verify(code)
        other = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        async with other:
            r = await post_setup(other, code, password="attacker-password")
            assert r.status_code == 303
            assert r.headers["location"] == "/login"
            r = await other.post(
                "/api/v1/auth/login", json={"password": "attacker-password"}
            )
            assert r.status_code == 401
            r = await other.post("/api/v1/auth/login", json={"password": PASSWORD})
            assert r.status_code == 201

    async def test_code_is_forgiving_about_case_and_spacing(
        self, app: FastAPI, browser: AsyncClient
    ) -> None:
        code = app.state.setup_code.code
        assert code
        r = await post_setup(browser, f" {code.lower().replace('-', ' ')} ")
        assert r.status_code == 303

    async def test_a_bad_password_does_not_burn_the_code(
        self, app: FastAPI, browser: AsyncClient
    ) -> None:
        code = app.state.setup_code.code
        assert code
        assert (await post_setup(browser, code, password="short")).status_code == 400
        assert (
            await post_setup(browser, code, confirm="different-password")
        ).status_code == 400
        assert app.state.setup_code.code == code
        assert (await post_setup(browser, code)).status_code == 303

    async def test_wrong_codes_are_throttled_per_client(
        self, app: FastAPI, browser: AsyncClient
    ) -> None:
        code = app.state.setup_code.code
        assert code
        for _ in range(10):
            assert (await post_setup(browser, "AAAAA-AAAAA")).status_code == 403
        # Out of attempts: even the right code is refused for now.
        assert (await post_setup(browser, code)).status_code == 429
        await assert_no_admin(app)

    async def test_a_correct_code_does_not_use_up_the_budget(
        self, app: FastAPI, browser: AsyncClient
    ) -> None:
        code = app.state.setup_code.code
        assert code
        for _ in range(9):
            await post_setup(browser, "AAAAA-AAAAA")
        # Typos in the password after a correct code are not guesses.
        for _ in range(5):
            assert (await post_setup(browser, code, password="x")).status_code == 400
        assert (await post_setup(browser, code)).status_code == 303

    async def test_spreading_guesses_over_clients_rotates_the_code(
        self, tmp_path: Path
    ) -> None:
        """A distributed guesser gets a new code instead of unlimited tries."""
        app = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(trusted_proxies=parse_trusted_proxies("172.18.0.1")),
        )
        old = app.state.setup_code.code
        assert old
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("172.18.0.1", 1)),
            base_url="http://test",
        ) as c:
            for i in range(SETUP_CODE_MAX_FAILURES):
                r = await post_setup(
                    c, "AAAAA-AAAAA", headers={"X-Forwarded-For": f"203.0.113.{i}"}
                )
                assert r.status_code == 403
            new = app.state.setup_code.code
            assert new and new != old
            # Guess number 21 and the old (say, leaked or guessed) code fail...
            r = await post_setup(c, old, headers={"X-Forwarded-For": "203.0.113.99"})
            assert r.status_code == 403
            await assert_no_admin(app)
            # ...while the owner, reading the log, uses the new one.
            r = await post_setup(c, new, headers={"X-Forwarded-For": "203.0.113.98"})
            assert r.status_code == 303

    async def test_only_one_of_two_racing_setups_wins(
        self, app: FastAPI, tmp_path: Path
    ) -> None:
        code = app.state.setup_code.code
        assert code

        async def attempt(password: str) -> Response:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                return await post_setup(c, code, password=password)

        first, second = await asyncio.gather(
            attempt("first-password-long"), attempt("second-password-long")
        )
        assert sorted([first.status_code, second.status_code]) == [303, 303]
        winners = [r for r in (first, second) if r.headers["location"] == "/"]
        assert len(winners) == 1

    async def test_csrf_is_still_required(
        self, app: FastAPI, browser: AsyncClient
    ) -> None:
        code = app.state.setup_code.code
        r = await browser.post(
            "/setup",
            data={
                "password": PASSWORD,
                "password_confirm": PASSWORD,
                "setup_code": code,
            },
        )
        assert r.status_code == 403
        assert r.json()["error_code"] == "CSRF_FAILED"
        await assert_no_admin(app)

    async def test_overlong_code_is_rejected_by_validation(
        self, browser: AsyncClient
    ) -> None:
        r = await post_setup(browser, "A" * 65)
        assert r.status_code == 422

    async def test_no_other_route_sets_the_password(
        self, app: FastAPI, browser: AsyncClient
    ) -> None:
        for method, path in [("PUT", "/setup"), ("PATCH", "/setup")]:
            r = await browser.request(method, path)
            assert r.status_code in (404, 405)
        await assert_no_admin(app)


# ---------------------------------------------------------------------------
# Startup: printing the code, and the paths that never need one
# ---------------------------------------------------------------------------


class TestStartup:
    async def test_fresh_install_prints_the_code(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kidsplay_server"):
            async with app.router.lifespan_context(app):
                pass
        code = app.state.setup_code.code
        assert code
        assert any(
            code in r.getMessage() and "Setup code" in r.getMessage()
            for r in caplog.records
        )

    async def test_restart_makes_a_new_code(self, tmp_path: Path) -> None:
        first = create_app(tmp_path / "test.db", tmp_path / "media")
        second = create_app(tmp_path / "test.db", tmp_path / "media")
        assert first.state.setup_code.code != second.state.setup_code.code
        assert not second.state.setup_code.verify(first.state.setup_code.code)

    async def test_seeded_password_needs_no_code(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = create_app(
            tmp_path / "test.db",
            tmp_path / "media",
            AuthConfig(admin_password=PASSWORD),
        )
        with caplog.at_level(logging.WARNING, logger="kidsplay_server"):
            async with (
                app.router.lifespan_context(app),
                AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as c,
            ):
                assert app.state.setup_code.code is None
                assert "Setup code" not in caplog.text
                r = await c.get("/setup")
                assert r.status_code == 303
                assert r.headers["location"] == "/login"
                r = await c.post("/api/v1/auth/login", json={"password": PASSWORD})
                assert r.status_code == 201

    async def test_all_in_one_install_needs_no_code(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``kidsplay-allinone`` sets the password itself, before the server."""
        db_path = tmp_path / "db.sqlite"
        _, admin_set = await initialize_server(db_path, PASSWORD, "Kid", "Handheld")
        assert admin_set
        app = create_app(db_path, tmp_path / "media")
        with caplog.at_level(logging.WARNING, logger="kidsplay_server"):
            async with app.router.lifespan_context(app):
                assert app.state.setup_code.code is None
        assert "Setup code" not in caplog.text

    async def test_existing_install_never_prints_a_code(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        db_path = tmp_path / "db.sqlite"
        await initialize_server(db_path, PASSWORD, "Kid", "Handheld")
        for _ in range(2):  # every restart
            app = create_app(db_path, tmp_path / "media")
            async with app.router.lifespan_context(app):
                assert app.state.setup_code.code is None
        assert "Setup code" not in caplog.text

    async def test_disabled_auth_prints_no_code(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = create_app(
            tmp_path / "test.db", tmp_path / "media", AuthConfig(disabled=True)
        )
        with caplog.at_level(logging.WARNING, logger="kidsplay_server"):
            async with app.router.lifespan_context(app):
                pass
        assert "Setup code" not in caplog.text

    async def test_rotation_prints_the_new_code(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kidsplay_server"):
            for _ in range(SETUP_CODE_MAX_FAILURES):
                app.state.setup_code.verify("AAAAA-AAAAA")
        assert app.state.setup_code.code in caplog.text
