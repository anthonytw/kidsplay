"""Server configuration loaded from environment variables.

All settings have sensible defaults for development on a local machine.
Override via environment variables for production.

Environment variables
---------------------
KIDSPLAY_DB_PATH
    Absolute path to the SQLite database file.
    Default: ``~/.local/share/kidsplay/db.sqlite``

KIDSPLAY_MEDIA_STORE
    Root directory for the content-addressed media store.
    Default: ``~/.local/share/kidsplay/media``

KIDSPLAY_LOG_FILE
    Path to the rotating log file.  Logs are written at DEBUG level with
    daily rotation and 7-day retention.  Set to an empty string to disable
    file logging entirely.
    Default: ``~/.local/share/kidsplay/server.log``

KIDSPLAY_SOURCE_URL
    Where the web UI's "Source code" footer link points (AGPL-3.0 section 13:
    users of a modified server must be offered its source). Set it to your own
    repository if you run a modified version. Only ``http(s)://`` URLs are used.
    Default: ``https://github.com/anthonytw/kidsplay``

KIDSPLAY_AUTH
    ``enabled`` (default) or ``disabled``.  ``disabled`` turns off admin
    authentication for the web UI and management API.  Only use it behind a
    reverse proxy that already authenticates; the server logs a warning at
    startup.  Device sync always uses per-device API keys.

KIDSPLAY_ADMIN_PASSWORD
    Pre-seeds the admin password for headless/Docker installs.  Only used if
    no admin password is set yet; it never overwrites one.  When unset, the
    web UI shows a first-run setup page instead.

KIDSPLAY_SECRET_KEY_FILE
    File holding the key that signs session cookies.  Generated with mode
    0600 on first run.
    Default: ``session_secret.key`` next to ``KIDSPLAY_DB_PATH``

KIDSPLAY_COOKIE_SECURE
    Overrides when the session cookie is marked ``Secure`` (sent over HTTPS
    only).  ``1`` always marks it, ``0`` never does.  Unset (default), it is
    marked whenever the request arrived over HTTPS: directly, or through a
    trusted proxy that sends ``X-Forwarded-Proto: https``.

KIDSPLAY_TRUSTED_PROXIES
    Comma-separated IP addresses and CIDR networks of the reverse proxies in
    front of the server (e.g. ``172.18.0.1,10.0.0.0/8``).  Only requests
    from these peers have ``X-Forwarded-For`` (client address, used by the
    login and pairing throttles and the logs) and ``X-Forwarded-Proto``
    believed.  Wildcards are refused.  Default: none.  Start uvicorn with
    ``--no-proxy-headers`` so only this setting decides.  See
    ``docs/DEVELOPMENT.md``.

KIDSPLAY_LOUDNORM, KIDSPLAY_LOUDNORM_LIMITING, KIDSPLAY_LOUDNESS_*
    Loudness normalization.  Read by ``kidsplay_server.server_settings``
    (targets can also be changed on the web settings page); see
    ``docs/LOUDNESS.md``.

KIDSPLAY_PROCESSING_NICE
    Niceness (0-19) for ffmpeg subprocesses.  ``0`` (default) leaves the
    priority alone; all-in-one installs set ``10`` so an import does not
    stutter the player sharing the CPU.

KIDSPLAY_PROCESSING_JOBS
    Most ffmpeg subprocesses allowed at once across the whole server, ``0``
    for no limit (default).  All-in-one installs set ``1``.  See
    ``docs/ALL_IN_ONE.md``.

Importer plugins read their own variables; for example the YouTube plugin
reads ``KIDSPLAY_YT_COOKIES`` (see ``docs/IMPORTERS.md``).

Example (production)::

    export KIDSPLAY_DB_PATH=/srv/kidsplay/db.sqlite
    export KIDSPLAY_MEDIA_STORE=/srv/kidsplay/media
    export KIDSPLAY_LOG_FILE=/srv/kidsplay/server.log
    uv run uvicorn kidsplay_server.api.app:create_app_from_env --factory \\
        --no-proxy-headers
"""

import os
from pathlib import Path

from kidsplay_server.auth import AuthConfig
from kidsplay_server.processing.resources import ResourceLimits
from kidsplay_server.proxy import parse_trusted_proxies

_DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "kidsplay"


class Settings:
    """Runtime configuration for the KidsPlay server.

    Reads from environment variables with sensible local-dev defaults.
    Instantiate once at import time via the module-level ``settings`` singleton.
    """

    def __init__(self) -> None:
        self.db_path: Path = Path(
            os.environ.get("KIDSPLAY_DB_PATH", str(_DEFAULT_DATA_DIR / "db.sqlite"))
        )
        self.media_store_root: Path = Path(
            os.environ.get("KIDSPLAY_MEDIA_STORE", str(_DEFAULT_DATA_DIR / "media"))
        )
        _log = os.environ.get(
            "KIDSPLAY_LOG_FILE", str(_DEFAULT_DATA_DIR / "server.log")
        )
        self.log_file_path: Path | None = Path(_log) if _log else None

        self.auth: AuthConfig = _auth_config_from_env()
        self.limits: ResourceLimits = _limits_from_env()


def _auth_config_from_env() -> AuthConfig:
    """Build the ``AuthConfig`` from ``KIDSPLAY_AUTH`` and related variables.

    Returns:
        Authentication settings.

    Raises:
        ValueError: If ``KIDSPLAY_AUTH`` is neither ``enabled`` nor
            ``disabled``.  A typo must not silently leave auth on or off.
    """
    mode = os.environ.get("KIDSPLAY_AUTH", "").strip().lower() or "enabled"
    if mode not in ("enabled", "disabled"):
        raise ValueError(f"KIDSPLAY_AUTH must be 'enabled' or 'disabled', not {mode!r}")
    secret_file = os.environ.get("KIDSPLAY_SECRET_KEY_FILE", "")
    return AuthConfig(
        disabled=mode == "disabled",
        admin_password=os.environ.get("KIDSPLAY_ADMIN_PASSWORD") or None,
        secret_key_path=Path(secret_file) if secret_file else None,
        cookie_secure=_cookie_secure_from_env(),
        trusted_proxies=parse_trusted_proxies(
            os.environ.get("KIDSPLAY_TRUSTED_PROXIES", "")
        ),
    )


def _cookie_secure_from_env() -> bool | None:
    """Read ``KIDSPLAY_COOKIE_SECURE``: ``1`` forces, ``0`` forbids, else auto.

    Returns:
        The override, or None to mark the cookie ``Secure`` on HTTPS requests.

    Raises:
        ValueError: If the value is not a yes/no spelling or empty.  A typo must
            not silently leave the cookie insecure.
    """
    raw = os.environ.get("KIDSPLAY_COOKIE_SECURE", "").strip().lower()
    if raw == "":
        return None
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"KIDSPLAY_COOKIE_SECURE must be 1 or 0, not {raw!r}")


def _int_from_env(name: str, default: int) -> int:
    """Read an integer environment variable.

    Args:
        name: Variable name.
        default: Value when the variable is unset or empty.

    Returns:
        The value, or ``default``.

    Raises:
        ValueError: If the variable is set but not a whole number.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from None


def _limits_from_env() -> ResourceLimits:
    """Build the ``ResourceLimits`` from the ``KIDSPLAY_PROCESSING_*`` variables.

    Returns:
        The processing resource limits.

    Raises:
        ValueError: If a value is not a whole number or out of range.
    """
    return ResourceLimits(
        nice=_int_from_env("KIDSPLAY_PROCESSING_NICE", 0),
        max_jobs=_int_from_env("KIDSPLAY_PROCESSING_JOBS", 0),
    )


settings = Settings()


DEFAULT_SOURCE_URL = "https://github.com/anthonytw/kidsplay"
"""The upstream repository, linked from the web UI by default."""


def source_url() -> str:
    """The URL of this server's source code, for the web UI's footer link.

    ``KIDSPLAY_SOURCE_URL`` when it is an ``http(s)`` URL, else the upstream
    repository: anything else (a typo, a ``javascript:`` URL) is ignored rather
    than rendered as a link.

    Returns:
        The source-code URL.
    """
    url = os.environ.get("KIDSPLAY_SOURCE_URL", "").strip()
    if url.lower().startswith(("https://", "http://")):
        return url
    return DEFAULT_SOURCE_URL
