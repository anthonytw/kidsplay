"""KidsPlay CLI entry point.

Defines the root ``kidsplay`` group with global options and registers the
``auth``, ``media``, ``device``, ``profile`` and ``importer`` subgroups.

Usage::

    kidsplay --help
    kidsplay --server http://kidsplay.local:8000 auth login
    kidsplay --server http://kidsplay.local:8000 profile list
    KIDSPLAY_SERVER=http://kidsplay.local:8000 kidsplay media list
"""

import click

from .auth import auth
from .credentials import load_token
from .devices import device, profile
from .i18n import N_, LocalizedGroup
from .importers import importer
from .media import media

_SERVER_HELP = N_(
    "Server base URL. Overrides the KIDSPLAY_SERVER environment variable."
)
_TOKEN_HELP = N_(
    "Admin API token. Overrides the KIDSPLAY_TOKEN environment variable and "
    "the token saved by 'kidsplay auth login'."
)
_LANG_HELP = N_(
    "Language of messages (en, es). Defaults to LANGUAGE, LC_ALL, LC_MESSAGES "
    "or LANG, then English."
)


@click.group(cls=LocalizedGroup)
@click.option(
    "--server",
    envvar="KIDSPLAY_SERVER",
    default="http://localhost:8000",
    show_default=True,
    help=_SERVER_HELP,
)
@click.option("--token", envvar="KIDSPLAY_TOKEN", default=None, help=_TOKEN_HELP)
@click.option("--lang", default=None, metavar="LANG", help=_LANG_HELP)
@click.pass_context
def cli(ctx: click.Context, server: str, token: str | None, lang: str | None) -> None:
    """KidsPlay — media management for kids' devices.

    Manages media, profiles, and devices on the KidsPlay server.
    Set KIDSPLAY_SERVER to avoid passing --server every time, and run
    'kidsplay auth login' once to save an admin token for that server.
    """
    del lang  # already applied by LocalizedGroup.parse_args
    ctx.ensure_object(dict)
    ctx.obj["server"] = server
    if token is None:
        try:
            stored = load_token(server)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        token = stored.token if stored else None
    ctx.obj["token"] = token


cli.add_command(auth)
cli.add_command(profile)
cli.add_command(device)
cli.add_command(media)
cli.add_command(importer)
