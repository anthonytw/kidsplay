"""``kidsplay-allinone``: set up the server and the player on one device.

For people without a home server. One command, run as the normal user on the
handheld (or any Linux machine):

1. creates the server's database and media store, and the admin account;
2. creates a profile and registers this device on it (safe to re-run: it
   reuses the ones with the same names);
3. writes the player's ``config.json`` with the local sync transport, so the
   player hard-links files from the server's media store instead of
   downloading a second copy;
4. installs and starts a systemd unit for the server, bound to localhost or,
   with ``--lan``, to the network so a parent can use the web UI from a phone.

The kiosk installer (``packages/kidsplay-device/deploy/install-kiosk.sh``) is
a separate, later step. It reads the config written here and skips the
``kidsplay-timeset`` clock bootstrap: the "server" is this machine, so its
clock cannot correct the device's.

See ``docs/ALL_IN_ONE.md``.
"""

import asyncio
import getpass
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import click

from kidsplay_models import Device, Profile
from kidsplay_server.auth import (
    MIN_PASSWORD_LENGTH,
    init_auth_db,
    is_admin_configured,
    set_initial_admin_password,
)
from kidsplay_server.database import (
    configure_conn,
    create_device,
    create_profile,
    ensure_private_db_file,
    init_db,
    list_devices,
    list_profiles,
)

UNIT_NAME = "kidsplay-server.service"
DOCS_URL = "https://github.com/anthonytw/kidsplay/blob/main/docs/ALL_IN_ONE.md"

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "kidsplay"
DEFAULT_CONFIG_PATH = Path.home() / ".kidsplay" / "config.json"

SERVER_NICE = 5
"""Niceness of the whole server process (web UI, thumbnails, tagging)."""

FFMPEG_NICE = 5
"""Extra niceness for ffmpeg on top of the server's, so 10 in total."""

_PATH = click.Path(path_type=Path)


@dataclass(frozen=True)
class Registration:
    """The profile and device this machine's player uses.

    Attributes:
        profile: The profile media is assigned to.
        device: This machine's registered device (with its API key).
        created: Whether the device was registered now (False: it already was).
    """

    profile: Profile
    device: Device
    created: bool


async def initialize_server(
    db_path: Path,
    admin_password: str | None,
    profile_name: str,
    device_name: str,
) -> tuple[Registration, bool]:
    """Create the database, admin account, profile and device.

    Idempotent: an existing admin password is never changed, and a profile or
    device with the same name is reused rather than duplicated.

    Args:
        db_path: The server's SQLite database (created if missing).
        admin_password: Password for a new admin account; ignored if the
            admin is already configured. ``None`` leaves the admin to the
            web UI's first-run page.
        profile_name: Name of the child's profile.
        device_name: Name of this device.

    Returns:
        The registration, and whether an admin password was set now.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_db_file(db_path)
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await init_auth_db(conn)

        admin_set = False
        if admin_password and not await is_admin_configured(conn):
            admin_set = await set_initial_admin_password(conn, admin_password)

        profile = next(
            (p for p in await list_profiles(conn) if p.name == profile_name), None
        )
        if profile is None:
            profile = Profile(name=profile_name)
            await create_profile(conn, profile)

        device = next(
            (
                d
                for d in await list_devices(conn)
                if d.name == device_name and d.profile_id == profile.id
            ),
            None,
        )
        created = device is None
        if device is None:
            device = Device(name=device_name, profile_id=profile.id)
            await create_device(conn, device)
        await conn.commit()
    return Registration(profile, device, created), admin_set


async def _admin_configured(db_path: Path) -> bool:
    """Whether the database at ``db_path`` already has an admin password."""
    if not db_path.exists():
        return False
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await init_auth_db(conn)
        return await is_admin_configured(conn)


def device_config(
    registration: Registration,
    *,
    server_url: str,
    media_store: Path,
    media_root: Path,
    db_path: Path,
) -> dict[str, str | int | bool]:
    """The player's ``config.json`` content for all-in-one mode.

    Same keys as ``kidsplay_device.config.DeviceConfig`` (a test loads the
    result with it), written here because the server does not depend on the
    device package.

    Args:
        registration: This device's registration.
        server_url: Where the player reaches the server (localhost).
        media_store: The server's media store, source of the linked files.
        media_root: The player's own media directory.
        db_path: The player's local database.

    Returns:
        The JSON-serialisable config.
    """
    return {
        "server_url": server_url,
        "device_id": str(registration.device.id),
        "api_key": registration.device.api_key,
        "media_root": str(media_root),
        "db_path": str(db_path),
        "sync_interval_seconds": 900,
        # The player runs on this very handheld, so it fills the screen.
        "fullscreen": True,
        "sync_transport": "local",
        "server_media_store": str(media_store),
    }


def _unit_escape(value: str) -> str:
    """Escape ``%``, which systemd reads as the start of a specifier."""
    return value.replace("%", "%%")


def render_server_unit(
    *,
    user: str,
    python: str,
    host: str,
    port: int,
    db_path: Path,
    media_store: Path,
    log_file: Path,
) -> str:
    """The systemd unit that runs the server in all-in-one mode.

    The resource budget lives here: the server runs at ``Nice=`` 5 with
    lower IO priority, its ffmpeg jobs at 10 (``KIDSPLAY_PROCESSING_NICE``)
    and one at a time (``KIDSPLAY_PROCESSING_JOBS``), so an import does not
    stutter the player sharing the CPU.

    Args:
        user: Account the server runs as (the one that owns the data).
        python: Interpreter of the virtualenv KidsPlay is installed in.
        host: Address to bind, ``127.0.0.1`` or ``0.0.0.0``.
        port: TCP port.
        db_path: Server database.
        media_store: Media store root.
        log_file: Server log file.

    Returns:
        The unit file text.
    """
    exec_start = _unit_escape(
        shlex.join(
            [
                python,
                "-m",
                "uvicorn",
                "kidsplay_server.api.app:create_app_from_env",
                "--factory",
                # KidsPlay applies X-Forwarded-* itself (KIDSPLAY_TRUSTED_PROXIES).
                "--no-proxy-headers",
                "--host",
                host,
                "--port",
                str(port),
            ]
        )
    )
    env = {
        "KIDSPLAY_DB_PATH": str(db_path),
        "KIDSPLAY_MEDIA_STORE": str(media_store),
        "KIDSPLAY_LOG_FILE": str(log_file),
        "KIDSPLAY_PROCESSING_NICE": str(FFMPEG_NICE),
        "KIDSPLAY_PROCESSING_JOBS": "1",
    }
    env_lines = "\n".join(
        f'Environment="{k}={_unit_escape(v)}"' for k, v in env.items()
    )
    return f"""[Unit]
Description=KidsPlay server (all-in-one)
Documentation={DOCS_URL}
After=network.target

[Service]
Type=simple
User={_unit_escape(user)}
ExecStart={exec_start}
{env_lines}
Nice={SERVER_NICE}
IOSchedulingClass=best-effort
IOSchedulingPriority=7
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _install_unit(unit_dir: Path, text: str) -> Path:
    """Write the unit file, with sudo if ``unit_dir`` is not writable."""
    dest = unit_dir / UNIT_NAME
    if os.access(unit_dir, os.W_OK):
        dest.write_text(text)
        return dest
    with tempfile.NamedTemporaryFile("w", suffix=".service") as tmp:
        tmp.write(text)
        tmp.flush()
        subprocess.run(["sudo", "install", "-m644", tmp.name, str(dest)], check=True)
    return dest


def _systemctl(*args: str) -> None:
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    subprocess.run([*prefix, "systemctl", *args], check=True)


def _write_config(path: Path, config: dict[str, str | int | bool], force: bool) -> None:
    """Write the player config, refusing to replace a different device's.

    Raises:
        click.ClickException: If a config that is not an all-in-one config
            already exists and ``force`` is not set.
    """
    existing: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError):
            loaded = {}
        existing = loaded if isinstance(loaded, dict) else {}
    if path.exists() and not force and existing.get("sync_transport") != "local":
        raise click.ClickException(
            f"{path} already configures a player for "
            f"{existing.get('server_url', 'another server')!r}. "
            "Pass --force to replace it."
        )
    # A re-run keeps the screen mode an earlier run (or the owner) chose.
    if existing.get("sync_transport") == "local" and isinstance(
        existing.get("fullscreen"), bool
    ):
        config = {**config, "fullscreen": existing["fullscreen"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # The config holds the device's API key: owner-only.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(config, indent=2))
    os.replace(tmp, path)


def _check_separate(media_store: Path, media_root: Path) -> None:
    """Refuse a player media directory that overlaps the server's store.

    The player prunes its media directory, so an overlap could delete the
    server's files.
    """
    store, root = media_store.resolve(), media_root.resolve()
    if store == root or store in root.parents or root in store.parents:
        raise click.ClickException(
            f"--media-root ({root}) and the server's media store ({store}) "
            "must be separate directories, neither inside the other."
        )


def _running_as_root() -> bool:
    """Whether this process is root."""
    return os.geteuid() == 0


def _same_filesystem(a: Path, b: Path) -> bool:
    """Whether two existing directories are on one filesystem."""
    return a.stat().st_dev == b.stat().st_dev


@click.command()
@click.option(
    "--data-dir",
    type=_PATH,
    default=DEFAULT_DATA_DIR,
    show_default=True,
    help="Server data: database, media store, log.",
)
@click.option(
    "--config-path",
    type=_PATH,
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Where to write the player's config.json.",
)
@click.option(
    "--media-root",
    type=_PATH,
    default=Path.home() / ".kidsplay" / "media",
    show_default=True,
    help="The player's own media directory (hard links land here). Keep it on "
    "the same filesystem as --data-dir, or files are copied.",
)
@click.option(
    "--player-db",
    type=_PATH,
    default=Path.home() / ".kidsplay" / "db.sqlite",
    show_default=True,
    help="The player's local database.",
)
@click.option("--profile-name", default="Kid", show_default=True)
@click.option("--device-name", default="This device", show_default=True)
@click.option(
    "--admin-password",
    envvar="KIDSPLAY_ADMIN_PASSWORD",
    default=None,
    help="Admin password for the web UI (prompted for if not set yet).",
)
@click.option("--port", type=int, default=8000, show_default=True)
@click.option(
    "--lan",
    is_flag=True,
    help="Listen on the network, not just localhost, so the web UI works from "
    "a phone. Plain HTTP: use a network you trust.",
)
@click.option(
    "--systemd-dir",
    type=_PATH,
    default=Path("/etc/systemd/system"),
    show_default=True,
    help="Where to install the server unit (sudo is used if not writable).",
)
@click.option(
    "--no-systemd",
    is_flag=True,
    help="Do not install or start the unit; print it instead.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Replace an existing player config for another server.",
)
def main(
    data_dir: Path,
    config_path: Path,
    media_root: Path,
    player_db: Path,
    profile_name: str,
    device_name: str,
    admin_password: str | None,
    port: int,
    lan: bool,
    systemd_dir: Path,
    no_systemd: bool,
    force: bool,
) -> None:
    """Set up the KidsPlay server and player on this one device."""
    if _running_as_root():
        raise click.ClickException(
            "Run this as the user who will run the player, not as root: the "
            "data, the player config and the server's user are all that user's."
        )
    data_dir = data_dir.expanduser().resolve()
    config_path = config_path.expanduser()
    media_root = media_root.expanduser().resolve()
    player_db = player_db.expanduser()
    db_path = data_dir / "db.sqlite"
    media_store = data_dir / "media"
    log_file = data_dir / "server.log"
    _check_separate(media_store, media_root)

    if admin_password is not None:
        _password_ok(admin_password)
    elif not asyncio.run(_admin_configured(db_path)):
        admin_password = _prompt_password()

    registration, admin_set = asyncio.run(
        initialize_server(db_path, admin_password, profile_name, device_name)
    )
    media_store.mkdir(parents=True, exist_ok=True)
    media_root.mkdir(parents=True, exist_ok=True)
    if not _same_filesystem(media_store, media_root):
        click.echo(
            "warning: the media store and --media-root are on different "
            "filesystems, so files will be copied and stored twice.",
            err=True,
        )

    _write_config(
        config_path,
        device_config(
            registration,
            server_url=f"http://127.0.0.1:{port}",
            media_store=media_store,
            media_root=media_root,
            db_path=player_db,
        ),
        force,
    )
    click.echo(f"Player config written to {config_path}")

    unit = render_server_unit(
        user=getpass.getuser(),
        python=sys.executable,
        host="0.0.0.0" if lan else "127.0.0.1",
        port=port,
        db_path=db_path,
        media_store=media_store,
        log_file=log_file,
    )
    if no_systemd:
        click.echo(f"\n# {UNIT_NAME}\n{unit}")
    else:
        dest = _install_unit(systemd_dir, unit)
        _systemctl("daemon-reload")
        _systemctl("enable", UNIT_NAME)
        # restart, not start: a re-run may have changed the port or paths.
        _systemctl("restart", UNIT_NAME)
        click.echo(f"Server unit installed at {dest} and started")

    click.echo(
        f"\nProfile {registration.profile.name!r}, device "
        f"{registration.device.name!r} "
        f"({'registered' if registration.created else 'already registered'})."
    )
    if admin_set:
        click.echo("Admin password set.")
    where = "<this-device's-address>" if lan else "localhost"
    click.echo(f"Web UI: http://{where}:{port}")
    click.echo(
        "Next: run packages/kidsplay-device/deploy/install-kiosk.sh to boot into "
        "the player (it skips the clock bootstrap for this all-in-one config)."
    )


def _prompt_password() -> str:
    """Ask for the new admin password, twice."""
    return click.prompt(
        f"Admin password for the web UI (at least {MIN_PASSWORD_LENGTH} characters)",
        hide_input=True,
        confirmation_prompt=True,
        value_proc=_password_ok,
    )


def _password_ok(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise click.UsageError(
            f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    return value
