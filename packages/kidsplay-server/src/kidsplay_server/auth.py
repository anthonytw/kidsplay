"""Admin authentication primitives: password, sessions, API tokens, secrets.

The server has a single admin account (it's a family app). Three credentials
are stored, each in its own table:

* ``admin_account`` — one row holding the argon2id hash of the admin password.
* ``admin_sessions`` — browser sessions. The browser holds a random session
  id inside a signed cookie; the table stores only its SHA-256, so a leaked
  database cannot be replayed as a cookie. Deleting the row (logout) revokes
  the session server-side even if the cookie is kept.
* ``admin_tokens`` — API tokens for the CLI and scripts. A token looks like
  ``kpa_<id>_<secret>``; the table stores the id and the SHA-256 of the
  secret, and verification compares hashes with ``hmac.compare_digest``.

Session ids and token secrets are 256-bit random values, so a fast hash is
the right tool for them; only the human-chosen password needs argon2.

Nothing here knows about HTTP. The FastAPI dependencies and routes that use
these functions live in ``kidsplay_server.api.auth`` and
``kidsplay_server.web.auth_routes``.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
import uuid
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import anyio
import anyio.to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from kidsplay_models import PAIRING_CODE_ALPHABET
from kidsplay_models.auth import AdminToken, AdminTokenCreated
from kidsplay_server.proxy import IPNetwork

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 8
"""Shortest admin password the setup page accepts."""

SESSION_LIFETIME = timedelta(days=14)
"""Absolute lifetime of a browser session, enforced server-side."""

TOKEN_PREFIX = "kpa_"
"""Prefix on every admin API token, so a leaked one is easy to recognise."""

_SECRET_KEY_MIN_LENGTH = 32

# argon2id with the library's defaults (RFC 9106 low-memory profile).
_hasher = PasswordHasher()

_HASH_THREADS = 2
"""Password hashes computed at once. Each uses about 64 MiB and a login flood
must not be able to fill a Raspberry Pi's memory with them."""

_hash_limiters: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, anyio.CapacityLimiter
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class AuthConfig:
    """Authentication settings passed to ``create_app``.

    Attributes:
        disabled: Skip admin authentication entirely (``KIDSPLAY_AUTH=disabled``).
            Only for installs behind a proxy that already authenticates.
        admin_password: Pre-seeds the admin password at startup if none is
            set yet (``KIDSPLAY_ADMIN_PASSWORD``). Never overwrites one.
        secret_key_path: File holding the session-signing key. Created with
            mode 0600 on first run. ``None`` puts it next to the database.
        cookie_secure: ``True`` always marks the session cookie ``Secure``,
            ``False`` never does, ``None`` (default) marks it when the request
            arrived over HTTPS, directly or through a trusted proxy.
        trusted_proxies: Networks of the reverse proxies whose
            ``X-Forwarded-For`` / ``X-Forwarded-Proto`` headers are believed
            (``KIDSPLAY_TRUSTED_PROXIES``); see ``kidsplay_server.proxy``.
    """

    disabled: bool = False
    admin_password: str | None = None
    secret_key_path: Path | None = None
    cookie_secure: bool | None = None
    trusted_proxies: tuple[IPNetwork, ...] = ()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_AUTH_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS admin_account (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id_hash     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_tokens (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT
);
"""


async def init_auth_db(conn: aiosqlite.Connection) -> None:
    """Create the admin auth tables if they do not exist.

    Idempotent, like ``database.init_db``.

    Args:
        conn: Open aiosqlite connection.
    """
    await conn.executescript(_CREATE_AUTH_TABLES_SQL)


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Admin password
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with argon2id.

    Args:
        password: Plaintext password.

    Returns:
        Encoded argon2 hash (includes salt and parameters).
    """
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Check a password against an argon2 hash.

    Args:
        password_hash: Encoded hash from ``hash_password``.
        password: Candidate plaintext password.

    Returns:
        True if the password matches; False on mismatch or a malformed hash.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _hash_limiter() -> anyio.CapacityLimiter:
    """Return this event loop's limit on concurrent password hashing."""
    loop = asyncio.get_running_loop()
    limiter = _hash_limiters.get(loop)
    if limiter is None:
        limiter = _hash_limiters[loop] = anyio.CapacityLimiter(_HASH_THREADS)
    return limiter


async def hash_password_async(password: str) -> str:
    """Hash a password on a worker thread, keeping the event loop free.

    Argon2 takes tens of milliseconds and much memory; on the event loop it
    would stall device sync and the log stream for every login attempt.

    Args:
        password: Plaintext password.

    Returns:
        Encoded argon2 hash.
    """
    return await anyio.to_thread.run_sync(
        hash_password, password, limiter=_hash_limiter()
    )


async def verify_password_async(password_hash: str, password: str) -> bool:
    """Check a password against a hash on a worker thread.

    Args:
        password_hash: Encoded hash from ``hash_password``.
        password: Candidate plaintext password.

    Returns:
        True if the password matches; False on mismatch or a malformed hash.
    """
    return await anyio.to_thread.run_sync(
        verify_password, password_hash, password, limiter=_hash_limiter()
    )


async def is_admin_configured(conn: aiosqlite.Connection) -> bool:
    """Return whether the admin password has been set.

    Args:
        conn: Open aiosqlite connection.

    Returns:
        True once first-run setup (or the env pre-seed) has run.
    """
    async with conn.execute("SELECT 1 FROM admin_account WHERE id = 1") as cur:
        return await cur.fetchone() is not None


async def set_initial_admin_password(conn: aiosqlite.Connection, password: str) -> bool:
    """Set the admin password, but only if none is set yet.

    The insert is atomic (``INSERT OR IGNORE`` on the single allowed row), so
    two racing first-run setups cannot both succeed. Does not commit.

    Args:
        conn: Open aiosqlite connection.
        password: Plaintext password to hash and store.

    Returns:
        True if the password was stored; False if one already existed.
    """
    now = _now().isoformat()
    cur = await conn.execute(
        "INSERT OR IGNORE INTO admin_account (id, password_hash, created_at, "
        "updated_at) VALUES (1, ?, ?, ?)",
        (await hash_password_async(password), now, now),
    )
    return cur.rowcount == 1


async def check_admin_password(conn: aiosqlite.Connection, password: str) -> bool:
    """Verify the admin password, upgrading the stored hash if needed.

    If argon2's recommended parameters have changed since the hash was made,
    the hash is recomputed and stored (the caller must commit).

    Args:
        conn: Open aiosqlite connection.
        password: Candidate plaintext password.

    Returns:
        True if an admin password is set and ``password`` matches it.
    """
    async with conn.execute(
        "SELECT password_hash FROM admin_account WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    stored: str = row[0]
    if not await verify_password_async(stored, password):
        return False
    if _hasher.check_needs_rehash(stored):
        await conn.execute(
            "UPDATE admin_account SET password_hash = ?, updated_at = ? WHERE id = 1",
            (await hash_password_async(password), _now().isoformat()),
        )
    return True


async def change_admin_password(conn: aiosqlite.Connection, new_password: str) -> None:
    """Replace the admin password. Does not commit.

    The caller must have checked the current password. Sessions are not
    touched here; see ``delete_sessions``.

    Args:
        conn: Open aiosqlite connection.
        new_password: Plaintext new password.
    """
    await conn.execute(
        "UPDATE admin_account SET password_hash = ?, updated_at = ? WHERE id = 1",
        (await hash_password_async(new_password), _now().isoformat()),
    )


# ---------------------------------------------------------------------------
# Browser sessions
# ---------------------------------------------------------------------------


async def create_session(conn: aiosqlite.Connection) -> str:
    """Start a new admin session and purge expired ones. Does not commit.

    Args:
        conn: Open aiosqlite connection.

    Returns:
        The raw session id to put in the signed cookie. Only its hash is
        stored.
    """
    now = _now()
    await conn.execute(
        "DELETE FROM admin_sessions WHERE expires_at <= ?", (now.isoformat(),)
    )
    session_id = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO admin_sessions (id_hash, created_at, expires_at) VALUES (?, ?, ?)",
        (_sha256(session_id), now.isoformat(), (now + SESSION_LIFETIME).isoformat()),
    )
    return session_id


async def is_session_valid(conn: aiosqlite.Connection, session_id: str) -> bool:
    """Return whether a session id belongs to a live, unexpired session.

    Args:
        conn: Open aiosqlite connection.
        session_id: Raw session id from the cookie.

    Returns:
        True if the session exists and has not expired.
    """
    async with conn.execute(
        "SELECT expires_at FROM admin_sessions WHERE id_hash = ?",
        (_sha256(session_id),),
    ) as cur:
        row = await cur.fetchone()
    return row is not None and datetime.fromisoformat(row[0]) > _now()


async def delete_session(conn: aiosqlite.Connection, session_id: str) -> None:
    """Revoke a session (logout). Does not commit.

    Args:
        conn: Open aiosqlite connection.
        session_id: Raw session id from the cookie.
    """
    await conn.execute(
        "DELETE FROM admin_sessions WHERE id_hash = ?", (_sha256(session_id),)
    )


async def delete_sessions(
    conn: aiosqlite.Connection, *, keep_session_id: str | None = None
) -> int:
    """Revoke every browser session ("log out everywhere"). Does not commit.

    Args:
        conn: Open aiosqlite connection.
        keep_session_id: Raw id of one session to leave alone (the caller's,
            so changing the password doesn't log the admin out of the page
            they are using), or None to end them all.

    Returns:
        How many sessions were ended.
    """
    if keep_session_id is None:
        cur = await conn.execute("DELETE FROM admin_sessions")
    else:
        cur = await conn.execute(
            "DELETE FROM admin_sessions WHERE id_hash != ?", (_sha256(keep_session_id),)
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Admin API tokens
# ---------------------------------------------------------------------------


def _admin_token(row: sqlite3.Row) -> AdminToken:
    return AdminToken(
        id=uuid.UUID(row["id"]),
        name=row["name"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_used_at=(
            datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None
        ),
    )


async def create_admin_token(
    conn: aiosqlite.Connection, name: str
) -> AdminTokenCreated:
    """Create an admin API token. Does not commit.

    Args:
        conn: Open aiosqlite connection.
        name: Human-readable label, e.g. ``"laptop CLI"``.

    Returns:
        The token metadata plus the secret token string. The secret is not
        stored and cannot be retrieved again.
    """
    token_id = uuid.uuid4()
    secret = secrets.token_urlsafe(32)
    created_at = _now()
    await conn.execute(
        "INSERT INTO admin_tokens (id, name, token_hash, created_at) "
        "VALUES (?, ?, ?, ?)",
        (token_id.hex, name, _sha256(secret), created_at.isoformat()),
    )
    return AdminTokenCreated(
        id=token_id,
        name=name,
        created_at=created_at,
        token=f"{TOKEN_PREFIX}{token_id.hex}_{secret}",
    )


async def _matching_token_id(
    conn: aiosqlite.Connection, token: str
) -> uuid.UUID | None:
    """Return the id of the admin token ``token`` belongs to, or None."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    id_hex, sep, secret = token.removeprefix(TOKEN_PREFIX).partition("_")
    if not sep or not secret:
        return None
    try:
        token_id = uuid.UUID(hex=id_hex)
    except ValueError:
        return None
    async with conn.execute(
        "SELECT token_hash FROM admin_tokens WHERE id = ?", (token_id.hex,)
    ) as cur:
        row = await cur.fetchone()
    if row is None or not hmac.compare_digest(_sha256(secret), row[0]):
        return None
    return token_id


async def is_admin_token_valid(conn: aiosqlite.Connection, token: str) -> bool:
    """Check an admin API token without recording a use.

    For re-checking a long-lived connection (the log stream).

    Args:
        conn: Open aiosqlite connection.
        token: Full token string as sent by the client.

    Returns:
        True if the token exists and has not been revoked.
    """
    return await _matching_token_id(conn, token) is not None


async def verify_admin_token(
    conn: aiosqlite.Connection, token: str
) -> uuid.UUID | None:
    """Check an admin API token and record its use. Does not commit.

    The token's id selects the row; the secret's hash is then compared in
    constant time.

    Args:
        conn: Open aiosqlite connection.
        token: Full token string as sent by the client.

    Returns:
        The token id if valid, else None.
    """
    token_id = await _matching_token_id(conn, token)
    if token_id is None:
        return None
    await conn.execute(
        "UPDATE admin_tokens SET last_used_at = ? WHERE id = ?",
        (_now().isoformat(), token_id.hex),
    )
    return token_id


async def list_admin_tokens(conn: aiosqlite.Connection) -> list[AdminToken]:
    """List admin API tokens, oldest first. Secrets are never included.

    Args:
        conn: Open aiosqlite connection.

    Returns:
        Token metadata.
    """
    async with conn.execute(
        "SELECT id, name, created_at, last_used_at FROM admin_tokens "
        "ORDER BY created_at"
    ) as cur:
        rows = await cur.fetchall()
    return [_admin_token(r) for r in rows]


async def delete_admin_token(conn: aiosqlite.Connection, token_id: uuid.UUID) -> bool:
    """Revoke an admin API token. Does not commit.

    Args:
        conn: Open aiosqlite connection.
        token_id: Id of the token to revoke.

    Returns:
        True if a token was deleted; False if the id was unknown.
    """
    cur = await conn.execute("DELETE FROM admin_tokens WHERE id = ?", (token_id.hex,))
    return cur.rowcount == 1


async def delete_admin_tokens(conn: aiosqlite.Connection) -> int:
    """Revoke every admin API token. Does not commit.

    Args:
        conn: Open aiosqlite connection.

    Returns:
        How many tokens were revoked.
    """
    cur = await conn.execute("DELETE FROM admin_tokens")
    return cur.rowcount


# ---------------------------------------------------------------------------
# Session-signing secret
# ---------------------------------------------------------------------------


def load_or_create_secret_key(path: Path) -> str:
    """Return the session-signing key, generating it on first run.

    The key is created with ``O_EXCL`` and mode 0600, so it is never readable
    by other users, even briefly. An existing file with looser permissions is
    tightened to 0600 with a warning.

    Args:
        path: Location of the key file.

    Returns:
        The secret key.

    Raises:
        RuntimeError: If the existing file is too short to be a real key.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        key = secrets.token_hex(32)
        try:
            os.write(fd, key.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        logger.info("Generated session secret key at %s", path)
        return key

    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        logger.warning(
            "Session secret %s had permissions %o; tightening to 600", path, mode
        )
        path.chmod(0o600)
    key = path.read_text().strip()
    if len(key) < _SECRET_KEY_MIN_LENGTH:
        raise RuntimeError(
            f"Session secret {path} is shorter than {_SECRET_KEY_MIN_LENGTH} "
            "characters; delete it to generate a new one"
        )
    return key


# ---------------------------------------------------------------------------
# First-run setup code
# ---------------------------------------------------------------------------

SETUP_CODE_LENGTH = 10
"""Characters in a setup code (about 50 bits, from the pairing alphabet)."""

SETUP_CODE_MAX_FAILURES = 20
"""Wrong guesses, across all clients, after which the code is replaced."""


def _random_setup_code() -> str:
    return "".join(
        secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH)
    )


class SetupCode:
    """The one-time code that lets the first visitor set the admin password.

    On a fresh install ``/setup`` would otherwise belong to whoever reaches it
    first. The server prints this code in its log, where only the person who
    runs the server can read it, and ``/setup`` refuses to set a password
    without it. It lives in memory only: a restart makes and prints a new one,
    and ``clear`` (called once the password exists) makes it worthless.

    Guessing is limited twice: per client by the login throttle, and here
    across all clients, by replacing the code after
    :data:`SETUP_CODE_MAX_FAILURES` wrong guesses in a row.

    Args:
        on_rotate: Called with the new code when guessing forced a new one, so
            the caller can print it again.
    """

    def __init__(self, on_rotate: Callable[[str], None] | None = None) -> None:
        self._code: str | None = _random_setup_code()
        self._failures = 0
        self._on_rotate = on_rotate

    @property
    def code(self) -> str | None:
        """The current code as people type it (``ABCDE-FGHJK``), or None once used."""
        if self._code is None:
            return None
        half = len(self._code) // 2
        return f"{self._code[:half]}-{self._code[half:]}"

    @staticmethod
    def _normalize(text: str) -> str:
        return "".join(text.split()).replace("-", "").upper()

    def verify(self, candidate: str) -> bool:
        """Check a submitted code in constant time.

        A wrong guess is counted; enough of them replace the code. Once
        ``clear`` has been called nothing is accepted.

        Args:
            candidate: What the visitor typed (case, spaces and the dash in
                the middle are ignored).

        Returns:
            True only if the code has not been cleared and ``candidate``
            matches it.
        """
        expected = self._code
        # Always run one comparison of equal-length data, cleared or not.
        target = expected if expected is not None else "-" * SETUP_CODE_LENGTH
        matches = hmac.compare_digest(
            self._normalize(candidate).encode(), target.encode()
        )
        if expected is not None and matches:
            return True
        if expected is not None:
            self._failures += 1
            if self._failures >= SETUP_CODE_MAX_FAILURES:
                self._code = _random_setup_code()
                self._failures = 0
                logger.warning(
                    "Too many wrong setup codes; the setup code was replaced"
                )
                if self._on_rotate is not None and self.code is not None:
                    self._on_rotate(self.code)
        return False

    def clear(self) -> None:
        """Retire the code for good (the admin password is now set)."""
        self._code = None


def setup_code_banner(code: str) -> str:
    """Format the log message that tells the owner the setup code.

    Args:
        code: The code as returned by ``SetupCode.code``.

    Returns:
        A multi-line banner that stands out in ``docker compose logs``.
    """
    return (
        "\n"
        "**********************************************************************\n"
        "*  KidsPlay first-run setup                                          *\n"
        "*                                                                    *\n"
        f"*  Setup code:  {code:<53}*\n"
        "*                                                                    *\n"
        "*  Open the web UI, and enter this code together with the admin      *\n"
        "*  password you choose. It works once and changes on every restart.  *\n"
        "*  To skip this step, set KIDSPLAY_ADMIN_PASSWORD instead.           *\n"
        "**********************************************************************"
    )


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------


class LoginThrottle:
    """In-memory limit on failed password attempts per client.

    After ``max_failures`` failures within ``window_seconds`` a client is
    blocked until the oldest failure ages out. Argon2 already makes each
    guess slow; this bounds how many a single client can make.

    Args:
        max_failures: Failures allowed per window.
        window_seconds: Length of the sliding window.
    """

    def __init__(self, max_failures: int = 10, window_seconds: float = 300.0) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, key: str) -> deque[float]:
        cutoff = time.monotonic() - self._window
        failures = self._failures.get(key, deque())
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if failures:
            self._failures[key] = failures
        else:
            self._failures.pop(key, None)
        return failures

    def begin_attempt(self, key: str) -> bool:
        """Reserve a login attempt for ``key``, or refuse it.

        The attempt is counted as a failure straight away, before the
        password is checked, so concurrent requests cannot all pass the check
        while the slow hash comparisons are in flight. Call ``reset`` after a
        successful login.

        Args:
            key: Client identifier (the remote address).

        Returns:
            True if the attempt may go ahead, False if ``key`` is blocked.
        """
        failures = self._prune(key)
        if len(failures) >= self._max_failures:
            return False
        failures.append(time.monotonic())
        self._failures[key] = failures
        return True

    def release(self, key: str) -> None:
        """Give back one attempt reserved by ``begin_attempt``.

        For attempts that turned out not to be failures. Unlike ``reset`` this
        forgets only that one attempt, so a client cannot wipe out its own
        earlier failures by making one request that succeeds.

        Args:
            key: Client identifier (the remote address).
        """
        failures = self._prune(key)
        if failures:
            failures.pop()
            if not failures:
                self._failures.pop(key, None)

    def is_blocked(self, key: str) -> bool:
        """Return whether ``key`` has used up its failed attempts.

        Args:
            key: Client identifier (the remote address).

        Returns:
            True if further attempts should be refused for now.
        """
        return len(self._prune(key)) >= self._max_failures

    def record_failure(self, key: str) -> None:
        """Count a failed attempt for ``key``.

        Args:
            key: Client identifier (the remote address).
        """
        failures = self._prune(key)
        failures.append(time.monotonic())
        self._failures[key] = failures

    def reset(self, key: str) -> None:
        """Forget ``key``'s failures after a successful login.

        Args:
            key: Client identifier (the remote address).
        """
        self._failures.pop(key, None)
