"""Admin authentication: FastAPI dependencies and the ``/auth`` API routes.

Every management route (web UI, media/profile/device/queue APIs, ``/docs``)
depends on ``require_admin`` (JSON API: 401) or ``require_admin_page`` (web
UI: redirect to ``/login``). The device sync endpoints do not: they keep
their per-device Bearer keys (``deps.get_authenticated_device``).

A request is an admin if either:

* it sends ``Authorization: Bearer kpa_...`` with a valid admin API token
  (the CLI and scripts), or
* it carries a signed session cookie for a live admin session (the browser).
  Cookie-authenticated requests with an unsafe method must also send the
  session's CSRF token in the ``X-CSRF-Token`` header; the web UI's
  ``fetch`` and HTMX calls add it automatically (see ``base.html``).

If an ``Authorization`` header is present it alone decides: a bad token is
rejected even if the browser also has a valid cookie.

Routes
------
POST   /auth/login              — exchange the admin password for an API token
GET    /auth/status             — how the caller is authenticated
GET    /auth/tokens             — list admin API tokens
POST   /auth/tokens             — create an admin API token
DELETE /auth/tokens/{token_id}  — revoke an admin API token
"""

import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.utils import get_authorization_scheme_param

from kidsplay_models.auth import (
    AdminLoginRequest,
    AdminPasswordChange,
    AdminToken,
    AdminTokenCreate,
    AdminTokenCreated,
    AuthStatus,
)
from kidsplay_server.auth import (
    MIN_PASSWORD_LENGTH,
    AuthConfig,
    LoginThrottle,
    SetupCode,
    change_admin_password,
    check_admin_password,
    create_admin_token,
    delete_admin_token,
    delete_admin_tokens,
    delete_sessions,
    is_admin_configured,
    is_admin_token_valid,
    is_session_valid,
    list_admin_tokens,
    verify_admin_token,
)
from kidsplay_server.database import configure_conn
from kidsplay_server.proxy import throttle_key

from .deps import DBConn

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
SESSION_ID_KEY = "sid"
CSRF_KEY = "csrf"

_bearer = HTTPBearer(
    auto_error=False,
    description="Admin API token (kpa_...). Create one with `kidsplay auth login` "
    "or on the Tokens page.",
)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


@dataclass(frozen=True)
class AdminPrincipal:
    """An authenticated admin request.

    Attributes:
        method: How the request authenticated.
        token_id: The admin API token used, when ``method == "token"``.
    """

    method: Literal["session", "token", "disabled"]
    token_id: uuid.UUID | None = None


class LoginRequired(Exception):
    """Raised by ``require_admin_page``; the app turns it into a login redirect.

    Args:
        next_url: Path (and query) to return to after logging in.
    """

    def __init__(self, next_url: str) -> None:
        super().__init__(next_url)
        self.next_url = next_url


def get_auth_config(request: Request) -> AuthConfig:
    """Return the app's ``AuthConfig``.

    Args:
        request: Current request.

    Returns:
        The config passed to ``create_app``.
    """
    config: AuthConfig = request.app.state.auth_config
    return config


def get_login_throttle(request: Request) -> LoginThrottle:
    """Return the app's shared ``LoginThrottle``.

    Args:
        request: Current request.

    Returns:
        The throttle for failed password attempts.
    """
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


def get_setup_code(request: Request) -> SetupCode:
    """Return the app's first-run ``SetupCode``.

    Args:
        request: Current request.

    Returns:
        The code object (already cleared once the admin password is set).
    """
    setup_code: SetupCode = request.app.state.setup_code
    return setup_code


def client_ip(request: Request) -> str:
    """Return the client's address, for display and logs.

    Behind a trusted reverse proxy (``KIDSPLAY_TRUSTED_PROXIES``) this is the
    real client taken from ``X-Forwarded-For``; otherwise the TCP peer. Headers
    from any other peer are ignored (see ``kidsplay_server.proxy``).

    Args:
        request: Current request.

    Returns:
        The address, or ``"unknown"`` if the server does not know it.
    """
    return request.client.host if request.client and request.client.host else "unknown"


def client_key(request: Request) -> str:
    """Identify the client for every throttle (login, setup, pairing).

    Same address as ``client_ip``, but IPv6 clients are grouped by /64 so a
    host cannot get a fresh budget by changing address.

    Args:
        request: Current request.

    Returns:
        The throttle key.
    """
    return throttle_key(client_ip(request))


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def get_csrf_token(request: Request) -> str:
    """Return the session's CSRF token, creating one if needed.

    Args:
        request: Current request (must pass through ``SessionMiddleware``).

    Returns:
        The token to embed in pages and echo back on unsafe requests.
    """
    token = request.session.get(CSRF_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_KEY] = token
    return token


def csrf_token_matches(request: Request, submitted: str | None) -> bool:
    """Compare a submitted CSRF token with the session's, in constant time.

    Args:
        request: Current request.
        submitted: Token from the ``X-CSRF-Token`` header or a form field.

    Returns:
        True only if the session has a token and ``submitted`` equals it.
    """
    expected = request.session.get(CSRF_KEY)
    if not isinstance(expected, str) or not expected or not submitted:
        return False
    return hmac.compare_digest(expected.encode(), submitted.encode())


def _host_of(url: str) -> str | None:
    """Return the lower-cased ``host[:port]`` of ``url``, or None."""
    try:
        return urlsplit(url).netloc.lower() or None
    except ValueError:
        return None


def is_cross_site_write(request: Request) -> bool:
    """Tell whether a browser sent this request from another origin.

    With ``KIDSPLAY_AUTH=disabled`` there is no session and so no CSRF token,
    yet browsers still attach the credentials of an authenticating proxy
    (Authelia's cookie, ...) to a request a malicious page makes. A sibling
    site on the same parent domain is even "same-site", which ``SameSite=Lax``
    lets through. So an unsafe request is refused when the browser says it is
    not same-origin:

    * ``Sec-Fetch-Site`` (all current browsers; a page cannot forge it) must be
      ``same-origin`` or ``none`` (typed URL or bookmark); ``same-site`` and
      ``cross-site`` are refused.
    * Without it (old browsers), an ``Origin`` header, if present, must name
      this server (``Host`` or ``X-Forwarded-Host``); ``null`` is refused.
    * With neither header the caller is not a browser (the CLI, scripts,
      ``curl``) and cannot be tricked into anything by a web page.

    Args:
        request: Current request.

    Returns:
        True if the request should be refused as cross-site.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.strip().lower() not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return False
    origin_host = _host_of(origin) if origin.strip().lower() != "null" else None
    if origin_host is None:
        return True
    ours: set[str] = set()
    for name in ("host", "x-forwarded-host"):
        host = request.headers.get(name, "").strip().lower()
        if host:
            # Browsers leave a default port out of ``Origin``; a proxy may not.
            ours.update({host, host.removesuffix(":80"), host.removesuffix(":443")})
    return origin_host not in ours


async def admin_still_authenticated(request: Request) -> bool:
    """Re-check, on a fresh connection, that a long-lived request's login holds.

    For streams (``/logs/stream``) that were authenticated when they opened:
    logging out, changing the password, revoking the token or the session
    expiring must end them too.

    Args:
        request: The request that opened the stream.

    Returns:
        True if the request would still be accepted as the admin.
    """
    if get_auth_config(request).disabled:
        return True
    async with aiosqlite.connect(request.app.state.db_path) as conn:
        await configure_conn(conn)
        if "authorization" in request.headers:
            scheme, token = get_authorization_scheme_param(
                request.headers["authorization"]
            )
            return scheme.lower() == "bearer" and await is_admin_token_valid(
                conn, token
            )
        session_id = request.session.get(SESSION_ID_KEY)
        return isinstance(session_id, str) and await is_session_valid(conn, session_id)


def auth_template_context(request: Request) -> dict[str, object]:
    """Jinja2 context processor exposing auth state to every template.

    Args:
        request: Current request.

    Returns:
        ``csrf_token`` for forms and scripts, and ``auth_enabled``.
    """
    return {
        "csrf_token": get_csrf_token(request),
        "auth_enabled": not get_auth_config(request).disabled,
    }


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def authenticate_admin(
    request: Request, db: DBConn, credentials: BearerCredentials
) -> AdminPrincipal | None:
    """Work out whether the request comes from the admin.

    Args:
        request: Current request.
        db: Database connection (injected).
        credentials: Parsed ``Authorization: Bearer`` header, if any.

    Returns:
        The principal, or None if the request is not authenticated.

    Raises:
        HTTPException: 403 if a cookie-authenticated unsafe request lacks a
            valid CSRF token, or (auth disabled) a browser says an unsafe
            request comes from another site.
    """
    if get_auth_config(request).disabled:
        if request.method not in SAFE_METHODS and is_cross_site_write(request):
            raise HTTPException(
                status_code=403,
                detail={
                    "detail": "Cross-site request refused",
                    "error_code": "CSRF_FAILED",
                },
            )
        return AdminPrincipal(method="disabled")

    if "authorization" in request.headers:
        if credentials is None:
            return None
        token_id = await verify_admin_token(db, credentials.credentials)
        if token_id is None:
            return None
        await db.commit()  # persist last_used_at
        return AdminPrincipal(method="token", token_id=token_id)

    session_id = request.session.get(SESSION_ID_KEY)
    if not isinstance(session_id, str):
        return None
    if not await is_session_valid(db, session_id):
        request.session.pop(SESSION_ID_KEY, None)
        return None
    if request.method not in SAFE_METHODS and not csrf_token_matches(
        request, request.headers.get(CSRF_HEADER)
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "Missing or invalid CSRF token",
                "error_code": "CSRF_FAILED",
            },
        )
    return AdminPrincipal(method="session")


async def require_admin(
    request: Request, db: DBConn, credentials: BearerCredentials
) -> AdminPrincipal:
    """Dependency for admin JSON API routes.

    Args:
        request: Current request.
        db: Database connection (injected).
        credentials: Parsed Bearer header (injected).

    Returns:
        The authenticated principal.

    Raises:
        HTTPException: 401 if not authenticated; 403 on a CSRF failure.
    """
    principal = await authenticate_admin(request, db, credentials)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail={
                "detail": "Admin authentication required",
                "error_code": "UNAUTHORIZED",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def require_admin_page(
    request: Request, db: DBConn, credentials: BearerCredentials
) -> AdminPrincipal:
    """Dependency for web UI pages: like ``require_admin`` but redirects.

    Args:
        request: Current request.
        db: Database connection (injected).
        credentials: Parsed Bearer header (injected).

    Returns:
        The authenticated principal.

    Raises:
        LoginRequired: If not authenticated (handled as a redirect to login).
        HTTPException: 403 on a CSRF failure.
    """
    principal = await authenticate_admin(request, db, credentials)
    if principal is None:
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        raise LoginRequired(next_url)
    return principal


AdminAuth = Annotated[AdminPrincipal, Depends(require_admin)]


def safe_next_url(target: str | None) -> str:
    """Return ``target`` if it is a same-site path, else ``"/"``.

    Prevents the login page's ``next`` parameter being used as an open
    redirect (``//evil.example``, ``https://...``, ``/\\evil.example``).

    Args:
        target: Candidate redirect target.

    Returns:
        A path on this server.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target or any(ord(c) < 0x20 or ord(c) == 0x7F for c in target):
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    return target


def login_redirect(request: Request, exc: Exception) -> Response:
    """Exception handler turning ``LoginRequired`` into a redirect to login.

    HTMX requests get a 401 with ``HX-Redirect`` so HTMX navigates the page
    instead of swapping the login page into a fragment.

    Args:
        request: Current request.
        exc: The ``LoginRequired`` raised by ``require_admin_page``.

    Returns:
        A 303 redirect (or 401 with ``HX-Redirect`` for HTMX).
    """
    next_url = exc.next_url if isinstance(exc, LoginRequired) else "/"
    target = "/login?" + urlencode({"next": safe_next_url(next_url)})
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=AdminTokenCreated, status_code=201)
async def login_for_token(
    body: AdminLoginRequest, request: Request, db: DBConn
) -> AdminTokenCreated:
    """Exchange the admin password for a new admin API token.

    Used by ``kidsplay auth login``. Public, but throttled per client.

    Args:
        body: Password and a name for the new token.
        request: Current request.
        db: Database connection (injected).

    Returns:
        The new token, including its secret (shown only this once).

    Raises:
        HTTPException: 429 if throttled, 409 if no admin password is set
            yet, 401 if the password is wrong.
    """
    throttle = get_login_throttle(request)
    key = client_key(request)
    if not throttle.begin_attempt(key):
        raise HTTPException(
            status_code=429,
            detail={
                "detail": "Too many failed login attempts; try again later",
                "error_code": "RATE_LIMITED",
            },
        )
    if not await is_admin_configured(db):
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "No admin password is set; finish setup in the web UI",
                "error_code": "SETUP_REQUIRED",
            },
        )
    if not await check_admin_password(db, body.password):
        logger.warning("Failed admin API login from %s", client_ip(request))
        raise HTTPException(
            status_code=401,
            detail={"detail": "Incorrect password", "error_code": "UNAUTHORIZED"},
        )
    throttle.reset(key)
    token = await create_admin_token(db, body.token_name)
    await db.commit()
    return token


@router.get("/status", response_model=AuthStatus)
async def auth_status(request: Request, principal: AdminAuth) -> AuthStatus:
    """Report how the caller is authenticated (``kidsplay auth status``).

    Args:
        request: Current request.
        principal: Authenticated admin (injected).

    Returns:
        Whether auth is enabled and the method used.
    """
    return AuthStatus(
        auth_enabled=not get_auth_config(request).disabled,
        method=principal.method,
    )


@router.put("/password", status_code=204)
async def change_password(
    body: AdminPasswordChange, request: Request, db: DBConn, principal: AdminAuth
) -> Response:
    """Change the admin password and end the other browser sessions.

    The current password is required even though the caller is logged in, so a
    stolen session or token can't be used to take over the account. Wrong
    guesses count against the same per-client limit as login attempts. The
    caller's own browser session (if any) is kept; API tokens are unaffected
    unless ``revoke_tokens`` is set, which revokes all of them, including the
    one the caller may be using.

    Args:
        body: The current and the new password.
        request: Current request.
        db: Database connection (injected).
        principal: Authenticated admin (injected).

    Returns:
        Empty 204 response.

    Raises:
        HTTPException: 409 if auth is disabled, 429 if throttled, 403 if the
            current password is wrong, 422 if the new one is too short.
    """
    if principal.method == "disabled":
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "Admin authentication is disabled; there is no password",
                "error_code": "AUTH_DISABLED",
            },
        )
    throttle = get_login_throttle(request)
    key = client_key(request)
    if not throttle.begin_attempt(key):
        raise HTTPException(
            status_code=429,
            detail={
                "detail": "Too many failed attempts; try again later",
                "error_code": "RATE_LIMITED",
            },
        )
    if not await check_admin_password(db, body.current_password):
        logger.warning("Wrong current password on change from %s", client_ip(request))
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "Current password is incorrect",
                "error_code": "WRONG_PASSWORD",
            },
        )
    throttle.reset(key)
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "detail": f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
                "error_code": "PASSWORD_TOO_SHORT",
            },
        )
    await change_admin_password(db, body.new_password)
    session_id = request.session.get(SESSION_ID_KEY)
    keep = session_id if principal.method == "session" else None
    await delete_sessions(db, keep_session_id=keep if isinstance(keep, str) else None)
    revoked = await delete_admin_tokens(db) if body.revoke_tokens else 0
    await db.commit()
    logger.info(
        "Admin password changed from %s (API tokens revoked: %d)",
        client_ip(request),
        revoked,
    )
    return Response(status_code=204)


@router.post("/sessions/revoke", response_model=dict[str, int])
async def revoke_sessions(
    request: Request, db: DBConn, _admin: AdminAuth
) -> dict[str, int]:
    """Log out every browser session ("log out everywhere"), this one included.

    API tokens are not affected.

    Args:
        request: Current request.
        db: Database connection (injected).
        _admin: Authenticated admin (injected).

    Returns:
        ``{"revoked": n}``, the number of sessions ended.
    """
    count = await delete_sessions(db)
    await db.commit()
    logger.info("All admin sessions revoked from %s", client_ip(request))
    return {"revoked": count}


@router.get("/tokens", response_model=list[AdminToken])
async def list_tokens(db: DBConn, _admin: AdminAuth) -> list[AdminToken]:
    """List admin API tokens (never includes the secrets).

    Args:
        db: Database connection (injected).
        _admin: Authenticated admin (injected).

    Returns:
        Token metadata, oldest first.
    """
    return await list_admin_tokens(db)


@router.post("/tokens", response_model=AdminTokenCreated, status_code=201)
async def create_token(
    body: AdminTokenCreate, db: DBConn, _admin: AdminAuth
) -> AdminTokenCreated:
    """Create an admin API token.

    Args:
        body: Name for the token.
        db: Database connection (injected).
        _admin: Authenticated admin (injected).

    Returns:
        The new token, including its secret (shown only this once).
    """
    token = await create_admin_token(db, body.name)
    await db.commit()
    return token


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(token_id: uuid.UUID, db: DBConn, _admin: AdminAuth) -> Response:
    """Revoke an admin API token.

    Args:
        token_id: Id of the token to revoke.
        db: Database connection (injected).
        _admin: Authenticated admin (injected).

    Returns:
        Empty 204 response.

    Raises:
        HTTPException: 404 if the token does not exist.
    """
    if not await delete_admin_token(db, token_id):
        raise HTTPException(
            status_code=404,
            detail={"detail": "Token not found", "error_code": "NOT_FOUND"},
        )
    await db.commit()
    return Response(status_code=204)
