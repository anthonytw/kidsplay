"""FastAPI application factory.

The single public symbol is ``create_app``.  All configuration is passed
explicitly — there are no module-level globals — so the factory is safe to
call multiple times in tests with different temporary paths.

Typical usage::

    app = create_app(Path("/var/lib/kidsplay/db.sqlite"), Path("/mnt/media"))
    uvicorn.run(app, host="0.0.0.0", port=8000)
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from kidsplay_models.auth import MAX_PASSWORD_LENGTH
from kidsplay_server.auth import (
    MIN_PASSWORD_LENGTH,
    SESSION_LIFETIME,
    AuthConfig,
    LoginThrottle,
    SetupCode,
    init_auth_db,
    is_admin_configured,
    load_or_create_secret_key,
    set_initial_admin_password,
    setup_code_banner,
)
from kidsplay_server.database import (
    configure_conn,
    ensure_private_db_file,
    get_or_create_server_id,
    init_db,
)
from kidsplay_server.i18n import remember_language, resolve_language, use_language
from kidsplay_server.importers import ImporterRegistry, get_default_registry
from kidsplay_server.pairing import PairingLimits, run_purge_loop
from kidsplay_server.processing.loudness_backfill import LoudnessBackfill
from kidsplay_server.processing.queue_worker import run_queue_worker
from kidsplay_server.processing.resources import ResourceLimits, configure_limits
from kidsplay_server.proxy import ProxyHeadersMiddleware, SecureCookieMiddleware
from kidsplay_server.server_settings import check_environment, load_server_settings
from kidsplay_server.storage import MediaStore
from kidsplay_server.web.auth_routes import router as web_auth_router
from kidsplay_server.web.routes import router as web_router

from .auth import LoginRequired, login_redirect, require_admin, require_admin_page
from .auth import router as auth_router
from .devices import router as devices_router
from .importers import router as importers_router
from .media import router as media_router
from .pairing import admin_router as pairing_admin_router
from .pairing import device_router as pairing_device_router
from .queue import router as queue_router
from .settings import router as settings_router
from .sync import router as sync_router
from .themes import router as themes_router

if TYPE_CHECKING:
    from kidsplay_server.discovery import AdvertiseConfig

logger = logging.getLogger(__name__)


def create_app_from_env() -> FastAPI:
    """No-argument factory for ``uvicorn --factory``.

    Reads ``db_path`` and ``media_store_root`` from the module-level
    ``settings`` object (populated from environment variables).  Use this
    entry point when starting the server from the command line::

        uv run uvicorn kidsplay_server.api.app:create_app_from_env --factory \\
            --no-proxy-headers

    Returns:
        Configured ``FastAPI`` application.
    """
    from kidsplay_server.config import settings
    from kidsplay_server.discovery import advertise_config_from_env
    from kidsplay_server.logging_setup import configure_logging

    configure_logging(settings.log_file_path)
    # Fail at startup, not at the first device sync, on a mistyped value.
    check_environment()
    return create_app(
        settings.db_path,
        settings.media_store_root,
        settings.auth,
        limits=settings.limits,
        advertise=advertise_config_from_env(),
    )


_AUTH_DISABLED_WARNING = """
**********************************************************************
*  KIDSPLAY_AUTH=disabled: admin authentication is OFF.              *
*  Anyone who can reach this server can manage media, profiles and   *
*  devices. Only run like this behind a proxy that authenticates.    *
**********************************************************************"""


def _seed_password_problem(password: str) -> str | None:
    """Return why ``KIDSPLAY_ADMIN_PASSWORD`` is not an acceptable password."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"shorter than the {MIN_PASSWORD_LENGTH} characters the setup page requires"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        return (
            f"longer than the {MAX_PASSWORD_LENGTH} characters the login form accepts"
        )
    return None


async def _seed_admin_password(conn: aiosqlite.Connection, password: str) -> None:
    """Store ``KIDSPLAY_ADMIN_PASSWORD`` unless an admin password exists.

    Holds the seed to the same length rules as the setup page.

    Raises:
        RuntimeError: If no admin password exists yet and the seed breaks the
            rules. Starting with a password that could not have been chosen
            in the UI would be worse than not starting.
    """
    problem = _seed_password_problem(password)
    if await is_admin_configured(conn):
        if problem:
            logger.warning(
                "KIDSPLAY_ADMIN_PASSWORD is %s; it is ignored because an admin "
                "password is already set. Remove it or make it valid.",
                problem,
            )
        logger.info("Admin password already set; ignoring KIDSPLAY_ADMIN_PASSWORD")
        return
    if problem:
        raise RuntimeError(
            f"KIDSPLAY_ADMIN_PASSWORD is {problem}. Use a longer password, or "
            "unset it to choose one on the setup page."
        )
    if await set_initial_admin_password(conn, password):
        logger.info("Admin password set from KIDSPLAY_ADMIN_PASSWORD")


def create_app(
    db_path: Path,
    media_store_root: Path,
    auth: AuthConfig | None = None,
    *,
    importers: ImporterRegistry | None = None,
    limits: ResourceLimits | None = None,
    advertise: "AdvertiseConfig | None" = None,
) -> FastAPI:
    """Create and configure the KidsPlay FastAPI application.

    Initialises the database schema on startup via the lifespan handler.
    Stores ``db_path``, a ``MediaStore``, the importer registry and the
    loudness backfill runner in ``app.state`` for
    dependency injection by route handlers.

    Every route except login/setup, static files and the device sync
    endpoints requires an admin session or admin API token (see
    ``kidsplay_server.api.auth``). The session-signing key is read from
    ``auth.secret_key_path`` (default: next to ``db_path``), and generated
    there on first run.

    A custom exception handler unwraps dict-shaped ``HTTPException.detail``
    values so that errors are returned in the documented format::

        {"detail": "Human-readable message", "error_code": "MACHINE_CODE"}

    Args:
        db_path: Path to the SQLite database file.  Created on first use.
        media_store_root: Root directory for content-addressed storage.
            Created by ``MediaStore.__init__`` if absent.
        auth: Authentication settings. Defaults to auth enabled with no
            pre-seeded password.
        importers: Importers to offer. Defaults to the process-wide registry
            discovered from the ``kidsplay.importers`` entry points.
        limits: Niceness and concurrency limits for ffmpeg, applied
            process-wide. Defaults to none.
        advertise: Announce the server on the local network (mDNS) so
            handhelds can find it while pairing. Defaults to off; only
            ``create_app_from_env`` turns it on.

    Returns:
        Configured ``FastAPI`` instance ready for serving or testing.
    """
    auth = auth or AuthConfig()
    media_store = MediaStore(media_store_root)
    registry = importers if importers is not None else get_default_registry()
    configure_limits(limits or ResourceLimits())
    backfill = LoudnessBackfill(db_path)
    # Needed by /setup while no admin password exists; printed in the log by the
    # lifespan handler, and retired as soon as a password is set.
    setup_code = SetupCode(
        on_rotate=lambda code: logger.warning(setup_code_banner(code))
    )
    secret_key = load_or_create_secret_key(
        auth.secret_key_path or db_path.parent / "session_secret.key"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ensure_private_db_file(db_path)
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            await init_auth_db(conn)
            if auth.admin_password:
                await _seed_admin_password(conn, auth.admin_password)
            await conn.commit()
            if await is_admin_configured(conn):
                setup_code.clear()
            elif not auth.disabled and setup_code.code is not None:
                logger.warning(setup_code_banner(setup_code.code))
            server_id = await get_or_create_server_id(conn)
            pairing_enabled = (await load_server_settings(conn)).values.pairing_enabled

        if auth.disabled:
            logger.warning(_AUTH_DISABLED_WARNING)

        logger.info(
            "KidsPlay server starting — db=%s store=%s importers=%s",
            db_path,
            media_store_root,
            ",".join(i.name for i in registry),
        )

        # Handhelds look for the server only to pair, so it is announced only
        # while pairing is allowed (the settings page switches it live).
        advertising = None
        if advertise is not None:
            from kidsplay_server.discovery import AdvertisingSwitch

            advertising = AdvertisingSwitch(replace(advertise, server_id=server_id))
            app.state.advertising = advertising
            await advertising.set_enabled(pairing_enabled)

        shutdown_event = asyncio.Event()
        purge_task = asyncio.create_task(
            run_purge_loop(db_path, shutdown_event), name="pairing-purge"
        )
        worker_task = asyncio.create_task(
            run_queue_worker(
                db_path,
                media_store,
                registry,
                shutdown_event=shutdown_event,
            )
        )
        try:
            yield
        finally:
            logger.info("KidsPlay server shutting down")
            if advertising is not None:
                await advertising.stop()
                app.state.advertising = None
            shutdown_event.set()
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            await purge_task

    # The default docs routes are unauthenticated; they are re-added below
    # behind admin auth.
    app = FastAPI(
        title="KidsPlay Server",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db_path = db_path
    app.state.media_store = media_store
    app.state.importers = registry
    app.state.loudness_backfill = backfill
    app.state.auth_config = auth
    app.state.login_throttle = LoginThrottle()
    app.state.setup_code = setup_code
    app.state.pairing_limits = PairingLimits.default()

    # Signed (itsdangerous), HttpOnly session cookie. It only carries a random
    # session id and the CSRF token; the session itself lives in the database.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="kidsplay_session",
        max_age=int(SESSION_LIFETIME.total_seconds()),
        same_site="lax",
        https_only=auth.cookie_secure is True,
    )
    if auth.cookie_secure is None:
        # Automatic: Secure whenever the request arrived over HTTPS.
        app.add_middleware(SecureCookieMiddleware, cookie_name="kidsplay_session")
    # Outermost, so the client address and scheme are corrected before anything
    # else looks at them (throttles, logs, the cookie flag).
    app.add_middleware(ProxyHeadersMiddleware, trusted_proxies=auth.trusted_proxies)

    @app.middleware("http")
    async def _log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Log method, path, status, and duration for every request."""
        start = time.monotonic()
        response = await call_next(request)
        if not request.url.path.startswith("/static"):
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "%s %s → %d (%.0fms)",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
        return response

    @app.middleware("http")
    async def _remember_language(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Pick the request's language and keep a ``?lang=xx`` pick in a cookie."""
        use_language(resolve_language(request))
        response = await call_next(request)
        remember_language(request, response)
        return response

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        """Return errors in the documented ``{detail, error_code}`` format."""
        if isinstance(exc.detail, dict):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc.detail), "error_code": "ERROR"},
        )

    _static_dir = Path(__file__).parent.parent / "web" / "static"
    app.mount(
        "/static",
        StaticFiles(directory=str(_static_dir)),
        name="static",
    )

    app.add_exception_handler(LoginRequired, login_redirect)

    admin_page = [Depends(require_admin_page)]
    admin_api = [Depends(require_admin)]

    @app.get("/openapi.json", include_in_schema=False, dependencies=admin_api)
    async def _openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False, dependencies=admin_page)
    async def _swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title=app.title)

    @app.get("/redoc", include_in_schema=False, dependencies=admin_page)
    async def _redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url="/openapi.json", title=app.title)

    # Public: login, logout and first-run setup (they check CSRF themselves).
    app.include_router(web_auth_router)
    app.include_router(web_router, dependencies=admin_page)
    # /auth/login is public (password-checked); its other routes are guarded
    # individually.
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(media_router, prefix="/api/v1", dependencies=admin_api)
    app.include_router(devices_router, prefix="/api/v1", dependencies=admin_api)
    # Device-facing: per-device Bearer API keys, never admin auth.
    app.include_router(sync_router, prefix="/api/v1")
    # Device-facing and unauthenticated by necessity (a device with no
    # credentials is being set up): rate-limited per client, and the API key
    # goes only to the holder of the binding secret. See api/pairing.py.
    app.include_router(pairing_device_router, prefix="/api/v1")
    app.include_router(pairing_admin_router, prefix="/api/v1", dependencies=admin_api)
    app.include_router(queue_router, prefix="/api/v1", dependencies=admin_api)
    app.include_router(importers_router, prefix="/api/v1", dependencies=admin_api)
    app.include_router(settings_router, prefix="/api/v1", dependencies=admin_api)
    app.include_router(themes_router, prefix="/api/v1", dependencies=admin_api)

    return app
