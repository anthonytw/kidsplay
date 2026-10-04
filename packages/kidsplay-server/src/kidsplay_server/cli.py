"""``kidsplay-server`` command-line entry point: backup, restore and store cleanup.

Paths default to the server's configuration (``KIDSPLAY_DB_PATH`` and
``KIDSPLAY_MEDIA_STORE``; see ``kidsplay_server.config``), so inside the Docker
container the commands need no path options::

    docker compose exec kidsplay-server \\
        .venv/bin/kidsplay-server backup /data/backups/kidsplay.tar.zst
"""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import click

from kidsplay_server.backup import BackupError, create_backup, restore_backup
from kidsplay_server.config import settings
from kidsplay_server.store_gc import DEFAULT_GRACE, collect_garbage

_PATH = click.Path(path_type=Path)

_db_option = click.option(
    "--db-path",
    type=_PATH,
    default=lambda: settings.db_path,
    show_default="$KIDSPLAY_DB_PATH",
    help="Server SQLite database.",
)
_media_option = click.option(
    "--media-store",
    type=_PATH,
    default=lambda: settings.media_store_root,
    show_default="$KIDSPLAY_MEDIA_STORE",
    help="Content-addressed media store root.",
)


def _report_missing(missing: list[str], what: str) -> None:
    """Print referenced-but-missing media and exit non-zero if there is any."""
    if not missing:
        return
    for rel in missing:
        click.echo(f"missing: {rel}", err=True)
    raise click.ClickException(
        f"{len(missing)} media file(s) referenced by the database are missing "
        f"from the {what}."
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Log each skipped file.")
def main(verbose: bool) -> None:
    """KidsPlay server administration."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )


@main.command()
@_db_option
@_media_option
@click.argument("dest", type=_PATH)
@click.option(
    "--db-only",
    is_flag=True,
    help="Back up only the database (media can be re-imported; keys cannot).",
)
@click.option(
    "--verify",
    is_flag=True,
    help="Directory backups: re-hash files already in the backup and rewrite "
    "any that are corrupt (slower; catches bit rot).",
)
def backup(
    dest: Path, db_only: bool, verify: bool, db_path: Path, media_store: Path
) -> None:
    """Back up the database and media store to DEST.

    DEST ending in .tar.zst writes one compressed archive. Any other DEST is a
    directory that is updated incrementally: only media it does not already
    have are copied. Safe while the server is running. The backup contains
    device API keys: it is created readable by the owner only.
    """
    try:
        result = create_backup(
            db_path, media_store, dest, db_only=db_only, verify=verify
        )
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    summary = "database"
    if not db_only:
        summary += (
            f" and {result.media_copied} new media file(s)"
            f" ({result.media_already_present} already present,"
            f" {len(result.media_rejected)} incomplete skipped)"
        )
        if result.media_repaired:
            summary += (
                f"; {len(result.media_repaired)} corrupt backup file(s) rewritten"
            )
    click.echo(f"Backed up {summary} -> {dest}")
    _report_missing(result.missing_referenced, "backup")


@main.command()
@_db_option
@_media_option
@click.argument("source", type=_PATH)
@click.option("--force", is_flag=True, help="Restore over a non-empty target.")
def restore(source: Path, force: bool, db_path: Path, media_store: Path) -> None:
    """Restore a backup (a .tar.zst archive or a backup directory).

    Refuses to overwrite a database or media store that already holds data
    unless --force is given. Stop the server before restoring.
    """
    try:
        result = restore_backup(source, db_path, media_store, force=force)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Restored database -> {db_path} and "
        f"{result.media_restored} media file(s) -> {media_store}"
    )
    for rel in result.media_rejected:
        click.echo(f"corrupt, not restored: {rel}", err=True)
    _report_missing(result.missing_referenced, "media store")


@main.command()
@_db_option
@_media_option
@click.option(
    "--grace-days",
    type=click.FloatRange(min=0),
    default=DEFAULT_GRACE.days,
    show_default=True,
    help="Delete a file only after it has stayed unreferenced this long.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be deleted; delete nothing and record nothing.",
)
def gc(grace_days: float, dry_run: bool, db_path: Path, media_store: Path) -> None:
    """Delete media files nothing refers to any more.

    Mostly the superseded normalized copies left by a new loudness target, and
    the files of deleted items. A file is only deleted once it has been
    unreferenced for the grace period, as seen by runs of this command: the
    first run only starts the clock. That keeps `kidsplay-server backup` safe;
    see docs/LOUDNESS.md. Run it while no import is in progress.
    """
    result = asyncio.run(
        collect_garbage(
            db_path,
            media_store,
            grace=timedelta(days=grace_days),
            dry_run=dry_run,
        )
    )
    verb = "would delete" if dry_run else "deleted"
    click.echo(
        f"{result.scanned} media file(s): {result.referenced} in use, "
        f"{verb} {result.deleted} ({result.freed_bytes / 1e6:.1f} MB), "
        f"{result.in_grace} waiting out the {grace_days:g}-day grace period, "
        f"{result.newly_unreferenced} newly unreferenced."
    )
    for rel in result.deleted_paths:
        click.echo(f"  {rel}")
