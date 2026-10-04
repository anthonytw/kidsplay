"""Pairing client: ask a server to add this device, then collect the key.

The device makes up a short *code* (shown on screen and in a QR code, for the
parent) and a long random *binding secret* (never shown). It registers both
with ``POST /api/v1/pairing`` and polls ``POST /api/v1/pairing/poll``; once the
parent approves, the poll returns this device's id and API key, once, and only
to a caller that presents the secret. :func:`write_config` then saves the same
``config.json`` that ``kidsplay device setup`` writes.

:class:`PairingSession` runs the exchange on a background thread so the
pygame loop never blocks; the screen in ``pairing_screen.py`` reads its
:class:`PairingState`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from kidsplay_device.config import DEFAULT_CONFIG_PATH
from kidsplay_models import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PairingCreate,
    PairingPoll,
    PairingPollResult,
    format_pairing_code,
    short_server_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = logging.getLogger(__name__)

#: Keys of ``config.json``, exactly as ``kidsplay device setup`` writes them.
#: Pairing also writes ``server_id`` when the server sent one.
CONFIG_KEYS: tuple[str, ...] = (
    "server_url",
    "device_id",
    "api_key",
    "media_root",
    "db_path",
    "sync_interval_seconds",
)

DEFAULT_MEDIA_ROOT = "~/.kidsplay/media"
DEFAULT_DB_PATH = "~/.kidsplay/db.sqlite"
DEFAULT_SYNC_INTERVAL = 900

_SAVE_ATTEMPTS = 3
_SAVE_RETRY_SECONDS = 1.0
_CONFIRM_TIMEOUT = 3.0
_SECRET_BYTES = 32
_CODE_RETRIES = 5
_REQUEST_TIMEOUT = 10.0
_MAX_BACKOFF = 30.0


class PairingProblem(Enum):
    """Why a pairing attempt stopped, as the screen explains it."""

    UNREACHABLE = auto()  # network / not a KidsPlay server
    EXPIRED = auto()  # code ran out
    USED = auto()  # code already collected
    DECLINED = auto()  # parent said no
    DISABLED = auto()  # pairing is off on the server
    BUSY = auto()  # rate limited, or the server is full
    CANT_SAVE = auto()  # approved, but this player could not write its config
    WRONG_SERVER = auto()  # the server changed identity while pairing
    UNKNOWN = auto()  # the server refused for another reason


class PairingError(Exception):
    """A pairing request failed.

    Args:
        problem: What went wrong, for the screen to explain.
        detail: Technical detail for the log (never shown, never a secret).
        transient: Whether it is worth trying again as it is: a transport
            error (timeout, connection reset), as opposed to an answer from
            the server.
    """

    def __init__(
        self, problem: PairingProblem, detail: str = "", *, transient: bool = False
    ) -> None:
        super().__init__(detail or problem.name)
        self.problem = problem
        self.detail = detail
        self.transient = transient


@dataclass(frozen=True)
class Credentials:
    """What an approved pairing hands over.

    Attributes:
        device_id: The new device's id.
        api_key: Its API key (shown nowhere, only written to ``config.json``).
        server_id: The id of the server that issued it (None from an older
            server); written to ``config.json`` so sync can tell if a
            different server ever answers.
    """

    device_id: str
    api_key: str
    server_id: str | None = None

    def __repr__(self) -> str:
        return f"Credentials(device_id={self.device_id!r}, api_key=<hidden>)"


def generate_code() -> str:
    """Make up a pairing code: 8 characters from the server's alphabet.

    Returns:
        The code without a dash, e.g. ``ABCD2345``.
    """
    return "".join(
        secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH)
    )


def generate_secret() -> str:
    """Make up a binding secret (256 random bits, URL-safe).

    Returns:
        The secret.
    """
    return secrets.token_urlsafe(_SECRET_BYTES)


PAIR_SERVER_FILENAME = "pair-server.txt"
"""Preset pairing server, next to ``config.json`` (``install-kiosk.sh --server``)."""

BOOT_PAIR_SERVER_FILE = Path("/boot/firmware/kidsplay-server.txt")
"""Preset pairing server on the SD card's boot partition, which any computer can
write to after flashing the card."""


def preset_pair_server(paths: tuple[Path, ...]) -> str | None:
    """The server to pair with without typing its address, if one was preset.

    Reads the first of *paths* that exists. Its first line that is not blank
    or a ``#`` comment is the address, in any form the pairing screen accepts
    (``https://kidsplay.example.net``, ``192.168.1.20``). A file that cannot
    be read, or an address that is not one, is logged and ignored: the device
    then shows the usual server picker.

    Args:
        paths: Files to look in, in order.

    Returns:
        The server's base URL, or None if nothing usable is preset.
    """
    for path in paths:
        try:
            # utf-8-sig: a file saved by a Windows editor may start with a BOM.
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Cannot read the pairing server preset %s: %s", path, exc)
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = normalize_server_url(line)
            if url is None:
                logger.warning(
                    "Ignoring the pairing server preset in %s: %r is not an address",
                    path,
                    line,
                )
                return None
            logger.info("Pairing with the preset server %s (from %s)", url, path)
            return url
    return None


def normalize_server_url(text: str) -> str | None:
    """Turn what a person typed into a base URL.

    ``192.168.1.20`` becomes ``http://192.168.1.20:8000`` (the server's usual
    port); an address with a port or a scheme is kept as typed.

    Args:
        text: Address typed on the on-screen keyboard.

    Returns:
        The URL without a trailing slash, or None if ``text`` is empty or has
        characters that cannot be in a host name.
    """
    cleaned = text.strip()
    scheme = ""
    rest = cleaned
    for prefix in ("http://", "https://"):
        if cleaned.lower().startswith(prefix):
            scheme, rest = prefix, cleaned[len(prefix) :]  # scheme is lower-cased
            break
    rest = rest.rstrip("/")
    if not rest or "/" in rest or not all(c.isalnum() or c in ".-:_[]" for c in rest):
        return None
    if not scheme:
        scheme = "http://"
        if ":" not in rest:
            rest += ":8000"
    return scheme + rest


def _problem_for(response: httpx.Response) -> PairingError:
    try:
        error_code = str(response.json().get("error_code", ""))
    except (ValueError, AttributeError):
        error_code = ""
    mapping = {
        "PAIRING_EXPIRED": PairingProblem.EXPIRED,
        # The server forgot the request (a long-expired row, or a reset
        # database): the fix is the same, a fresh code.
        "PAIRING_NOT_FOUND": PairingProblem.EXPIRED,
        "PAIRING_USED": PairingProblem.USED,
        "PAIRING_DENIED": PairingProblem.DECLINED,
        "PAIRING_DISABLED": PairingProblem.DISABLED,
        "RATE_LIMITED": PairingProblem.BUSY,
        "TOO_MANY_PENDING": PairingProblem.BUSY,
    }
    problem = mapping.get(error_code)
    if problem is None:
        problem = (
            PairingProblem.UNREACHABLE
            if response.status_code in (404, 405) and not error_code
            else PairingProblem.UNKNOWN
        )
    return PairingError(problem, f"HTTP {response.status_code} {error_code}".strip())


class PairingClient:
    """The two device-facing calls, as an async client.

    Args:
        server_url: Base URL of the server.
        http_client: Client to use (tests pass one bound to the app);
            a fresh one is made per call if None.
    """

    def __init__(
        self, server_url: str, *, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self._url = server_url.rstrip("/")
        self._client = http_client
        self.server_id: str | None = None
        """The server's id, once ``start`` has seen it."""

    @contextlib.asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(
            base_url=self._url, timeout=_REQUEST_TIMEOUT
        ) as client:
            yield client

    async def _post(self, path: str, payload: dict[str, object]) -> httpx.Response:
        try:
            async with self._http() as client:
                return await client.post(f"{self._url}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise PairingError(
                PairingProblem.UNREACHABLE, type(exc).__name__, transient=True
            ) from exc

    async def start(
        self,
        code: str,
        secret: str,
        *,
        device_name: str = "",
        width: int = 640,
        height: int = 480,
    ) -> bool:
        """Register a code and secret with the server.

        Args:
            code: Code from :func:`generate_code`.
            secret: Secret from :func:`generate_secret`.
            device_name: Name to suggest to the parent.
            width: Screen width in pixels.
            height: Screen height in pixels.

        Returns:
            True if registered; False if the server already has a live request
            with this code (make up another one and try again).

        Raises:
            PairingError: The server is unreachable, refuses pairing, or is
                not a KidsPlay server.
        """
        body = PairingCreate(
            code=code,
            binding_secret=secret,
            device_name=device_name,
            display_width=width,
            display_height=height,
        )
        response = await self._post("/api/v1/pairing", body.model_dump())
        if response.status_code == 201:
            self.server_id = _server_id_of(response)
            return True
        if response.status_code == 409 and _error_code(response) == "CODE_IN_USE":
            return False
        raise _problem_for(response)

    async def poll(self, code: str, secret: str) -> Credentials | None:
        """Ask whether the parent has approved.

        Args:
            code: The registered code.
            secret: The binding secret.

        Returns:
            The credentials once approved (this works once); None while the
            request is still waiting.

        Raises:
            PairingError: Expired, already collected, declined, disabled, or
                the server cannot be reached.
        """
        body = PairingPoll(code=code, binding_secret=secret)
        response = await self._post("/api/v1/pairing/poll", body.model_dump())
        if response.status_code != 200:
            raise _problem_for(response)
        try:
            result = PairingPollResult.model_validate(response.json())
        except ValueError as exc:
            raise PairingError(PairingProblem.UNREACHABLE, "bad response") from exc
        if result.status != "approved":
            return None
        if result.device_id is None or not result.api_key:
            raise PairingError(PairingProblem.UNKNOWN, "approved without credentials")
        if (
            self.server_id is not None
            and result.server_id is not None
            and result.server_id != self.server_id
        ):
            # The server that answers is not the one that took the request.
            raise PairingError(PairingProblem.WRONG_SERVER, "server id changed")
        return Credentials(
            device_id=str(result.device_id),
            api_key=result.api_key,
            server_id=result.server_id or self.server_id,
        )

    async def confirm(self, code: str, secret: str) -> bool:
        """Tell the server the credentials are saved, so it closes the request.

        Best effort: without it the server closes the request by itself after
        a couple of minutes.

        Args:
            code: The registered code.
            secret: The binding secret.

        Returns:
            True if the server acknowledged; False on any failure.
        """
        body = PairingPoll(code=code, binding_secret=secret)
        try:
            response = await self._post("/api/v1/pairing/confirm", body.model_dump())
        except PairingError:
            return False
        return response.status_code == 204


def _server_id_of(response: httpx.Response) -> str | None:
    try:
        value = response.json().get("server_id")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error_code", ""))
    except (ValueError, AttributeError):
        return ""


def config_payload(server_url: str, credentials: Credentials) -> dict[str, object]:
    """Build the ``config.json`` content, the same as ``kidsplay device setup``.

    Args:
        server_url: Base URL the device paired with.
        credentials: The delivered device id and key.

    Returns:
        The six keys in :data:`CONFIG_KEYS`, plus ``server_id`` when the server
        sent one.
    """
    payload: dict[str, object] = {
        "server_url": server_url,
        "device_id": credentials.device_id,
        "api_key": credentials.api_key,
        "media_root": DEFAULT_MEDIA_ROOT,
        "db_path": DEFAULT_DB_PATH,
        "sync_interval_seconds": DEFAULT_SYNC_INTERVAL,
    }
    if credentials.server_id:
        payload["server_id"] = credentials.server_id
    return payload


def write_config(
    path: Path, server_url: str, credentials: Credentials
) -> dict[str, object]:
    """Write ``config.json`` atomically, readable by its owner only.

    The file holds the API key, so it is created with mode 0600 from the start
    (never briefly world-readable) and put in place with a rename, so a power
    cut leaves either no config or a complete one, never half of one.

    Args:
        path: Destination (normally ``~/.kidsplay/config.json``).
        server_url: Base URL the device paired with.
        credentials: The delivered device id and key.

    Returns:
        The payload that was written.
    """
    payload = config_payload(server_url, credentials)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)  # mkstemp already makes it 0600; be explicit
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(payload, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return payload


class Stage(Enum):
    """Where a :class:`PairingSession` is."""

    IDLE = auto()
    CONNECTING = auto()
    WAITING = auto()  # code is on screen, parent has not approved yet
    DONE = auto()
    FAILED = auto()


@dataclass(frozen=True)
class PairingState:
    """A snapshot of a session for the screen to draw.

    Attributes:
        stage: Where the session is.
        server_url: The server being paired with.
        code: The code to show (dash-formatted), once known.
        problem: Why it failed, when ``stage`` is FAILED.
        seconds_left: Seconds until the code expires while WAITING.
        server_id: The server's id as people compare it (``AB12-CD34``), once
            the server has said; the parent's approval page shows the same.
    """

    stage: Stage = Stage.IDLE
    server_url: str = ""
    code: str = ""
    problem: PairingProblem | None = None
    seconds_left: int = 0
    server_id: str = ""


class PairingSession:
    """One pairing attempt, driven on a background thread.

    Args:
        config_path: Where to write ``config.json`` on success.
        device_name: Name to suggest to the parent.
        width: Screen width in pixels.
        height: Screen height in pixels.
        poll_interval: Seconds between polls.
        retry_delay: First wait after a poll fails on the network (doubles up
            to 30 seconds); ``poll_interval`` if None.
        code_lifetime: Seconds the server keeps a code (10 minutes); the
            server decides when it is over, this drives the countdown and how
            long a dead network is retried.
        clock: Monotonic clock (tests pass a fake).
        http_client_factory: Builds the async client for a server URL; the
            default opens a real connection. Tests bind it to the app.
    """

    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG_PATH,
        *,
        device_name: str = "",
        width: int = 640,
        height: int = 480,
        poll_interval: float = 3.0,
        retry_delay: float | None = None,
        code_lifetime: int = 600,
        clock: Callable[[], float] | None = None,
        http_client_factory: Callable[[str], httpx.AsyncClient | None] | None = None,
    ) -> None:
        self._config_path = config_path
        self._device_name = device_name
        self._size = (width, height)
        self._poll_interval = poll_interval
        self._retry_delay = poll_interval if retry_delay is None else retry_delay
        self._lifetime = code_lifetime
        self._clock = clock or time.monotonic
        self._factory = http_client_factory or (lambda url: None)
        self._lock = threading.Lock()
        self._state = PairingState()
        self._cancel = threading.Event()
        self._deadline: float | None = None
        self._thread: threading.Thread | None = None
        self.config: dict[str, object] | None = None

    @property
    def state(self) -> PairingState:
        """The latest snapshot (thread-safe)."""
        with self._lock:
            state = self._state
        if state.stage is Stage.WAITING and self._deadline is not None:
            left = max(0, int(self._deadline - self._clock()))
            return replace(state, problem=None, seconds_left=left)
        return state

    _deadline: float | None = None

    def _set(
        self,
        *,
        stage: Stage | None = None,
        server_url: str | None = None,
        code: str | None = None,
        problem: PairingProblem | None = None,
        server_id: str | None = None,
    ) -> None:
        with self._lock:
            self._state = replace(
                self._state,
                stage=stage if stage is not None else self._state.stage,
                server_url=(
                    server_url if server_url is not None else self._state.server_url
                ),
                code=code if code is not None else self._state.code,
                problem=problem,
                server_id=(
                    server_id if server_id is not None else self._state.server_id
                ),
            )

    def start(self, server_url: str) -> None:
        """Begin pairing with ``server_url`` on a background thread.

        Args:
            server_url: Base URL of the server.
        """
        self.cancel()
        self._cancel = threading.Event()
        self._deadline = None
        self._set(
            stage=Stage.CONNECTING,
            server_url=server_url,
            code="",
            problem=None,
            server_id="",
        )
        self._thread = threading.Thread(
            target=self._run, args=(server_url, self._cancel), daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        """Stop any attempt in progress (its thread ends by itself)."""
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the background thread (tests)."""
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, server_url: str, cancel: threading.Event) -> None:
        try:
            asyncio.run(self._pair(server_url, cancel))
        except PairingError as exc:
            logger.warning("Pairing stopped: %s", exc.problem.name)
            if not cancel.is_set():
                self._set(stage=Stage.FAILED, problem=exc.problem)
        except Exception:
            logger.exception("Pairing failed unexpectedly")
            if not cancel.is_set():
                self._set(stage=Stage.FAILED, problem=PairingProblem.UNKNOWN)

    async def _pair(self, server_url: str, cancel: threading.Event) -> None:
        injected = self._factory(server_url)
        client = PairingClient(server_url, http_client=injected)
        secret = generate_secret()
        code = ""
        for _ in range(_CODE_RETRIES):
            code = generate_code()
            if await client.start(
                code,
                secret,
                device_name=self._device_name,
                width=self._size[0],
                height=self._size[1],
            ):
                break
        else:
            raise PairingError(PairingProblem.BUSY, "no free code")
        self._deadline = self._clock() + self._lifetime
        self._set(
            stage=Stage.WAITING,
            code=format_pairing_code(code),
            server_id=short_server_id(client.server_id or ""),
        )
        backoff = self._retry_delay
        while not cancel.is_set():
            try:
                credentials = await client.poll(code, secret)
            except PairingError as exc:
                # A dropped WiFi packet must not throw the code away while the
                # parent is typing it: retry transport errors, backing off,
                # until the code has expired. An answer from the server
                # (expired, used, declined, busy...) is final.
                if not exc.transient or self._expired():
                    raise
                logger.warning("Poll failed (%s); retrying in %.0fs", exc, backoff)
                await self._sleep(cancel, backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            backoff = self._retry_delay
            if credentials is not None:
                await self._save(client, code, secret, server_url, credentials, cancel)
                self._set(stage=Stage.DONE)
                return
            await self._sleep(cancel, self._poll_interval)

    async def _save(
        self,
        client: PairingClient,
        code: str,
        secret: str,
        server_url: str,
        credentials: Credentials,
        cancel: threading.Event,
    ) -> None:
        """Write ``config.json`` (retrying briefly), then tell the server.

        A failed write (a full or read-only card, a hiccup) is retried a few
        times with the key still in memory; the server would also hand the same
        key out again for a couple of minutes if it were needed. Only when the
        config is safely on disk is the server told to close the request.

        Raises:
            PairingError: The config could not be written.
        """
        for attempt in range(1, _SAVE_ATTEMPTS + 1):
            try:
                self.config = write_config(self._config_path, server_url, credentials)
                break
            except OSError as exc:
                logger.warning(
                    "Could not save %s (attempt %d): %s",
                    self._config_path,
                    attempt,
                    exc,
                )
                if attempt == _SAVE_ATTEMPTS:
                    raise PairingError(
                        PairingProblem.CANT_SAVE, type(exc).__name__
                    ) from exc
                await self._sleep(cancel, _SAVE_RETRY_SECONDS)
        try:
            await asyncio.wait_for(client.confirm(code, secret), _CONFIRM_TIMEOUT)
        except TimeoutError:
            logger.info("Server did not confirm in time; it will close by itself")

    def _expired(self) -> bool:
        return self._deadline is not None and self._clock() >= self._deadline

    async def _sleep(self, cancel: threading.Event, seconds: float) -> None:
        # Sleep in small slices so cancel is honoured promptly.
        remaining = seconds
        while remaining > 0 and not cancel.is_set():
            step = min(0.1, remaining)
            await asyncio.sleep(step)
            remaining -= step
