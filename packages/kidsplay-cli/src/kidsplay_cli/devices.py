"""Profile and device subcommands.

Commands
--------
kidsplay profile list
kidsplay profile create <name>
kidsplay profile delete <id>
kidsplay profile settings <id> [--max-volume N] [--bedtime-mode MODE]
    [--language LANG] ...

kidsplay device list
kidsplay device create <name> --profile <id>
kidsplay device delete <id>
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from kidsplay_models import (
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    BedtimeMode,
    BedtimeWindow,
    ProfileSettings,
    Weekday,
)

from .client import KidsPlayClient, KidsPlayError
from .i18n import N_, _

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Any: mirrors asyncio.run(), which accepts any coroutine yield/send types.
def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine synchronously via asyncio.run()."""
    return asyncio.run(coro)


def _handle_error(exc: KidsPlayError) -> None:
    """Print a styled error and exit with code 1."""
    msg = exc.detail
    if exc.error_code:
        msg = f"{msg} [{exc.error_code}]"
    console.print(_("[bold red]Error:[/bold red] {msg}").format(msg=msg))
    if exc.status_code == 401:
        console.print(_("Log in first with [bold]kidsplay auth login[/bold]."))
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Profile commands
# ---------------------------------------------------------------------------


@click.group()
def profile() -> None:
    """Manage child profiles."""


@profile.command("list")
@click.pass_context
def profile_list(ctx: click.Context) -> None:
    """List all profiles."""

    async def _run_cmd() -> None:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            profiles = await client.list_profiles()

        if not profiles:
            console.print(_("[dim]No profiles found.[/dim]"))
            return

        table = Table(title=_("Profiles"), show_header=True, header_style="bold cyan")
        table.add_column(_("ID"), style="dim", no_wrap=True)
        table.add_column(_("Name"), style="bold")
        table.add_column(_("Created"))

        for p in profiles:
            created = p.get("created_at", "")[:10]
            table.add_row(p["id"], p["name"], created)

        console.print(table)

    try:
        _run(_run_cmd())
    except KidsPlayError as exc:
        _handle_error(exc)


@profile.command("create")
@click.argument("name")
@click.pass_context
def profile_create(ctx: click.Context, name: str) -> None:
    """Create a new profile with NAME."""

    async def _run_cmd() -> dict:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            return await client.create_profile(name)

    try:
        p = _run(_run_cmd())
        console.print(
            _(
                "[green]Created profile[/green] [bold]{name}[/bold] [dim]({id})[/dim]"
            ).format(name=p["name"], id=p["id"])
        )
    except KidsPlayError as exc:
        _handle_error(exc)


@profile.command("delete")
@click.argument("profile_id")
@click.confirmation_option(
    prompt=N_("Delete this profile?"), help=N_("Confirm the action without prompting.")
)
@click.pass_context
def profile_delete(ctx: click.Context, profile_id: str) -> None:
    """Delete the profile with PROFILE_ID."""

    async def _run_cmd() -> None:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            await client.delete_profile(profile_id)

    try:
        _run(_run_cmd())
        console.print(
            _("[green]Deleted profile[/green] [dim]{profile_id}[/dim]").format(
                profile_id=profile_id
            )
        )
    except KidsPlayError as exc:
        _handle_error(exc)


def _parse_days(spec: str) -> list[Weekday]:
    """Parse ``mon,tue`` / ``all`` / ``weekdays`` / ``weekend`` into weekdays."""
    days: list[Weekday] = []
    for part in spec.lower().split(","):
        part = part.strip()
        if part == "all":
            days.extend(Weekday)
        elif part == "weekdays":
            days.extend(list(Weekday)[:5])
        elif part == "weekend":
            days.extend(list(Weekday)[5:])
        else:
            try:
                days.append(Weekday(part))
            except ValueError:
                raise click.BadParameter(
                    _(
                        "unknown day {part!r}; use mon..sun, weekdays, weekend or all"
                    ).format(part=part)
                ) from None
    return days


def _parse_bedtime(spec: str) -> tuple[list[Weekday], BedtimeWindow]:
    """Parse ``DAYS=HH:MM-HH:MM`` into weekdays and a window."""
    days_part, sep, times = spec.partition("=")
    bedtime, dash, wake = times.partition("-")
    if not sep or not dash:
        raise click.BadParameter(
            _("{spec!r}: expected DAYS=BEDTIME-WAKE, e.g. weekdays=20:00-07:00").format(
                spec=spec
            )
        )
    try:
        window = BedtimeWindow.model_validate(
            {"bedtime": bedtime.strip(), "wake": wake.strip()}
        )
    except ValueError:  # pydantic's ValidationError is a ValueError
        raise click.BadParameter(
            _(
                "{spec!r}: times must be local HH:MM (no timezone offset) and "
                "bedtime must differ from wake"
            ).format(spec=spec)
        ) from None
    return _parse_days(days_part), window


# Display text for enum values. Tables show these, never the raw value; the
# raw values stay what users type after ``--bedtime-mode`` and in DAYS.
_MODE_LABELS: dict[BedtimeMode, str] = {
    BedtimeMode.OFF: N_("off — no restrictions"),
    BedtimeMode.AUDIOBOOKS_ONLY: N_("audiobooks only"),
    BedtimeMode.SLEEP_SCREEN: N_("sleep screen — no playback"),
}

_WEEKDAY_LABELS: dict[Weekday, str] = {
    Weekday.MONDAY: N_("Monday"),
    Weekday.TUESDAY: N_("Tuesday"),
    Weekday.WEDNESDAY: N_("Wednesday"),
    Weekday.THURSDAY: N_("Thursday"),
    Weekday.FRIDAY: N_("Friday"),
    Weekday.SATURDAY: N_("Saturday"),
    Weekday.SUNDAY: N_("Sunday"),
}


def _language_text(language: str | None) -> str:
    """Name a profile's language; None means the device's own default."""
    if language is None:
        return _("device default")
    return LANGUAGE_NAMES.get(language, language)


def _print_settings(settings: ProfileSettings) -> None:
    """Print profile settings as a table."""
    table = Table(title=_("Profile settings"), show_header=False)
    table.add_column(_("Setting"), style="bold")
    table.add_column(_("Value"))
    table.add_row(_("Max volume"), f"{settings.max_volume}%")
    buttons = (
        _("device default")
        if settings.volume_buttons is None
        else _("on")
        if settings.volume_buttons
        else _("off")
    )
    table.add_row(_("Volume buttons"), buttons)
    table.add_row(_("Button sounds"), _("on") if settings.ui_sounds else _("off"))
    table.add_row(_("Language"), _language_text(settings.language))
    table.add_row(_("Bedtime mode"), _(_MODE_LABELS[settings.bedtime_mode]))
    for day in Weekday:
        window = settings.bedtime_schedule.get(day)
        value = (
            f"{window.bedtime:%H:%M} - {window.wake:%H:%M}"
            if window
            else _("[dim]none[/dim]")
        )
        table.add_row(_("Bedtime {day}").format(day=_(_WEEKDAY_LABELS[day])), value)
    console.print(table)


@profile.command("settings")
@click.argument("profile_id")
@click.option(
    "--max-volume",
    type=click.IntRange(0, 100),
    help=N_("Loudest the device may play, in percent."),
)
@click.option(
    "--volume-buttons/--no-volume-buttons",
    default=None,
    help=N_("Enable the in-app volume buttons (for hardware without a dial)."),
)
@click.option(
    "--ui-sounds/--no-ui-sounds",
    default=None,
    help=N_("Play a short sound when a button is pressed (on by default)."),
)
@click.option(
    "--bedtime-mode",
    type=click.Choice([m.value for m in BedtimeMode]),
    help=N_("What the device does during bedtime."),
)
@click.option(
    "--language",
    type=click.Choice(SUPPORTED_LANGUAGES),
    help=N_("Language of the device's screens."),
)
@click.option(
    "--bedtime",
    "bedtimes",
    multiple=True,
    metavar="DAYS=HH:MM-HH:MM",
    help=N_(
        "Set bedtime and wake time, e.g. weekdays=20:00-07:00 or "
        "fri,sat=21:00-08:30. DAYS: mon..sun, weekdays, weekend, all. Repeatable."
    ),
)
@click.option(
    "--no-bedtime",
    "no_bedtimes",
    multiple=True,
    metavar="DAYS",
    help=N_("Remove the bedtime for DAYS (same syntax as --bedtime). Repeatable."),
)
@click.pass_context
def profile_settings(
    ctx: click.Context,
    profile_id: str,
    max_volume: int | None,
    volume_buttons: bool | None,
    ui_sounds: bool | None,
    bedtime_mode: str | None,
    language: str | None,
    bedtimes: tuple[str, ...],
    no_bedtimes: tuple[str, ...],
) -> None:
    """Show or change the settings of profile PROFILE_ID.

    Without options, prints the current settings. With options, changes
    only what is given and prints the result. Devices pick changes up at
    their next sync.
    """
    removals = [day for spec in no_bedtimes for day in _parse_days(spec)]
    additions = [_parse_bedtime(spec) for spec in bedtimes]
    changing = (
        max_volume is not None
        or volume_buttons is not None
        or ui_sounds is not None
        or bedtime_mode is not None
        or language is not None
        or bool(removals)
        or bool(additions)
    )

    async def _run_cmd() -> ProfileSettings:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            current = ProfileSettings.model_validate(
                await client.get_profile_settings(profile_id)
            )
            if not changing:
                return current
            schedule = dict(current.bedtime_schedule)
            for day in removals:
                schedule.pop(day, None)
            for days, window in additions:
                for day in days:
                    schedule[day] = window
            updated = current.model_copy(
                update={
                    "bedtime_schedule": schedule,
                    **({"max_volume": max_volume} if max_volume is not None else {}),
                    **(
                        {"volume_buttons": volume_buttons}
                        if volume_buttons is not None
                        else {}
                    ),
                    **({"ui_sounds": ui_sounds} if ui_sounds is not None else {}),
                    **(
                        {"bedtime_mode": BedtimeMode(bedtime_mode)}
                        if bedtime_mode is not None
                        else {}
                    ),
                    **({"language": language} if language is not None else {}),
                }
            )
            stored = await client.put_profile_settings(
                profile_id, updated.model_dump(mode="json")
            )
            return ProfileSettings.model_validate(stored)

    try:
        settings = _run(_run_cmd())
    except KidsPlayError as exc:
        _handle_error(exc)
        return
    if changing:
        console.print(_("[green]Updated profile settings.[/green]"))
    _print_settings(settings)


# ---------------------------------------------------------------------------
# Device commands
# ---------------------------------------------------------------------------


@click.group()
def device() -> None:
    """Manage playback devices."""


@device.command("list")
@click.pass_context
def device_list(ctx: click.Context) -> None:
    """List all registered devices."""

    async def _run_cmd() -> None:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            devices = await client.list_devices()

        if not devices:
            console.print(_("[dim]No devices found.[/dim]"))
            return

        table = Table(title=_("Devices"), show_header=True, header_style="bold cyan")
        table.add_column(_("ID"), style="dim", no_wrap=True)
        table.add_column(_("Name"), style="bold")
        table.add_column(_("Profile ID"), style="dim")
        table.add_column(_("Display"))
        table.add_column(_("Last sync"))

        for d in devices:
            last_sync = (d.get("last_sync_at") or _("never"))[:19]
            display = f"{d['display_width']}×{d['display_height']}"
            table.add_row(d["id"], d["name"], d["profile_id"], display, last_sync)

        console.print(table)

    try:
        _run(_run_cmd())
    except KidsPlayError as exc:
        _handle_error(exc)


@device.command("create")
@click.argument("name")
@click.option(
    "--profile",
    "profile_id",
    required=True,
    help=N_("UUID of the child's profile to link to this device."),
)
@click.option("--width", default=640, show_default=True, help=N_("Screen width px."))
@click.option("--height", default=480, show_default=True, help=N_("Screen height px."))
@click.pass_context
def device_create(
    ctx: click.Context,
    name: str,
    profile_id: str,
    width: int,
    height: int,
) -> None:
    """Register a new device with NAME."""

    async def _run_cmd() -> dict:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            return await client.create_device(
                name, profile_id, display_width=width, display_height=height
            )

    try:
        d = _run(_run_cmd())
        console.print(
            _(
                "[green]Registered device[/green] [bold]{name}[/bold] "
                "[dim]({id})[/dim]\n"
                "  api_key: [dim]{api_key}[/dim]"
            ).format(name=d["name"], id=d["id"], api_key=d["api_key"])
        )
    except KidsPlayError as exc:
        _handle_error(exc)


@device.command("delete")
@click.argument("device_id")
@click.confirmation_option(
    prompt=N_("Delete this device?"), help=N_("Confirm the action without prompting.")
)
@click.pass_context
def device_delete(ctx: click.Context, device_id: str) -> None:
    """Unregister the device with DEVICE_ID."""

    async def _run_cmd() -> None:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            await client.delete_device(device_id)

    try:
        _run(_run_cmd())
        console.print(
            _("[green]Deleted device[/green] [dim]{device_id}[/dim]").format(
                device_id=device_id
            )
        )
    except KidsPlayError as exc:
        _handle_error(exc)


@device.command("setup")
@click.option(
    "--name",
    default=None,
    help=N_("Device name. Required when registering a new device."),
)
@click.option(
    "--profile",
    "profile_id",
    default=None,
    help=N_("Profile UUID to link. Required when registering a new device."),
)
@click.option(
    "--device-id",
    default=None,
    help=N_("UUID of an already-registered device. Use instead of --name/--profile."),
)
@click.option(
    "--api-key",
    default=None,
    help=N_("API key of the already-registered device. Required with --device-id."),
)
@click.option(
    "--width",
    default=640,
    show_default=True,
    help=N_(
        "Screen width in pixels. Registered for a new device, and written to the "
        "config when it is not 640."
    ),
)
@click.option(
    "--height",
    default=480,
    show_default=True,
    help=N_(
        "Screen height in pixels. Registered for a new device, and written to the "
        "config when it is not 480."
    ),
)
# Both live under ~/.kidsplay/ alongside config.json, NOT under ~/kidsplay/ --
# that is the git checkout the device's app is deployed from, so media landing
# there puts a couple of GB of untracked files inside the worktree, one
# `git clean -fdx` away from deletion.
@click.option(
    "--media-root",
    default="~/.kidsplay/media",
    show_default=True,
    help=N_("Path on the device where synced media will be stored."),
)
@click.option(
    "--db-path",
    default="~/.kidsplay/db.sqlite",
    show_default=True,
    help=N_("Path on the device for the local SQLite database."),
)
@click.option(
    "--sync-interval",
    default=900,
    show_default=True,
    help=N_("Seconds between background sync attempts."),
)
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(),
    help=N_("Write config JSON to this file instead of printing to stdout."),
)
@click.pass_context
def device_setup(
    ctx: click.Context,
    name: str | None,
    profile_id: str | None,
    device_id: str | None,
    api_key: str | None,
    width: int,
    height: int,
    media_root: str,
    db_path: str,
    sync_interval: int,
    output: str | None,
) -> None:
    """Generate a device config file.

    Two modes:

    \b
    NEW DEVICE — register on the server and generate config:
      kidsplay device setup --name "Alice's Gameboy" --profile <profile-id>

    \b
    EXISTING DEVICE — generate config without re-registering:
      kidsplay device setup --device-id <id> --api-key <key>

    \b
    By default the config JSON is printed to stdout so you can copy it to the
    device. Use --output to write directly to a file:
      kidsplay device setup ... -o config.json
    """
    import json

    # Validate option combinations.
    if device_id is not None:
        if api_key is None:
            console.print(
                _("[bold red]Error:[/bold red] --api-key is required with --device-id.")
            )
            raise SystemExit(1)
        if name is not None or profile_id is not None:
            console.print(
                _(
                    "[bold red]Error:[/bold red] "
                    "--name and --profile are not used with --device-id."
                )
            )
            raise SystemExit(1)
        resolved_id = device_id
        resolved_key = api_key
    else:
        if name is None or profile_id is None:
            console.print(
                _(
                    "[bold red]Error:[/bold red] "
                    "Provide either --name + --profile (new device) "
                    "or --device-id + --api-key (existing device)."
                )
            )
            raise SystemExit(1)

        async def _create() -> dict:
            async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
                return await client.create_device(
                    name,
                    profile_id,
                    display_width=width,
                    display_height=height,
                )

        try:
            d = _run(_create())
        except KidsPlayError as exc:
            _handle_error(exc)
            return  # unreachable, but satisfies type checker

        resolved_id = d["id"]
        resolved_key = d["api_key"]
        err_console.print(
            _(
                "[green]Registered device[/green] [bold]{name}[/bold] [dim]({id})[/dim]"
            ).format(name=d["name"], id=resolved_id)
        )

    config = {
        "server_url": ctx.obj["server"],
        "device_id": resolved_id,
        "api_key": resolved_key,
        "media_root": media_root,
        "db_path": db_path,
        "sync_interval_seconds": sync_interval,
    }
    # The player reads its window size from here; the reference 640x480 is what
    # it assumes when the keys are absent, so existing configs stay unchanged.
    if (width, height) != (640, 480):
        config["width"] = width
        config["height"] = height
    config_json = json.dumps(config, indent=2)

    if output:
        from pathlib import Path

        dest = Path(output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(config_json)
        err_console.print(
            _("[green]Config written to[/green] {dest}").format(dest=dest)
        )
    else:
        print(config_json)
