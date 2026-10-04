"""Admin authentication subcommands.

Commands
--------
kidsplay auth login [--name NAME]  — prompt for the admin password, save a token
kidsplay auth logout               — revoke and forget the saved token
kidsplay auth status               — show whether the CLI is authenticated
"""

import asyncio
import socket

import click
from rich.console import Console

from .client import KidsPlayClient, KidsPlayError
from .credentials import StoredToken, delete_token, load_token, save_token
from .i18n import N_, _

console = Console()


def _fail(exc: KidsPlayError) -> None:
    msg = exc.detail
    if exc.error_code:
        msg = f"{msg} [{exc.error_code}]"
    console.print(_("[bold red]Error:[/bold red] {msg}").format(msg=msg))
    raise SystemExit(1)


@click.group()
def auth() -> None:
    """Log in to the server's admin API."""


@auth.command("login")
@click.option(
    "--name",
    default=None,
    help=N_("Label for the token on the server. Default: kidsplay-cli@<hostname>."),
)
@click.password_option(
    "--password",
    prompt=N_("Admin password"),
    confirmation_prompt=False,
    envvar=None,
    help=N_("Admin password (prompted for if omitted)."),
)
@click.pass_context
def auth_login(ctx: click.Context, name: str | None, password: str) -> None:
    """Create an admin API token and save it for this server.

    The token is stored in ~/.config/kidsplay/credentials.json (mode 0600)
    and used automatically by every other command.
    """
    server: str = ctx.obj["server"]
    token_name = name or f"kidsplay-cli@{socket.gethostname()}"

    async def _login() -> dict:
        async with KidsPlayClient(server) as client:
            return await client.login(password, token_name)

    try:
        created = asyncio.run(_login())
    except KidsPlayError as exc:
        _fail(exc)
        return  # unreachable, but satisfies type checker

    path = save_token(
        server, StoredToken(token=created["token"], token_id=str(created["id"]))
    )
    console.print(
        _(
            "[green]Logged in to[/green] {server} "
            "[dim](token '{name}' saved to {path})[/dim]"
        ).format(server=server, name=created["name"], path=path)
    )


@auth.command("logout")
@click.pass_context
def auth_logout(ctx: click.Context) -> None:
    """Revoke the saved token on the server and delete it locally."""
    server: str = ctx.obj["server"]
    stored = load_token(server)
    if stored is None:
        console.print(_("[dim]Not logged in to {server}.[/dim]").format(server=server))
        return

    async def _revoke() -> None:
        async with KidsPlayClient(server, stored.token) as client:
            await client.revoke_token(stored.token_id)

    try:
        asyncio.run(_revoke())
    except KidsPlayError as exc:
        # Already revoked (401/404) is fine; anything else is worth a mention,
        # but the local copy is removed either way.
        if exc.status_code not in (401, 404):
            console.print(
                _(
                    "[yellow]Warning:[/yellow] could not revoke token on server: "
                    "{detail}"
                ).format(detail=exc.detail)
            )
    delete_token(server)
    console.print(_("[green]Logged out of[/green] {server}").format(server=server))


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show whether the CLI can use the server's admin API."""
    server: str = ctx.obj["server"]

    async def _status() -> dict:
        async with KidsPlayClient(server, ctx.obj["token"]) as client:
            return await client.auth_status()

    try:
        status = asyncio.run(_status())
    except KidsPlayError as exc:
        if exc.status_code == 401:
            console.print(
                _(
                    "Not logged in to {server}. Run [bold]kidsplay auth login[/bold]."
                ).format(server=server)
            )
            raise SystemExit(1) from exc
        _fail(exc)
        return  # unreachable, but satisfies type checker

    if not status["auth_enabled"]:
        console.print(
            _("{server} has admin authentication [bold]disabled[/bold].").format(
                server=server
            )
        )
    else:
        console.print(
            _("[green]Authenticated[/green] to {server} with an API token.").format(
                server=server
            )
        )
