"""On-device pairing endpoints (see ``kidsplay_server.pairing``).

Device-facing and deliberately **unauthenticated** (a device with no
credentials is exactly what is being set up); they are rate-limited per client
instead, and only ever release a key to the holder of the binding secret.

POST /pairing          — a device starts a pairing (code + binding secret)
POST /pairing/poll     — the device polls; the key is delivered (again, to the
                         same secret, until the device confirms)
POST /pairing/confirm  — the device has saved its key; closes the request

Admin-only, like every other management route:

GET  /pairing/requests — requests waiting for approval
POST /pairing/approve  — approve a code for a profile (creates the device)
POST /pairing/deny     — reject a code
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from kidsplay_models import (
    PairingCreate,
    PairingCreated,
    PairingPoll,
    PairingPollResult,
)
from kidsplay_server import pairing
from kidsplay_server.database import get_or_create_server_id
from kidsplay_server.pairing import (
    ApproveOutcome,
    CreateOutcome,
    PairingLimits,
    PollOutcome,
)
from kidsplay_server.server_settings import load_server_settings

from .auth import client_ip, client_key
from .deps import DBConn

device_router = APIRouter(tags=["pairing"])
admin_router = APIRouter(tags=["pairing"])
logger = logging.getLogger(__name__)


def _error(status: int, detail: str, error_code: str) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"detail": detail, "error_code": error_code}
    )


def _limits(request: Request) -> PairingLimits:
    limits: PairingLimits = request.app.state.pairing_limits
    return limits


def _too_many() -> HTTPException:
    return _error(429, "Too many pairing attempts; wait a few minutes", "RATE_LIMITED")


def throttle_create(request: Request) -> None:
    """Count a pairing request against the client's budget before any work."""
    if not _limits(request).create.begin_attempt(client_key(request)):
        raise _too_many()


def throttle_poll(request: Request) -> None:
    """Reserve a poll and a possible wrong-guess before any work is done.

    The wrong-guess budget is spent up front (as the login throttle does), so
    concurrent polls cannot all get past the check while the database lookups
    are in flight. ``poll_pairing`` gives the reservation back for every poll
    that turns out not to be a wrong guess.
    """
    limits = _limits(request)
    key = client_key(request)
    if not limits.poll.begin_attempt(key) or not limits.poll_failures.begin_attempt(
        key
    ):
        raise _too_many()


async def _require_enabled(db: DBConn) -> None:
    if not (await load_server_settings(db)).values.pairing_enabled:
        raise _error(403, "Pairing is turned off on this server", "PAIRING_DISABLED")


@device_router.post(
    "/pairing",
    response_model=PairingCreated,
    status_code=201,
    dependencies=[Depends(throttle_create)],
)
async def create_pairing(
    body: PairingCreate, db: DBConn, request: Request
) -> PairingCreated:
    """Start a pairing from a device.

    Args:
        body: Code, binding secret and screen size.
        db: Database connection (injected).
        request: Current request (for the client address).

    Returns:
        The canonical code and when it expires.

    Raises:
        HTTPException: 403 if pairing is disabled, 422 for a malformed code
            or short secret, 429 when rate limited or the server is full. A
            code that is already in use gets the same 201 as a free one.
    """
    await _require_enabled(db)
    code = pairing.normalize_code(body.code)
    if code is None:
        raise _error(422, "Invalid pairing code", "INVALID_CODE")
    now = pairing.utcnow()
    outcome = await pairing.create_request(
        db,
        code=code,
        secret=body.binding_secret,
        device_name=body.device_name.strip(),
        display_width=body.display_width,
        display_height=body.display_height,
        client=client_ip(request),
        now=now,
    )
    if outcome is CreateOutcome.TOO_MANY:
        raise _error(429, "Too many pairings are waiting", "TOO_MANY_PENDING")
    if outcome is CreateOutcome.CODE_IN_USE:
        # Answer exactly as for a new request, so that probing codes can't tell
        # which are live. Nothing was stored, so the prober's polls (with a
        # secret that isn't the owner's) find nothing; and each probe counts
        # against its wrong-guess budget like any other bad poll. A real
        # device that collides (about 1 in 10^11) sees an "expired" screen and
        # is offered a fresh code.
        _limits(request).poll_failures.record_failure(client_key(request))
        await db.rollback()
    else:
        await db.commit()
        logger.info("Pairing requested from %s", client_ip(request))
    return PairingCreated(
        code=code,
        expires_at=now + pairing.CODE_LIFETIME,
        poll_interval_seconds=pairing.POLL_INTERVAL_SECONDS,
        server_id=await get_or_create_server_id(db),
    )


@device_router.post(
    "/pairing/poll",
    response_model=PairingPollResult,
    dependencies=[Depends(throttle_poll)],
)
async def poll_pairing(
    body: PairingPoll, db: DBConn, request: Request
) -> PairingPollResult:
    """Poll for approval; the API key is returned exactly once.

    Args:
        body: Code and binding secret.
        db: Database connection (injected).
        request: Current request.

    Returns:
        ``pending``, or ``approved`` with ``device_id`` and ``api_key``.

    Raises:
        HTTPException: 403 if pairing is disabled or the request was denied,
            404 for an unknown code or wrong secret, 410 if expired or already
            collected, 429 when rate limited.
    """
    # ``throttle_poll`` already counted this poll as a wrong guess. Only a poll
    # that proves knowledge of the binding secret (or hits a server-side
    # condition) is given back; keeping the count for wrong guesses avoids a
    # second bookkeeping step, and releasing just this one attempt (rather than
    # resetting) stops a client erasing earlier wrong guesses by polling a
    # request of its own.
    failures = _limits(request).poll_failures
    key = client_key(request)
    try:
        await _require_enabled(db)
    except HTTPException:
        failures.release(key)
        raise
    code = pairing.normalize_code(body.code)
    if code is None:
        raise _error(404, "Pairing request not found", "PAIRING_NOT_FOUND")
    outcome, delivered = await pairing.poll(
        db, code=code, secret=body.binding_secret, now=pairing.utcnow()
    )
    if outcome is PollOutcome.NOT_FOUND:
        raise _error(404, "Pairing request not found", "PAIRING_NOT_FOUND")
    failures.release(key)
    if outcome is PollOutcome.PENDING:
        return PairingPollResult(status="pending")
    if outcome is PollOutcome.DELIVERED and delivered is not None:
        logger.info("Pairing completed for device %s", delivered.device_id)
        return PairingPollResult(
            status="approved",
            device_id=delivered.device_id,
            api_key=delivered.api_key,
            server_id=await get_or_create_server_id(db),
        )
    if outcome is PollOutcome.EXPIRED:
        raise _error(410, "This code has expired", "PAIRING_EXPIRED")
    if outcome is PollOutcome.ALREADY_USED:
        raise _error(410, "This code was already used", "PAIRING_USED")
    raise _error(403, "Pairing was declined", "PAIRING_DENIED")


@device_router.post(
    "/pairing/confirm",
    status_code=204,
    dependencies=[Depends(throttle_poll)],
)
async def confirm_pairing(body: PairingPoll, db: DBConn, request: Request) -> None:
    """Tell the server the device has saved its key, closing the request.

    Optional: without it the key stays fetchable with the binding secret for
    the short re-delivery window, then closes by itself.

    Args:
        body: Code and binding secret.
        db: Database connection (injected).
        request: Current request.

    Raises:
        HTTPException: 404 for an unknown code or wrong secret, 429 when rate
            limited.
    """
    # ``throttle_poll`` reserved a wrong-guess slot for this request and it is
    # deliberately NOT given back, even when the secret is right: refunding here
    # would let a client that paired with its own secret hand back wrong
    # guesses made elsewhere. A device confirms once, so the cost is one slot.
    code = pairing.normalize_code(body.code)
    if code is None or not await pairing.confirm(
        db, code=code, secret=body.binding_secret, now=pairing.utcnow()
    ):
        raise _error(404, "Pairing request not found", "PAIRING_NOT_FOUND")


class PendingPairing(BaseModel):
    """A pairing request waiting for approval."""

    code: str
    device_name: str
    display_width: int
    display_height: int
    client: str
    created_at: datetime
    expires_at: datetime


class PairingApprove(BaseModel):
    """Body of ``POST /pairing/approve``."""

    code: str = Field(max_length=32)
    profile_id: uuid.UUID
    name: str | None = Field(default=None, max_length=60)


class PairingDeny(BaseModel):
    """Body of ``POST /pairing/deny``."""

    code: str = Field(max_length=32)


class PairingApproved(BaseModel):
    """Response of ``POST /pairing/approve`` (never includes the API key)."""

    device_id: uuid.UUID
    name: str
    profile_id: uuid.UUID


@admin_router.get("/pairing/requests", response_model=list[PendingPairing])
async def list_pairing_requests(db: DBConn) -> list[PendingPairing]:
    """List pairing requests waiting for approval.

    Args:
        db: Database connection (injected).

    Returns:
        Pending, unexpired requests, oldest first.
    """
    return [
        PendingPairing(
            code=r.code,
            device_name=r.device_name,
            display_width=r.display_width,
            display_height=r.display_height,
            client=r.client,
            created_at=r.created_at,
            expires_at=r.expires_at,
        )
        for r in await pairing.list_pending(db, pairing.utcnow())
    ]


_APPROVE_ERRORS: dict[ApproveOutcome, tuple[int, str, str]] = {
    ApproveOutcome.NOT_FOUND: (
        404,
        "No pairing request with that code",
        "PAIRING_NOT_FOUND",
    ),
    ApproveOutcome.EXPIRED: (410, "That code has expired", "PAIRING_EXPIRED"),
    ApproveOutcome.NOT_PENDING: (
        409,
        "That request was already handled",
        "PAIRING_NOT_PENDING",
    ),
    ApproveOutcome.NO_PROFILE: (404, "Profile not found", "NOT_FOUND"),
}


@admin_router.post("/pairing/approve", response_model=PairingApproved)
async def approve_pairing(body: PairingApprove, db: DBConn) -> PairingApproved:
    """Approve a pairing request, creating the device.

    The API key is not returned here: it goes to the device, once.

    Args:
        body: The code, the profile to link, and an optional device name.
        db: Database connection (injected).

    Returns:
        The created device's id, name and profile.

    Raises:
        HTTPException: 403 if pairing is disabled; 404, 409 or 410 if the code
            is unknown, already handled or expired, or the profile is missing.
    """
    await _require_enabled(db)
    code = pairing.normalize_code(body.code)
    if code is None:
        raise _error(422, "Invalid pairing code", "INVALID_CODE")
    outcome, device = await pairing.approve(
        db,
        code=code,
        profile_id=body.profile_id,
        name=body.name,
        now=pairing.utcnow(),
    )
    if outcome is not ApproveOutcome.OK or device is None:
        raise _error(*_APPROVE_ERRORS[outcome])
    await db.commit()
    logger.info("Approved pairing for device %s", device.id)
    return PairingApproved(
        device_id=device.id, name=device.name, profile_id=device.profile_id
    )


@admin_router.post("/pairing/deny", status_code=204)
async def deny_pairing(body: PairingDeny, db: DBConn) -> None:
    """Reject a pending pairing request.

    Args:
        body: The code.
        db: Database connection (injected).

    Raises:
        HTTPException: 404 if unknown, 409 if already handled or expired.
    """
    code = pairing.normalize_code(body.code)
    if code is None:
        raise _error(422, "Invalid pairing code", "INVALID_CODE")
    outcome = await pairing.deny(db, code=code, now=pairing.utcnow())
    if outcome is not ApproveOutcome.OK:
        raise _error(*_APPROVE_ERRORS[outcome])
    await db.commit()
