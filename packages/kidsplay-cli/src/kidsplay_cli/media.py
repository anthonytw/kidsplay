"""Media subcommands.

Commands
--------
kidsplay media ingest <path> --type music|audiobook|photo
    [--playlist <title>] [--profile <id>]
kidsplay media list [--type music] [--profile <id>] [--search <q>]
kidsplay media show <id>
kidsplay media delete <id>
kidsplay media assign <media_id> <profile_id>
kidsplay media unassign <media_id> <profile_id>
kidsplay media normalize (--all | <id>... | --status) [--wait]
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from .client import KidsPlayClient, KidsPlayError
from .i18n import N_, _

console = Console()

_NORMALIZE_POLL_SECONDS = 2.0
_MAX_ERRORS_SHOWN = 10

_MEDIA_TYPES = click.Choice(["music", "audiobook", "photo"], case_sensitive=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Any: mirrors asyncio.run(), which accepts any coroutine yield/send types.
def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _handle_error(exc: KidsPlayError) -> None:
    msg = exc.detail
    if exc.error_code:
        msg = f"{msg} [{exc.error_code}]"
    console.print(_("[bold red]Error:[/bold red] {msg}").format(msg=msg))
    if exc.status_code == 401:
        console.print(_("Log in first with [bold]kidsplay auth login[/bold]."))
    raise SystemExit(1)


def _loudness_text(item: dict) -> str:
    """Describe a media item's loudness normalization for ``media show``."""
    target = item.get("loudness_target_lufs")
    source = item.get("loudness_source_lufs")
    gain = item.get("loudness_gain_db")
    if target is None:
        return _("[dim]not normalized[/dim]")
    if gain is None or source is None:
        return _("too quiet to measure, kept unchanged")
    mode = item.get("loudness_mode")
    if mode == "dynamic":
        return _(
            "{source:.1f} LUFS → {target:g} LUFS ({gain:+.1f} dB, loud peaks limited)"
        ).format(source=source, target=target, gain=gain)
    if mode == "capped":
        return _(
            "{source:.1f} LUFS → {output:.1f} LUFS ({gain:+.1f} dB, kept below "
            "the {target:g} LUFS target to avoid limiting)"
        ).format(source=source, output=source + gain, gain=gain, target=target)
    return _("{source:.1f} LUFS → {target:g} LUFS ({gain:+.1f} dB)").format(
        source=source, target=target, gain=gain
    )


def _print_normalize_status(status: dict) -> None:
    """Print a loudness normalization status dict, with at most a few errors."""
    state = _("running") if status["running"] else _("finished")
    if not status.get("started_at"):
        state = _("not run since the server started")
    console.print(_("Loudness backfill: {state}").format(state=state))
    console.print(
        _(
            "  {normalized} normalized, {skipped} skipped, "
            "{unchanged} unchanged, {failed} failed (of {total})"
        ).format(
            normalized=status["normalized"],
            skipped=status["skipped"],
            unchanged=status["unchanged"],
            failed=status["failed"],
            total=status["total"],
        )
    )
    errors = status.get("errors", [])
    for error in errors[:_MAX_ERRORS_SHOWN]:
        console.print(f"  [red]{error}[/red]")
    hidden = len(errors) - _MAX_ERRORS_SHOWN + status.get("errors_omitted", 0)
    if hidden > 0:
        console.print(
            _("  [red]…and {n} more failed[/red]").format(n=hidden),
        )


# Display names for MediaType values. Tables show these, never the raw value;
# the values themselves stay what users type after ``--type``.
_MEDIA_TYPE_LABELS = {
    "music": N_("music"),
    "audiobook": N_("audiobook"),
    "photo": N_("photo"),
}


def _type_label(media_type: str) -> str:
    """Translated name of a media type (unknown values pass through)."""
    message = _MEDIA_TYPE_LABELS.get(media_type)
    return _(message) if message else media_type


def _status_style(status: str) -> str:
    return {
        "ready": _("[green]ready[/green]"),
        "processing": _("[yellow]processing[/yellow]"),
        "failed": _("[red]failed[/red]"),
        "pending": _("[dim]pending[/dim]"),
    }.get(status, status)


# ---------------------------------------------------------------------------
# Media group
# ---------------------------------------------------------------------------


@click.group()
def media() -> None:
    """Manage media items (music, audiobooks, photos)."""


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@media.command("ingest")
@click.argument("path")
@click.option(
    "--type",
    "media_type",
    type=_MEDIA_TYPES,
    required=True,
    help=N_("Media type: music, audiobook, or photo."),
)
@click.option(
    "--playlist",
    "playlist_title",
    default=None,
    help=N_("Playlist/album name. Defaults to the directory name."),
)
@click.option(
    "--profile",
    "profile_id",
    default=None,
    multiple=True,
    help=N_("Profile UUID to assign ingested media to. Repeatable."),
)
@click.pass_context
def media_ingest(
    ctx: click.Context,
    path: str,
    media_type: str,
    playlist_title: str | None,
    profile_id: tuple[str, ...],
) -> None:
    """Ingest media from PATH on the server filesystem.

    PATH can be a single file or a directory.  For directories, all
    supported files are processed recursively.
    """
    import os

    # Default playlist title to the directory/file base name.
    if not playlist_title:
        playlist_title = os.path.basename(path.rstrip("/\\")) or path

    async def _run_cmd() -> dict:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            return await client.ingest(
                path,
                media_type,
                playlist_title,
                profile_ids=list(profile_id) or None,
            )

    try:
        result = _run(_run_cmd())
    except KidsPlayError as exc:
        _handle_error(exc)
        return

    total = result["total_files"]
    ok = result["successful"]
    skipped = result["skipped"]
    failed = result["failed"]

    if total == 0:
        console.print(_("[yellow]No supported files found.[/yellow]"))
        return

    # Summary line.
    parts = [_("[green]{count} ingested[/green]").format(count=ok)]
    if skipped:
        parts.append(_("[dim]{count} skipped (duplicate)[/dim]").format(count=skipped))
    if failed:
        parts.append(_("[red]{count} failed[/red]").format(count=failed))
    console.print(
        _("Ingested {total} file(s): {parts}").format(
            total=total, parts=", ".join(parts)
        )
    )

    # Show individual failures.
    for r in result.get("results", []):
        if r.get("errors"):
            console.print(
                _("  [red]✗[/red] {path}: {errors}").format(
                    path=r["source_path"], errors="; ".join(r["errors"])
                )
            )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@media.command("list")
@click.option("--type", "media_type", type=_MEDIA_TYPES, default=None)
@click.option(
    "--profile", "profile_id", default=None, help=N_("Filter by profile UUID.")
)
@click.option("--search", "q", default=None, help=N_("Search title/artist/playlist."))
@click.option("--limit", default=100, show_default=True)
@click.option("--offset", default=0, show_default=True)
@click.pass_context
def media_list(
    ctx: click.Context,
    media_type: str | None,
    profile_id: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> None:
    """List media items with optional filtering."""

    async def _run_cmd() -> list[dict]:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            return await client.list_media(
                media_type=media_type,
                profile_id=profile_id,
                q=q,
                limit=limit,
                offset=offset,
            )

    try:
        items = _run(_run_cmd())
    except KidsPlayError as exc:
        _handle_error(exc)
        return

    if not items:
        console.print(_("[dim]No media found.[/dim]"))
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column(_("ID"), style="dim", no_wrap=True)
    table.add_column(_("Type"))
    table.add_column(_("Playlist"))
    table.add_column(_("Title"), style="bold")
    table.add_column(_("Artist"))
    table.add_column(_("Status"))

    for item in items:
        duration = item.get("duration_seconds")
        artist = item.get("artist") or ""
        if duration:
            mins, secs = divmod(duration, 60)
            dur = f"[{mins}:{secs:02d}]"
            artist = f"{artist} {dur}" if artist else dur
        table.add_row(
            item["id"],
            _type_label(item["media_type"]),
            item["playlist_title"],
            item["title"],
            artist,
            _status_style(item["processing_status"]),
        )

    console.print(table)
    if len(items) == limit:
        console.print(
            _("[dim]Showing {limit} results. Use --offset to paginate.[/dim]").format(
                limit=limit
            )
        )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@media.command("show")
@click.argument("media_id")
@click.pass_context
def media_show(ctx: click.Context, media_id: str) -> None:
    """Show details for a single media item."""

    async def _run_cmd() -> dict:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            return await client.get_media(media_id)

    try:
        item = _run(_run_cmd())
    except KidsPlayError as exc:
        _handle_error(exc)
        return

    console.print(f"[bold]{item['title']}[/bold]")
    console.print(_("  ID:         [dim]{value}[/dim]").format(value=item["id"]))
    console.print(
        _("  Type:       {value}").format(value=_type_label(item["media_type"]))
    )
    console.print(_("  Playlist:   {value}").format(value=item["playlist_title"]))
    console.print(_("  Artist:     {value}").format(value=item.get("artist") or "—"))
    duration = item.get("duration_seconds")
    if duration:
        mins, secs = divmod(duration, 60)
        console.print(_("  Duration:   {mins}:{secs:02d}").format(mins=mins, secs=secs))
    console.print(
        _("  Status:     {value}").format(
            value=_status_style(item["processing_status"])
        )
    )
    if item["media_type"] != "photo":
        console.print(_("  Loudness:   {value}").format(value=_loudness_text(item)))
    console.print(
        _("  Created:    {value}").format(value=item.get("created_at", "")[:19])
    )


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@media.command("delete")
@click.argument("media_id")
@click.confirmation_option(
    prompt=N_("Delete this media item and all its files?"),
    help=N_("Confirm the action without prompting."),
)
@click.pass_context
def media_delete(ctx: click.Context, media_id: str) -> None:
    """Delete a media item and all its processed files."""

    async def _run_cmd() -> None:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            await client.delete_media(media_id)

    try:
        _run(_run_cmd())
        console.print(
            _("[green]Deleted[/green] [dim]{media_id}[/dim]").format(media_id=media_id)
        )
    except KidsPlayError as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# assign / unassign
# ---------------------------------------------------------------------------


@media.command("assign")
@click.argument("media_id")
@click.argument("profile_id")
@click.pass_context
def media_assign(ctx: click.Context, media_id: str, profile_id: str) -> None:
    """Assign MEDIA_ID to PROFILE_ID."""

    async def _run_cmd() -> list[dict]:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            return await client.assign_media(media_id, [profile_id])

    try:
        _run(_run_cmd())
        console.print(
            _(
                "[green]Assigned[/green] [dim]{media_id}[/dim] → "
                "[dim]{profile_id}[/dim]"
            ).format(media_id=media_id, profile_id=profile_id)
        )
    except KidsPlayError as exc:
        _handle_error(exc)


@media.command("unassign")
@click.argument("media_id")
@click.argument("profile_id")
@click.pass_context
def media_unassign(ctx: click.Context, media_id: str, profile_id: str) -> None:
    """Remove the assignment of MEDIA_ID from PROFILE_ID."""

    async def _run_cmd() -> None:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            await client.unassign_media(media_id, profile_id)

    try:
        _run(_run_cmd())
        console.print(
            _(
                "[green]Unassigned[/green] [dim]{media_id}[/dim] "
                "from [dim]{profile_id}[/dim]"
            ).format(media_id=media_id, profile_id=profile_id)
        )
    except KidsPlayError as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


@media.command("normalize")
@click.argument("media_ids", nargs=-1)
@click.option(
    "--all", "all_media", is_flag=True, help=N_("Normalize the whole audio library.")
)
@click.option(
    "--status", "show_status", is_flag=True, help=N_("Show backfill progress only.")
)
@click.option("--wait", is_flag=True, help=N_("Wait until the backfill finishes."))
@click.pass_context
def media_normalize(
    ctx: click.Context,
    media_ids: tuple[str, ...],
    all_media: bool,
    show_status: bool,
    wait: bool,
) -> None:
    """Loudness-normalize existing audio on the server, in the background.

    Items already normalized to the current target are skipped, so running
    this again re-encodes nothing. Pass --all or one or more MEDIA_IDS.
    """
    if show_status == bool(all_media or media_ids):
        raise click.UsageError(_("Give --all, one or more MEDIA_IDS, or --status."))
    if all_media and media_ids:
        raise click.UsageError(_("Give either --all or MEDIA_IDS, not both."))

    async def _run_cmd() -> dict:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            if show_status:
                status = await client.normalize_status()
            else:
                status = await client.start_normalize(
                    all_media=all_media, media_ids=list(media_ids)
                )
            while wait and status["running"]:
                await asyncio.sleep(_NORMALIZE_POLL_SECONDS)
                status = await client.normalize_status()
            return status

    try:
        status = _run(_run_cmd())
    except KidsPlayError as exc:
        _handle_error(exc)
        return
    _print_normalize_status(status)
    if status["failed"]:
        raise SystemExit(1)
