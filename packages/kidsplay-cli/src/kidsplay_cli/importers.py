"""Importer subcommands.

Commands
--------
kidsplay importer list
"""

import asyncio

import click
from rich.console import Console
from rich.table import Table

from .client import KidsPlayClient, KidsPlayError
from .i18n import _

console = Console()


@click.group()
def importer() -> None:
    """Inspect the media sources (importers) the server supports."""


@importer.command("list")
@click.pass_context
def importer_list(ctx: click.Context) -> None:
    """List the importers installed on the server.

    Plugins such as YouTube (kidsplay-importer-ytdlp) appear only when
    installed on the server.
    """

    async def _run_cmd() -> list[dict]:
        async with KidsPlayClient(ctx.obj["server"], ctx.obj["token"]) as client:
            return await client.list_importers()

    try:
        importers = asyncio.run(_run_cmd())
    except KidsPlayError as exc:
        msg = exc.detail
        if exc.error_code:
            msg = f"{msg} [{exc.error_code}]"
        console.print(_("[bold red]Error:[/bold red] {msg}").format(msg=msg))
        raise SystemExit(1) from exc

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column(_("Name"), style="bold", no_wrap=True)
    table.add_column(_("Label"))
    table.add_column(_("Queued"))
    table.add_column(_("Preview"))
    for info in importers:
        table.add_row(
            info["name"],
            info["label"],
            _("yes") if info["requires_queue"] else _("no"),
            _("yes") if info["supports_preview"] else _("no"),
        )
    console.print(table)
