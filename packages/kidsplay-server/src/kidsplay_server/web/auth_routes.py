"""Web UI routes for admin login, logout, first-run setup and API tokens.

Routes
------
GET  /setup   — first-run page to set the admin password (only while unset).
POST /setup   — set the admin password and log in.
GET  /login   — login form.
POST /login   — check the password and start a session.
POST /logout  — end the session.
GET  /tokens  — list, create and revoke admin API tokens (admin only).

Every form post here checks the session's CSRF token from the ``csrf_token``
field, including login and setup (to stop login CSRF).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kidsplay_models.auth import MAX_PASSWORD_LENGTH
from kidsplay_server.api.auth import (
    CSRF_KEY,
    SESSION_ID_KEY,
    authenticate_admin,
    client_ip,
    client_key,
    csrf_token_matches,
    get_auth_config,
    get_login_throttle,
    get_setup_code,
    require_admin_page,
    safe_next_url,
)
from kidsplay_server.api.deps import DBConn
from kidsplay_server.auth import (
    MIN_PASSWORD_LENGTH,
    check_admin_password,
    create_session,
    delete_session,
    is_admin_configured,
    list_admin_tokens,
    set_initial_admin_password,
)
from kidsplay_server.i18n import translate

from .routes import templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"], include_in_schema=False)

PasswordField = Annotated[str, Form(max_length=MAX_PASSWORD_LENGTH)]
CsrfField = Annotated[str, Form()]


def _csrf_rejected() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"detail": "Missing or invalid CSRF token", "error_code": "CSRF_FAILED"},
    )


async def _start_session(request: Request, db: DBConn) -> None:
    """Replace the cookie session with a fresh logged-in one.

    Clearing first gives a new session id and CSRF token, so a session
    fixed by an attacker before login is never promoted to admin.
    """
    request.session.clear()
    request.session[SESSION_ID_KEY] = await create_session(db)
    await db.commit()


def _render(
    request: Request,
    template: str,
    context: dict[str, object],
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {"public_page": True, **context},
        status_code=status_code,
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: DBConn) -> Response:
    """Show the first-run setup form, or go to login if already set up.

    Args:
        request: Current request.
        db: Database connection (injected).

    Returns:
        The setup page, or a redirect to ``/login``.
    """
    if await is_admin_configured(db):
        return RedirectResponse("/login", status_code=303)
    return _render(request, "setup.html", {"min_password_length": MIN_PASSWORD_LENGTH})


def _setup_form_error(request: Request, error: str, status_code: int) -> HTMLResponse:
    return _render(
        request,
        "setup.html",
        {"error": error, "min_password_length": MIN_PASSWORD_LENGTH},
        status_code=status_code,
    )


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    db: DBConn,
    password: PasswordField,
    password_confirm: PasswordField,
    csrf_token: CsrfField = "",
    setup_code: Annotated[str, Form(max_length=64)] = "",
) -> Response:
    """Set the admin password on first run and log the admin in.

    Needs the one-time setup code the server printed in its log, so the first
    person to reach the page can't claim the account unless they can also read
    the log. Wrong codes are throttled per client (like login attempts).

    Args:
        request: Current request.
        db: Database connection (injected).
        password: New admin password.
        password_confirm: Must equal ``password``.
        csrf_token: Session CSRF token from the form.
        setup_code: The code from the server log.

    Returns:
        A redirect to the dashboard, the form again with an error (403 for a
        wrong code, 429 when throttled, 400 for a bad password), or a redirect
        to ``/login`` if a password was already set.

    Raises:
        HTTPException: 403 if the CSRF token is missing or wrong.
    """
    if not csrf_token_matches(request, csrf_token):
        raise _csrf_rejected()
    if await is_admin_configured(db):
        return RedirectResponse("/login", status_code=303)

    throttle = get_login_throttle(request)
    key = client_key(request)
    if not throttle.begin_attempt(key):
        return _setup_form_error(
            request,
            translate(request, "Too many failed attempts. Try again later."),
            429,
        )
    if not get_setup_code(request).verify(setup_code):
        logger.warning("Wrong first-run setup code from %s", client_ip(request))
        return _setup_form_error(
            request,
            translate(
                request,
                "Incorrect setup code. Find it in the server log, for example "
                "with: docker compose logs kidsplay-server",
            ),
            403,
        )
    # The code is right: this attempt was not a guess, so give it back.
    throttle.release(key)

    error = None
    if len(password) < MIN_PASSWORD_LENGTH:
        error = translate(
            request,
            "Password must be at least {min_length} characters.",
            min_length=MIN_PASSWORD_LENGTH,
        )
    elif password != password_confirm:
        error = translate(request, "Passwords do not match.")
    if error:
        return _setup_form_error(request, error, 400)

    if not await set_initial_admin_password(db, password):
        # Lost a race with another setup request; that one wins.
        return RedirectResponse("/login", status_code=303)
    await db.commit()
    get_setup_code(request).clear()
    logger.info("Admin password set via first-run setup")
    await _start_session(request, db)
    return RedirectResponse("/", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: DBConn, next: str = "/") -> Response:
    """Show the login form.

    Redirects to ``/setup`` if no admin password exists yet, and straight to
    ``next`` if already logged in (or auth is disabled).

    Args:
        request: Current request.
        db: Database connection (injected).
        next: Where to go after logging in.

    Returns:
        The login page or a redirect.
    """
    next_url = safe_next_url(next)
    if get_auth_config(request).disabled:
        return RedirectResponse(next_url, status_code=303)
    if not await is_admin_configured(db):
        return RedirectResponse("/setup", status_code=303)
    if await authenticate_admin(request, db, None) is not None:
        return RedirectResponse(next_url, status_code=303)
    return _render(request, "login.html", {"next": next_url})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    db: DBConn,
    password: PasswordField,
    csrf_token: CsrfField = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    """Check the admin password and start a session.

    Args:
        request: Current request.
        db: Database connection (injected).
        password: Admin password.
        csrf_token: Session CSRF token from the form.
        next: Where to go after logging in.

    Returns:
        A redirect to ``next`` on success; the form with an error (401) on a
        wrong password, or 429 when throttled.

    Raises:
        HTTPException: 403 if the CSRF token is missing or wrong.
    """
    if not csrf_token_matches(request, csrf_token):
        raise _csrf_rejected()
    next_url = safe_next_url(next)
    throttle = get_login_throttle(request)
    key = client_key(request)
    if not throttle.begin_attempt(key):
        return _render(
            request,
            "login.html",
            {
                "next": next_url,
                "error": translate(
                    request, "Too many failed attempts. Try again later."
                ),
            },
            status_code=429,
        )
    if not await check_admin_password(db, password):
        logger.warning("Failed admin login from %s", client_ip(request))
        return _render(
            request,
            "login.html",
            {"next": next_url, "error": translate(request, "Incorrect password.")},
            status_code=401,
        )
    throttle.reset(key)
    await _start_session(request, db)
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout")
async def logout(
    request: Request, db: DBConn, csrf_token: CsrfField = ""
) -> RedirectResponse:
    """End the admin session, revoking it server-side.

    Args:
        request: Current request.
        db: Database connection (injected).
        csrf_token: Session CSRF token from the form.

    Returns:
        A redirect to ``/login``.

    Raises:
        HTTPException: 403 if the CSRF token is missing or wrong.
    """
    if not csrf_token_matches(request, csrf_token):
        raise _csrf_rejected()
    session_id = request.session.get(SESSION_ID_KEY)
    if isinstance(session_id, str):
        await delete_session(db, session_id)
        await db.commit()
    request.session.pop(SESSION_ID_KEY, None)
    request.session.pop(CSRF_KEY, None)
    return RedirectResponse("/login", status_code=303)


@router.get(
    "/tokens",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_page)],
)
async def tokens_page(request: Request, db: DBConn) -> HTMLResponse:
    """Render the admin API token management page.

    Args:
        request: Current request.
        db: Database connection (injected).

    Returns:
        Rendered ``tokens.html``.
    """
    tokens = await list_admin_tokens(db)
    return templates.TemplateResponse(
        request, "tokens.html", {"active_page": "tokens", "tokens": tokens}
    )
