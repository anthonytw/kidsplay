"""On-device pairing: a handheld asks to join, a parent approves it.

Flow
----
1. The device generates a short *code* (shown on its screen and in a QR code)
   and a long random *binding secret* that never leaves it except in the
   requests below. ``POST /api/v1/pairing`` registers the code and stores only
   a SHA-256 hash of the secret.
2. The device polls ``POST /api/v1/pairing/poll`` with the code and the secret.
3. A parent approves the code in the web UI (admin session or token, CSRF
   protected): the server creates the device and its API key right then.
4. The next poll that presents the right secret receives the device id and API
   key. The row moves to ``delivered``. The device then saves its config and
   calls ``POST /api/v1/pairing/confirm``, which closes the row for good.
   Until it confirms (at most ``REDELIVER_WINDOW``), the same secret gets the
   same key again, so a lost response or a failed write on the device doesn't
   strand a paired device; after that nothing can read the key through
   pairing again.

Security notes
--------------
* The code only *names* a request for the parent. It is not the credential:
  the API key is released only to the holder of the 256-bit-class binding
  secret, and a code can be live for one request at a time, so nobody can
  squat or steal a code the real device is using.
* Secrets are compared as hashes with ``hmac.compare_digest``. A request for
  an unknown code or with the wrong secret gets the same answer, so the
  endpoint does not reveal which codes exist.
* Nothing here logs a code, a secret or an API key.
"""

import asyncio
import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import aiosqlite

from kidsplay_models import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_MIN_SECRET_LENGTH,
    PAIRING_POLL_INTERVAL_SECONDS,
    Device,
    format_pairing_code,
    normalize_pairing_code,
)
from kidsplay_server.auth import LoginThrottle
from kidsplay_server.database import configure_conn, create_device

logger = logging.getLogger(__name__)

CODE_ALPHABET = PAIRING_CODE_ALPHABET
CODE_LENGTH = PAIRING_CODE_LENGTH

CODE_LIFETIME = timedelta(minutes=10)
"""How long a request can be approved and collected."""

COLLECT_GRACE = timedelta(minutes=2)
"""Minimum time a device has to collect its key after approval."""

REDELIVER_WINDOW = timedelta(minutes=2)
"""How long after delivery the holder of the secret can fetch the key again,
unless the device confirms first."""

RETAIN_EXPIRED = timedelta(hours=1)
"""How long an expired row stays, so the device is told "expired" and not
"unknown"."""

MAX_PENDING = 100
"""Live requests the server holds at once; bounds the table."""

MIN_SECRET_LENGTH = PAIRING_MIN_SECRET_LENGTH
POLL_INTERVAL_SECONDS = PAIRING_POLL_INTERVAL_SECONDS

_DUMMY_HASH = hashlib.sha256(b"kidsplay-pairing-dummy").hexdigest()


class PairingStatus(StrEnum):
    """Stored state of a pairing request."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    DELIVERED = "delivered"


class PollOutcome(StrEnum):
    """What a poll tells the device."""

    PENDING = "pending"
    DELIVERED = "delivered"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
    DENIED = "denied"
    NOT_FOUND = "not_found"


class ApproveOutcome(StrEnum):
    """Result of an admin approval or denial."""

    OK = "ok"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    NOT_PENDING = "not_pending"
    NO_PROFILE = "no_profile"


class CreateOutcome(StrEnum):
    """Result of a device's pairing request."""

    CREATED = "created"
    CODE_IN_USE = "code_in_use"
    TOO_MANY = "too_many"


@dataclass(frozen=True)
class PairingLimits:
    """Per-client throttles for the unauthenticated pairing endpoints.

    Attributes:
        create: Requests to start a pairing, per client per 10 minutes.
        poll: Polls per client per minute (a device polls every 3 seconds).
        poll_failures: Polls with an unknown code or wrong secret, per client
            per 5 minutes.
    """

    create: LoginThrottle
    poll: LoginThrottle
    poll_failures: LoginThrottle

    @classmethod
    def default(cls) -> "PairingLimits":
        """Build the production limits."""
        return cls(
            create=LoginThrottle(max_failures=10, window_seconds=600.0),
            poll=LoginThrottle(max_failures=60, window_seconds=60.0),
            poll_failures=LoginThrottle(max_failures=10, window_seconds=300.0),
        )


@dataclass(frozen=True)
class PairingRequest:
    """A pairing request as an admin sees it (never the secret)."""

    code: str
    status: PairingStatus
    device_name: str
    display_width: int
    display_height: int
    client: str
    created_at: datetime
    expires_at: datetime
    device_id: uuid.UUID | None


@dataclass(frozen=True)
class DeliveredKey:
    """The one-time result of a successful collection."""

    device_id: uuid.UUID
    api_key: str


def utcnow() -> datetime:
    """Return the current UTC time (patched by tests)."""
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


normalize_code = normalize_pairing_code
format_code = format_pairing_code


def hash_secret(secret: str) -> str:
    """Hash a binding secret for storage.

    A fast hash is right here: the secret is a long random value, not a
    password, so there is nothing to brute-force.

    Args:
        secret: The device's binding secret.

    Returns:
        Hex SHA-256 digest.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def _request(row: aiosqlite.Row) -> PairingRequest:
    return PairingRequest(
        code=row["code"],
        status=PairingStatus(row["status"]),
        device_name=row["device_name"],
        display_width=row["display_width"],
        display_height=row["display_height"],
        client=row["client"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        device_id=uuid.UUID(row["device_id"]) if row["device_id"] else None,
    )


async def _purge(conn: aiosqlite.Connection, now: datetime) -> int:
    cursor = await conn.execute(
        "DELETE FROM pairing_requests WHERE expires_at < ?",
        (_iso(now - RETAIN_EXPIRED),),
    )
    return cursor.rowcount


async def purge_expired(conn: aiosqlite.Connection, now: datetime) -> int:
    """Delete requests that expired long enough ago. Commits.

    Called at startup and on a timer, so expired secret hashes don't wait for
    the next pairing to be cleaned up.

    Args:
        conn: Open, configured connection.
        now: Current time.

    Returns:
        How many rows were removed.
    """
    removed = await _purge(conn, now)
    await conn.commit()
    return removed


PURGE_INTERVAL_SECONDS = 600.0
"""How often the server sweeps expired requests out of the table."""


async def run_purge_loop(
    db_path: Path,
    shutdown_event: asyncio.Event,
    interval: float | None = None,
) -> None:
    """Sweep expired pairing requests, now and then every ``interval`` seconds.

    Without this the expired rows (and their secret hashes) would stay until
    the next pairing happened to purge them.

    Args:
        db_path: The server database.
        shutdown_event: Set when the server stops.
        interval: Seconds between sweeps; ``PURGE_INTERVAL_SECONDS`` if None.
    """
    wait = PURGE_INTERVAL_SECONDS if interval is None else interval
    while True:
        try:
            async with aiosqlite.connect(db_path) as conn:
                await configure_conn(conn)
                removed = await purge_expired(conn, utcnow())
            if removed:
                logger.info("Removed %d expired pairing request(s)", removed)
        except Exception:  # a failed sweep must not end the loop
            logger.exception("Could not remove expired pairing requests")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=wait)
        except TimeoutError:
            continue
        return


async def create_request(
    conn: aiosqlite.Connection,
    *,
    code: str,
    secret: str,
    device_name: str,
    display_width: int,
    display_height: int,
    client: str,
    now: datetime,
) -> CreateOutcome:
    """Register a pairing request. Does not commit.

    A code that belongs to a live request is refused, so the real device
    holding it is never displaced. An expired or finished one is replaced.

    Args:
        conn: Open, configured connection.
        code: Normalized code (see ``normalize_code``).
        secret: The device's binding secret.
        device_name: Name the device suggests for itself.
        display_width: Screen width in pixels.
        display_height: Screen height in pixels.
        client: Remote address, shown to the parent.
        now: Current time.

    Returns:
        The outcome.
    """
    await _purge(conn, now)
    async with conn.execute(
        "SELECT expires_at FROM pairing_requests WHERE code = ?", (code,)
    ) as cur:
        existing = await cur.fetchone()
    if existing is not None:
        if datetime.fromisoformat(existing["expires_at"]) > now:
            return CreateOutcome.CODE_IN_USE
        await conn.execute("DELETE FROM pairing_requests WHERE code = ?", (code,))
    async with conn.execute(
        "SELECT COUNT(*) FROM pairing_requests WHERE status = ? AND expires_at > ?",
        (PairingStatus.PENDING.value, _iso(now)),
    ) as cur:
        row = await cur.fetchone()
    if row is not None and row[0] >= MAX_PENDING:
        return CreateOutcome.TOO_MANY
    try:
        await conn.execute(
            """
            INSERT INTO pairing_requests
                (code, secret_hash, status, device_name, display_width,
                 display_height, client, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                hash_secret(secret),
                PairingStatus.PENDING.value,
                device_name,
                display_width,
                display_height,
                client,
                _iso(now),
                _iso(now + CODE_LIFETIME),
            ),
        )
    except aiosqlite.IntegrityError:
        # Another request took the same code between the check and here.
        await conn.rollback()
        return CreateOutcome.CODE_IN_USE
    return CreateOutcome.CREATED


async def list_pending(
    conn: aiosqlite.Connection, now: datetime
) -> list[PairingRequest]:
    """List requests waiting for a parent, oldest first.

    Args:
        conn: Open, configured connection.
        now: Current time; expired requests are left out.

    Returns:
        The pending requests.
    """
    async with conn.execute(
        """
        SELECT * FROM pairing_requests
        WHERE status = ? AND expires_at > ? ORDER BY created_at
        """,
        (PairingStatus.PENDING.value, _iso(now)),
    ) as cur:
        return [_request(row) for row in await cur.fetchall()]


async def poll(
    conn: aiosqlite.Connection, *, code: str, secret: str, now: datetime
) -> tuple[PollOutcome, DeliveredKey | None]:
    """Answer a device's poll, releasing the key to the holder of the secret.

    The key is released when the request is approved and, until the device
    calls ``confirm`` (or ``REDELIVER_WINDOW`` runs out), again to the same
    secret. Commits.

    Args:
        conn: Open, configured connection.
        code: Normalized code.
        secret: The binding secret the device presents.
        now: Current time.

    Returns:
        The outcome and, only for ``DELIVERED``, the key.
    """
    async with conn.execute(
        "SELECT * FROM pairing_requests WHERE code = ?", (code,)
    ) as cur:
        row = await cur.fetchone()
    expected = row["secret_hash"] if row is not None else _DUMMY_HASH
    # Always compare, even for an unknown code, so timing does not tell them
    # apart; then treat "unknown" and "wrong secret" identically.
    matches = hmac.compare_digest(hash_secret(secret).encode(), expected.encode())
    if row is None or not matches:
        return PollOutcome.NOT_FOUND, None
    status = PairingStatus(row["status"])
    if status is PairingStatus.DELIVERED:
        # ``expires_at`` of a delivered row is the end of the re-delivery
        # window (set on delivery, pulled to "now" by ``confirm``).
        if datetime.fromisoformat(row["expires_at"]) <= now:
            return PollOutcome.ALREADY_USED, None
        return await _deliver(conn, row["device_id"])
    if status is PairingStatus.DENIED:
        return PollOutcome.DENIED, None
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return PollOutcome.EXPIRED, None
    if status is PairingStatus.PENDING:
        return PollOutcome.PENDING, None

    # Approved: the status flip is the single-use gate. Only the statement
    # that changes approved -> delivered gets the key.
    cursor = await conn.execute(
        """
        UPDATE pairing_requests SET status = ?, expires_at = ?
        WHERE code = ? AND status = ?
        """,
        (
            PairingStatus.DELIVERED.value,
            _iso(now + REDELIVER_WINDOW),
            code,
            PairingStatus.APPROVED.value,
        ),
    )
    if cursor.rowcount != 1:
        await conn.rollback()
        return PollOutcome.ALREADY_USED, None
    return await _deliver(conn, row["device_id"])


async def _deliver(
    conn: aiosqlite.Connection, device_id: str
) -> tuple[PollOutcome, DeliveredKey | None]:
    """Read the device's key for a poll that has earned it. Commits."""
    async with conn.execute(
        "SELECT api_key FROM devices WHERE id = ?", (device_id,)
    ) as cur:
        device = await cur.fetchone()
    await conn.commit()
    if device is None:
        # Approved, then the parent deleted the device before it collected.
        return PollOutcome.DENIED, None
    return PollOutcome.DELIVERED, DeliveredKey(
        device_id=uuid.UUID(device_id), api_key=device["api_key"]
    )


async def confirm(
    conn: aiosqlite.Connection, *, code: str, secret: str, now: datetime
) -> bool:
    """Close a delivered request: the device has saved its key. Commits.

    After this the key can no longer be fetched through pairing, whatever is
    left of the re-delivery window. Idempotent.

    Args:
        conn: Open, configured connection.
        code: Normalized code.
        secret: The binding secret the device presents.
        now: Current time.

    Returns:
        True if ``code`` and ``secret`` belong together (an unknown code and a
        wrong secret are indistinguishable), whether or not this call changed
        anything.
    """
    async with conn.execute(
        "SELECT * FROM pairing_requests WHERE code = ?", (code,)
    ) as cur:
        row = await cur.fetchone()
    expected = row["secret_hash"] if row is not None else _DUMMY_HASH
    matches = hmac.compare_digest(hash_secret(secret).encode(), expected.encode())
    if row is None or not matches:
        return False
    await conn.execute(
        """
        UPDATE pairing_requests SET expires_at = ?
        WHERE code = ? AND status = ? AND expires_at > ?
        """,
        (_iso(now), code, PairingStatus.DELIVERED.value, _iso(now)),
    )
    await conn.commit()
    return True


async def approve(
    conn: aiosqlite.Connection,
    *,
    code: str,
    profile_id: uuid.UUID,
    name: str | None,
    now: datetime,
) -> tuple[ApproveOutcome, Device | None]:
    """Approve a pending request: create the device and mark it collectable.

    Does not commit.

    Args:
        conn: Open, configured connection.
        code: Normalized code.
        profile_id: Profile the new device is linked to.
        name: Device name; the device's own suggestion if None or blank.
        now: Current time.

    Returns:
        The outcome and the created device (without exposing the key to the
        caller's response).
    """
    async with conn.execute(
        "SELECT * FROM pairing_requests WHERE code = ?", (code,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return ApproveOutcome.NOT_FOUND, None
    if row["status"] != PairingStatus.PENDING.value:
        return ApproveOutcome.NOT_PENDING, None
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at <= now:
        return ApproveOutcome.EXPIRED, None
    async with conn.execute(
        "SELECT 1 FROM profiles WHERE id = ?", (str(profile_id),)
    ) as cur:
        if await cur.fetchone() is None:
            return ApproveOutcome.NO_PROFILE, None

    device = Device(
        name=(name or "").strip() or row["device_name"] or "KidsPlay",
        profile_id=profile_id,
        display_width=row["display_width"],
        display_height=row["display_height"],
    )
    cursor = await conn.execute(
        """
        UPDATE pairing_requests
        SET status = ?, device_id = ?, expires_at = ?
        WHERE code = ? AND status = ?
        """,
        (
            PairingStatus.APPROVED.value,
            str(device.id),
            _iso(max(expires_at, now + COLLECT_GRACE)),
            code,
            PairingStatus.PENDING.value,
        ),
    )
    if cursor.rowcount != 1:
        return ApproveOutcome.NOT_PENDING, None
    await create_device(conn, device)
    return ApproveOutcome.OK, device


async def deny(
    conn: aiosqlite.Connection, *, code: str, now: datetime
) -> ApproveOutcome:
    """Deny a pending request. Does not commit.

    Args:
        conn: Open, configured connection.
        code: Normalized code.
        now: Current time.

    Returns:
        ``OK``, ``NOT_FOUND`` or ``NOT_PENDING`` (already decided or expired).
    """
    cursor = await conn.execute(
        """
        UPDATE pairing_requests SET status = ?
        WHERE code = ? AND status = ? AND expires_at > ?
        """,
        (
            PairingStatus.DENIED.value,
            code,
            PairingStatus.PENDING.value,
            _iso(now),
        ),
    )
    if cursor.rowcount == 1:
        return ApproveOutcome.OK
    async with conn.execute(
        "SELECT 1 FROM pairing_requests WHERE code = ?", (code,)
    ) as cur:
        found = await cur.fetchone() is not None
    return ApproveOutcome.NOT_PENDING if found else ApproveOutcome.NOT_FOUND
