"""Device configuration.

Loads from a JSON file at a well-known path (~/.kidsplay/config.json by
default, or any path passed explicitly). The config file must exist before
the player starts.

Example config.json::

    {
        "server_url": "http://kidsplay.local:8000",
        "device_id": "550e8400-e29b-41d4-a716-446655440000",
        "api_key": "my-secret-key",
        "media_root": "~/.kidsplay/media",
        "db_path": "~/.kidsplay/db.sqlite",
        "sync_interval_seconds": 900,
        "fullscreen": true,
        "language": "es"
    }

All-in-one mode (server and player on the same machine) adds two keys::

        "sync_transport": "local",
        "server_media_store": "~/.local/share/kidsplay/media"

The manifest is still fetched over HTTP; files are hard-linked (or copied)
from the server's media store instead of downloaded. See
``docs/ALL_IN_ONE.md``.

``language`` is optional. It overrides the language the child's profile sets
on the server (``en`` or ``es``); leave it out to follow the profile.

Other hardware adds up to four more optional keys (see ``docs/HARDWARE.md``)::

        "width": 800,
        "height": 480,
        "input_profile": "generic-gamepad",
        "input_overrides": {"joy_buttons": {"7": "playpause"}}

Left out, they are 640×480 and the ``gpi2`` profile: the reference hardware.
A ``width``/``height`` that is not a whole number, or is below 240×180, is
logged and replaced by 640×480 rather than stopping the player.

Pairing on the device adds ``"server_id"``, the identity of the server that
approved it. The sync then refuses a server that answers with another id, so a
wrong or look-alike server can't feed the device content later. It is
trust-on-first-use: over plain HTTP a relay on the first pairing is not caught
(use HTTPS, see ``docs/PAIRING.md``). Devices without it (set up by hand, or
before this existed) remember the first id they see.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from .input_profiles import DEFAULT_INPUT_PROFILE
from .layout import MIN_HEIGHT, MIN_WIDTH, REFERENCE_HEIGHT, REFERENCE_WIDTH

logger = logging.getLogger(__name__)

SyncTransport = Literal["http", "local"]
SYNC_TRANSPORTS: tuple[str, ...] = get_args(SyncTransport)

_DEFAULT_CONFIG_PATH = Path.home() / ".kidsplay" / "config.json"
DEFAULT_CONFIG_PATH = _DEFAULT_CONFIG_PATH
"""Where the player looks for its config (and where pairing writes it)."""


MAX_SCREEN_SIDE = 8192
"""Largest accepted width or height. Beyond this it is a typo (an extra digit),
and the player would try to allocate a surface of gigabytes."""


def _optional_str(value: object) -> str | None:
    """Return ``value`` if it is a non-empty string, else None."""
    return value if isinstance(value, str) and value.strip() else None


def _screen_size(data: dict) -> tuple[int, int]:
    """Read ``width`` and ``height`` from a config, falling back on bad input.

    A non-numeric value, or a size outside the supported range, must not stop
    the player from starting: like an unknown input profile, it is logged and
    the reference 640×480 is used instead.

    Args:
        data: The parsed config file.

    Returns:
        The screen size in pixels.
    """
    raw = (data.get("width", REFERENCE_WIDTH), data.get("height", REFERENCE_HEIGHT))
    try:
        # bool is an int subclass; ``"width": true`` is a typo, not 1 pixel.
        if any(isinstance(v, bool) for v in raw):
            raise TypeError("not a number")
        width, height = int(raw[0]), int(raw[1])
    except (TypeError, ValueError, OverflowError):
        reason = f"not whole numbers: width={raw[0]!r}, height={raw[1]!r}"
    else:
        if width > MAX_SCREEN_SIDE or height > MAX_SCREEN_SIDE:
            reason = f"{width}x{height} is larger than {MAX_SCREEN_SIDE} per side"
        elif width >= MIN_WIDTH and height >= MIN_HEIGHT:
            return width, height
        else:
            reason = f"{width}x{height} is smaller than {MIN_WIDTH}x{MIN_HEIGHT}"
    logger.warning(
        "Ignoring the screen size in the config (%s); using %dx%d",
        reason,
        REFERENCE_WIDTH,
        REFERENCE_HEIGHT,
    )
    return REFERENCE_WIDTH, REFERENCE_HEIGHT


@dataclass
class DeviceConfig:
    """Configuration for the KidsPlay device player.

    Args:
        server_url: Base URL of the KidsPlay server (no trailing slash).
        device_id: UUID of this device as registered on the server.
        api_key: Bearer token for authenticating sync requests.
        media_root: Local directory where synced media files are stored.
        db_path: Path to the device's local SQLite database file.
        sync_interval_seconds: Seconds between sync attempts after startup.
        fullscreen: Open the pygame window in fullscreen mode.
        sync_transport: How files are fetched: ``"http"`` (default) downloads
            them from the server; ``"local"`` links them from
            ``server_media_store`` (server on this machine).
        server_media_store: The server's media store directory. Required, and
            only used, with ``sync_transport="local"``.
        language: Local override of the profile's language (``"en"``,
            ``"es"``), or None to follow the profile.
        width: Window width in pixels.
        height: Window height in pixels.
        input_profile: Name of the input profile (``gpi2``, ``keyboard``,
            ``generic-gamepad``). An unknown name is logged and treated as
            ``gpi2`` when the player starts.
        input_overrides: Bindings that replace the profile's; see
            ``input_profiles``.
        server_id: The id of the server this device paired with (delivered with
            the key, see ``docs/PAIRING.md``). Sync refuses a server that
            answers with a different one. None for a device set up by hand;
            it then remembers the first id it sees.

    Raises:
        ValueError: If the screen is smaller than 240×180 (``load`` never
            passes one: it falls back to 640×480 first), if
            ``sync_transport`` is unknown, if ``"local"`` has no
            ``server_media_store``, or if ``media_root`` and
            ``server_media_store`` are the same directory or nested in one
            another (the device prunes ``media_root``, so an overlap could
            delete the server's files).
    """

    server_url: str
    device_id: str
    api_key: str
    media_root: Path
    db_path: Path
    sync_interval_seconds: int = field(default=900)
    fullscreen: bool = field(default=False)
    sync_transport: SyncTransport = field(default="http")
    server_media_store: Path | None = field(default=None)

    language: str | None = field(default=None)
    width: int = field(default=REFERENCE_WIDTH)
    height: int = field(default=REFERENCE_HEIGHT)
    input_profile: str = field(default=DEFAULT_INPUT_PROFILE)
    input_overrides: dict[str, object] = field(default_factory=dict)
    server_id: str | None = field(default=None)

    def __post_init__(self) -> None:
        """Validate the screen size and the transport settings."""
        if self.width < MIN_WIDTH or self.height < MIN_HEIGHT:
            raise ValueError(
                f"width x height must be at least {MIN_WIDTH}x{MIN_HEIGHT}, "
                f"not {self.width}x{self.height}"
            )
        if self.sync_transport not in SYNC_TRANSPORTS:
            raise ValueError(
                f"sync_transport must be one of {', '.join(SYNC_TRANSPORTS)}, "
                f"not {self.sync_transport!r}"
            )
        if self.sync_transport != "local":
            return
        if self.server_media_store is None:
            raise ValueError('sync_transport "local" needs server_media_store')
        store = self.server_media_store.expanduser().resolve()
        root = self.media_root.expanduser().resolve()
        if store == root or store in root.parents or root in store.parents:
            raise ValueError(
                f"media_root ({root}) and server_media_store ({store}) must be "
                "separate directories, neither inside the other"
            )

    @property
    def is_local(self) -> bool:
        """Whether files come from a media store on this machine."""
        return self.sync_transport == "local"

    @classmethod
    def load(cls, path: Path | None = None) -> "DeviceConfig":
        """Load configuration from a JSON file.

        Args:
            path: Path to the config file. Defaults to
                ``~/.kidsplay/config.json``.

        Returns:
            Populated ``DeviceConfig`` instance.

        Raises:
            FileNotFoundError: If the config file does not exist.
            KeyError: If a required field is missing from the JSON.
            ValueError: If the transport settings are invalid. (A bad screen
                size is not an error: it is logged and replaced by 640×480.)
        """
        if path is None:
            path = _DEFAULT_CONFIG_PATH
        data: dict = json.loads(path.read_text())
        store = data.get("server_media_store")
        overrides = data.get("input_overrides")
        width, height = _screen_size(data)
        return cls(
            server_url=data["server_url"],
            device_id=data["device_id"],
            api_key=data["api_key"],
            media_root=Path(data["media_root"]).expanduser(),
            db_path=Path(data["db_path"]).expanduser(),
            sync_interval_seconds=int(data.get("sync_interval_seconds", 900)),
            fullscreen=bool(data.get("fullscreen", False)),
            sync_transport=data.get("sync_transport", "http"),
            server_media_store=Path(store).expanduser() if store else None,
            language=_optional_str(data.get("language")),
            width=width,
            height=height,
            input_profile=_optional_str(data.get("input_profile"))
            or DEFAULT_INPUT_PROFILE,
            input_overrides=overrides if isinstance(overrides, dict) else {},
            server_id=_optional_str(data.get("server_id")),
        )

    def save(self, path: Path | None = None) -> None:
        """Save configuration to a JSON file.

        Creates parent directories if they don't exist.

        Args:
            path: Destination path. Defaults to ``~/.kidsplay/config.json``.
        """
        if path is None:
            path = _DEFAULT_CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "server_url": self.server_url,
            "device_id": self.device_id,
            "api_key": self.api_key,
            "media_root": str(self.media_root),
            "db_path": str(self.db_path),
            "sync_interval_seconds": self.sync_interval_seconds,
            "fullscreen": self.fullscreen,
        }
        # Only written when set, so a plain HTTP config file is unchanged.
        if self.is_local:
            payload["sync_transport"] = self.sync_transport
            payload["server_media_store"] = str(self.server_media_store)
        if self.language is not None:
            payload["language"] = self.language
        # Likewise only when they differ from the reference hardware.
        if (self.width, self.height) != (REFERENCE_WIDTH, REFERENCE_HEIGHT):
            payload["width"] = self.width
            payload["height"] = self.height
        if self.input_profile != DEFAULT_INPUT_PROFILE:
            payload["input_profile"] = self.input_profile
        if self.input_overrides:
            payload["input_overrides"] = self.input_overrides
        if self.server_id is not None:
            payload["server_id"] = self.server_id
        path.write_text(json.dumps(payload, indent=2))
