"""On-device pairing contract, shared by the server and the device.

A device makes up a short ``code`` (for the parent to read or scan) and a long
random ``binding_secret`` (never displayed). See ``docs/PAIRING.md``.
"""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

PAIRING_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
"""Code characters: no 0/O, 1/I or L, which are easy to misread on a small
screen. 31 symbols."""

PAIRING_CODE_LENGTH = 8
"""Characters in a code: 31**8 is about 8.5e11 combinations (39.6 bits)."""

PAIRING_MIN_SECRET_LENGTH = 32
"""Shortest binding secret the server accepts."""

PAIRING_POLL_INTERVAL_SECONDS = 3
"""How often a device polls for approval."""

SERVER_ID_HEADER = "X-KidsPlay-Server-Id"
"""Response header on the sync manifest that names the server (see
:func:`short_server_id`). A device pins the id it paired with and refuses a
server that answers with another."""

_CODE_RE = re.compile(rf"^[{PAIRING_CODE_ALPHABET}]{{{PAIRING_CODE_LENGTH}}}$")


def normalize_pairing_code(raw: str) -> str | None:
    """Canonicalize a typed, scanned or displayed code.

    Args:
        raw: Text such as ``"abcd-2345"``.

    Returns:
        The upper-case code without separators, or None if ``raw`` is not a
        valid code.
    """
    cleaned = re.sub(r"[\s-]", "", raw).upper()
    return cleaned if _CODE_RE.match(cleaned) else None


def format_pairing_code(code: str) -> str:
    """Return ``code`` as people see it: ``ABCD-2345``.

    Args:
        code: A normalized code.

    Returns:
        The code with a dash in the middle.
    """
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


def short_server_id(server_id: str) -> str:
    """Return a server id as people compare it: ``AB12-CD34``.

    The device shows this while pairing and the server shows it on the page
    where a parent approves, so a mismatch (a wrong or look-alike server
    on the network) is visible to a person. A relay to the real server shows the
    real id, so this does not replace HTTPS.

    Args:
        server_id: The server's id, as sent by the server.

    Returns:
        The first eight hex digits, upper-case, with a dash in the middle.
    """
    digits = re.sub(r"[^0-9a-fA-F]", "", server_id)[:8].upper()
    return f"{digits[:4]}-{digits[4:]}" if len(digits) > 4 else digits


class PairingCreate(BaseModel):
    """Body of ``POST /api/v1/pairing``: a device asks to be added.

    Attributes:
        code: Device-generated code (8 characters; dash and case ignored).
        binding_secret: Long random secret only this device knows; the API
            key is released to whoever presents it.
        device_name: Name the device suggests for itself.
        display_width: Screen width in pixels.
        display_height: Screen height in pixels.
    """

    code: str = Field(max_length=32)
    binding_secret: str = Field(
        min_length=PAIRING_MIN_SECRET_LENGTH, max_length=256, repr=False
    )
    device_name: str = Field(default="", max_length=60)
    display_width: int = Field(default=640, ge=100, le=10_000)
    display_height: int = Field(default=480, ge=100, le=10_000)


class PairingCreated(BaseModel):
    """Response of ``POST /api/v1/pairing``.

    ``server_id`` is the server's stable identity (None from an older
    server); see :func:`short_server_id`.
    """

    code: str
    expires_at: datetime
    poll_interval_seconds: int
    server_id: str | None = None


class PairingPoll(BaseModel):
    """Body of ``POST /api/v1/pairing/poll``."""

    code: str = Field(max_length=32)
    binding_secret: str = Field(max_length=256, repr=False)


class PairingPollResult(BaseModel):
    """Response of ``POST /api/v1/pairing/poll``.

    ``status`` is ``pending`` (keep polling) or ``approved``; only an
    ``approved`` result carries the credentials. They can be fetched again
    with the same secret for a short while, until the device confirms with
    ``POST /api/v1/pairing/confirm``. ``server_id`` names the server the key
    belongs to; the device pins it.
    """

    status: str
    device_id: uuid.UUID | None = None
    api_key: str | None = Field(default=None, repr=False)
    server_id: str | None = None
