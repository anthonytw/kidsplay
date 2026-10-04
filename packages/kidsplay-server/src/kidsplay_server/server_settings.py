"""Server settings that can be changed at runtime from the web UI.

Each setting has a default, can be saved from the settings page (stored in
the ``server_settings`` table), and can be pinned by an environment
variable. A set environment variable always wins: the page shows the
setting as env-locked and refuses to change it.

Environment variables
---------------------
KIDSPLAY_SYNC_INTERVAL_SECONDS
    Seconds between device sync attempts, sent to devices in the manifest.
    Only a value that was saved or pinned is sent; until then each device
    uses ``sync_interval_seconds`` from its own ``config.json``.
    Default: 900 (15 minutes), which is also the device default.

KIDSPLAY_WEBP_QUALITY
    WebP quality (1-100) for thumbnails and photos processed from now on.
    Files already in the media store are never re-encoded.
    Default: 85.

KIDSPLAY_PAIRING_ENABLED
    Whether handhelds may pair themselves with a code (``true`` or
    ``false``). Default: true.

KIDSPLAY_LOUDNESS_TARGET_LUFS
    Integrated-loudness target in LUFS (-70 to -5) for audio processed from
    now on. Default: -16. Items already normalized keep their target until
    the library is normalized again (``docs/LOUDNESS.md``).

KIDSPLAY_LOUDNESS_TARGET_LUFS_MUSIC, KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIOBOOK
    Per-media-type targets. Unset means the overall target.

The remaining loudness variables are deployment switches, not runtime
settings: they are read from the environment only, each time a file is
normalized, and are not on the settings page.

KIDSPLAY_LOUDNORM
    ``enabled`` (default) or ``disabled``: normalize audio at ingest.

KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP
    True-peak ceiling in dBTP, -9 to 0. Default: -1.5.

KIDSPLAY_LOUDNORM_LIMITING
    ``allowed`` (default) or ``never``: whether ffmpeg may limit peaks.

Environment values are read when settings are resolved, not at import, and
are validated like values saved from the page: an invalid one raises
``ValueError`` rather than silently falling back.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kidsplay_server.database import (
    delete_server_setting_value,
    get_server_setting_values,
    set_server_setting_value,
)
from kidsplay_server.i18n import N_
from kidsplay_server.processing.audio import (
    DEFAULT_TARGET_LUFS,
    DEFAULT_TRUE_PEAK_DBTP,
    MAX_TARGET_LUFS,
    MIN_TARGET_LUFS,
    LoudnessConfig,
)

logger = logging.getLogger(__name__)


class ServerSettings(BaseModel):
    """Effective runtime settings of the server.

    Attributes:
        sync_interval_seconds: Seconds between device sync attempts (sent to
            devices only once saved or pinned).
        webp_quality: WebP encode quality for new thumbnails and photos.
        pairing_enabled: Whether devices may request pairing (see
            ``kidsplay_server.pairing``).
        loudness_target_lufs: Loudness target for audio without its own.
        loudness_target_lufs_music: Target for music, or ``None`` for
            ``loudness_target_lufs``.
        loudness_target_lufs_audiobook: Target for audiobooks, or ``None``
            for ``loudness_target_lufs``.
    """

    sync_interval_seconds: int = Field(
        default=900,
        ge=60,
        le=86_400,
        description=N_(
            "Seconds between device sync attempts (60 to 86400). Devices use "
            "their own setting until you save one here."
        ),
    )
    webp_quality: int = Field(
        default=85,
        ge=1,
        le=100,
        description=N_(
            "WebP quality (1-100) for thumbnails and photos processed from now on."
        ),
    )
    pairing_enabled: bool = Field(
        default=True,
        description=N_(
            "Let handhelds pair themselves with a code. Turn off once your "
            "devices are set up."
        ),
    )
    loudness_target_lufs: float = Field(
        default=DEFAULT_TARGET_LUFS,
        ge=MIN_TARGET_LUFS,
        le=MAX_TARGET_LUFS,
        description=N_(
            "Loudness target in LUFS (-70 to -5) for music and audiobooks "
            "without their own. Applies to audio processed from now on; "
            "normalize the library again to bring existing items to it."
        ),
    )
    loudness_target_lufs_music: float | None = Field(
        default=None,
        ge=MIN_TARGET_LUFS,
        le=MAX_TARGET_LUFS,
        description=N_("Loudness target for music. Empty uses the overall target."),
    )
    loudness_target_lufs_audiobook: float | None = Field(
        default=None,
        ge=MIN_TARGET_LUFS,
        le=MAX_TARGET_LUFS,
        description=N_(
            "Loudness target for audiobooks, for example -14 for louder "
            "speech. Empty uses the overall target."
        ),
    )


ENV_VARS: dict[str, str] = {
    "sync_interval_seconds": "KIDSPLAY_SYNC_INTERVAL_SECONDS",
    "webp_quality": "KIDSPLAY_WEBP_QUALITY",
    "pairing_enabled": "KIDSPLAY_PAIRING_ENABLED",
    "loudness_target_lufs": "KIDSPLAY_LOUDNESS_TARGET_LUFS",
    "loudness_target_lufs_music": "KIDSPLAY_LOUDNESS_TARGET_LUFS_MUSIC",
    "loudness_target_lufs_audiobook": "KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIOBOOK",
}
"""Environment variable that pins each setting."""

LOUDNESS_KEYS: frozenset[str] = frozenset(
    key for key in ENV_VARS if key.startswith("loudness_")
)
"""Settings that change the target loudness (the page offers to normalize)."""


class ServerSettingsUpdate(BaseModel):
    """Partial update of the server settings.

    A field left out (or ``None``) is unchanged. Use ``reset`` to return
    saved settings to their defaults (for the per-type loudness targets, to
    "same as the overall target"). An unknown key is rejected.
    """

    model_config = ConfigDict(extra="forbid")

    sync_interval_seconds: int | None = None
    webp_quality: int | None = None
    pairing_enabled: bool | None = None
    loudness_target_lufs: float | None = None
    loudness_target_lufs_music: float | None = None
    loudness_target_lufs_audiobook: float | None = None
    reset: list[str] = Field(
        default_factory=list,
        description="Setting keys to revert to their default.",
    )


@dataclass(frozen=True)
class ResolvedServerSettings:
    """Effective server settings plus where each value came from.

    Attributes:
        values: The effective settings.
        env_locked: Keys pinned by an environment variable.
        saved: Keys saved from the web UI (whether or not env-locked).
    """

    values: ServerSettings
    env_locked: frozenset[str]
    saved: frozenset[str]


class EnvLockedError(ValueError):
    """Raised when an update targets a setting pinned by the environment."""


type SettingValue = int | float | bool | None


def _validated(key: str, raw: str) -> SettingValue:
    """Validate one raw value against the ``ServerSettings`` field."""
    try:
        value = ServerSettings.model_validate({key: raw})
    except ValidationError as exc:
        raise ValueError(f"invalid value {raw!r} for {key}: {exc}") from exc
    result: SettingValue = getattr(value, key)
    return result


def _serialized(value: int | float | bool) -> str:
    """Render a setting value the way ``_validated`` reads it back."""
    return str(value).lower() if isinstance(value, bool) else str(value)


async def load_server_settings(
    conn: aiosqlite.Connection,
    environ: Mapping[str, str] | None = None,
) -> ResolvedServerSettings:
    """Resolve the effective server settings.

    Precedence: environment variable, then the value saved from the web UI,
    then the default. A saved value that no longer validates (for example
    after a range was narrowed) is ignored in favour of the default.

    Args:
        conn: Open, configured connection.
        environ: Environment to read; ``os.environ`` if None.

    Returns:
        The effective settings and their sources.

    Raises:
        ValueError: If an environment variable holds an invalid value.
    """
    env = os.environ if environ is None else environ
    saved = await get_server_setting_values(conn)
    values: dict[str, SettingValue] = {}
    locked: set[str] = set()
    for key, var in ENV_VARS.items():
        env_value = env.get(var, "").strip()
        if env_value:
            values[key] = _validated(key, env_value)
            locked.add(key)
        elif key in saved:
            try:
                values[key] = _validated(key, saved[key])
            except ValueError:
                logger.warning(
                    "Ignoring invalid saved server setting %s=%r", key, saved[key]
                )
    return ResolvedServerSettings(
        values=ServerSettings.model_validate(values),
        env_locked=frozenset(locked),
        saved=frozenset(k for k in saved if k in ENV_VARS),
    )


async def update_server_settings(
    conn: aiosqlite.Connection,
    update: ServerSettingsUpdate,
    environ: Mapping[str, str] | None = None,
) -> ResolvedServerSettings:
    """Validate and save a partial update, then return the resolved settings.

    Does not commit; the caller commits.

    Args:
        conn: Open, configured connection.
        update: Values to save and keys to reset.
        environ: Environment to read; ``os.environ`` if None.

    Returns:
        The effective settings after the update.

    Raises:
        EnvLockedError: If the update changes an env-locked setting.
        ValueError: If a value is out of range or a reset key is unknown.
    """
    current = await load_server_settings(conn, environ)
    changes = {
        key: value
        for key, value in update.model_dump(exclude={"reset"}).items()
        if value is not None
    }
    unknown = [key for key in update.reset if key not in ENV_VARS]
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    locked = sorted((set(changes) | set(update.reset)) & current.env_locked)
    if locked:
        raise EnvLockedError(
            "set by environment variable: "
            + ", ".join(f"{key} ({ENV_VARS[key]})" for key in locked)
        )
    now = datetime.now()
    for key, value in changes.items():
        raw = _serialized(value)
        _validated(key, raw)
        await set_server_setting_value(conn, key, raw, now)
    for key in update.reset:
        await delete_server_setting_value(conn, key)
    return await load_server_settings(conn, environ)


def check_environment(environ: Mapping[str, str] | None = None) -> None:
    """Validate the settings' environment variables, for a fail-fast startup.

    Args:
        environ: Environment to read; ``os.environ`` if None.

    Raises:
        ValueError: If a variable is set to an invalid value.
    """
    env = os.environ if environ is None else environ
    for key, var in ENV_VARS.items():
        env_value = env.get(var, "").strip()
        if env_value:
            try:
                _validated(key, env_value)
            except ValueError as exc:
                raise ValueError(f"{var}: {exc}") from exc
    loudness_config(ServerSettings(), env)


def _loudness_switches(
    environ: Mapping[str, str] | None = None,
) -> tuple[bool, bool, float]:
    """Read the env-only loudness switches.

    Args:
        environ: Environment to read; ``os.environ`` if None.

    Returns:
        ``(enabled, allow_limiting, true_peak_dbtp)``.

    Raises:
        ValueError: If a variable holds an invalid value.
    """
    env = os.environ if environ is None else environ
    mode = env.get("KIDSPLAY_LOUDNORM", "").strip().lower() or "enabled"
    if mode not in ("enabled", "disabled"):
        raise ValueError(
            f"KIDSPLAY_LOUDNORM must be 'enabled' or 'disabled', not {mode!r}"
        )
    limiting = env.get("KIDSPLAY_LOUDNORM_LIMITING", "").strip().lower()
    if limiting not in ("", "allowed", "never"):
        raise ValueError(
            f"KIDSPLAY_LOUDNORM_LIMITING must be 'allowed' or 'never', not {limiting!r}"
        )
    raw_peak = env.get("KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP", "").strip()
    peak = DEFAULT_TRUE_PEAK_DBTP
    if raw_peak:
        try:
            peak = float(raw_peak)
        except ValueError:
            raise ValueError(
                f"KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP must be a number, not {raw_peak!r}"
            ) from None
    return mode == "enabled", limiting != "never", peak


def loudness_config(
    values: ServerSettings, environ: Mapping[str, str] | None = None
) -> LoudnessConfig:
    """Build the loudness normalization settings.

    Args:
        values: Effective server settings (the targets).
        environ: Environment to read the switches from; ``os.environ`` if None.

    Returns:
        The targets from ``values`` plus the env-only switches.

    Raises:
        ValueError: If a switch variable or the peak ceiling is invalid.
    """
    enabled, allow_limiting, peak = _loudness_switches(environ)
    return LoudnessConfig(
        enabled=enabled,
        allow_limiting=allow_limiting,
        target_lufs=values.loudness_target_lufs,
        true_peak_dbtp=peak,
        music_target_lufs=values.loudness_target_lufs_music,
        audiobook_target_lufs=values.loudness_target_lufs_audiobook,
    )


async def load_loudness_config(
    conn: aiosqlite.Connection, environ: Mapping[str, str] | None = None
) -> LoudnessConfig:
    """Resolve the loudness settings as they are right now.

    Read on every ingest and every normalization job, so a target changed on
    the settings page applies to the next file without a restart.

    Args:
        conn: Open, configured connection.
        environ: Environment to read; ``os.environ`` if None.

    Returns:
        The loudness settings.

    Raises:
        ValueError: If an environment variable holds an invalid value.
    """
    resolved = await load_server_settings(conn, environ)
    return loudness_config(resolved.values, environ)
