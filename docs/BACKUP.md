# Server backup and restore

`kidsplay-server backup` and `kidsplay-server restore` protect the server
against a lost disk. They are installed with the server package
(`uv sync --all-packages`) and read the same `KIDSPLAY_DB_PATH` and
`KIDSPLAY_MEDIA_STORE` settings as the server. Override them with `--db-path`
and `--media-store`.

> **A backup contains credentials.** Every device's API key is stored in
> plaintext in the database, and so in every backup. Archives are created with
> `0600` permissions; a new backup directory gets `0700`, and its database
> file `0600`. Keep it that way: store backups where only the server's
> administrator can read them, and encrypt them before they leave the machine
> (for example to an off-site or cloud copy).

## What is in a backup

| Included | Not included |
|---|---|
| The whole server database, via the SQLite backup API: profiles, devices and their API keys, media metadata, profile assignments, the import queue, and any table added in future versions | Device-side state. Back up each handheld's identity (`~/.kidsplay/config.json`) with `just device-backup-config HOST`; the device re-syncs its media from the server after a restore |
| The processed media store (audio, resized photos, thumbnails), unless you pass `--db-only` | Your original source library (the files you imported from) |
| | Server settings: environment variables, `docker/.env`, yt-dlp cookies |
| | Logs |

`--db-only` backs up the database alone. It is small and fast, and it holds
the things you cannot recreate: device keys and assignments. Media can be
re-imported from the originals, but every re-imported item gets a new ID, so
you would have to assign it to profiles again.

## Backing up

```bash
# One self-contained archive (zstd-compressed tar):
uv run kidsplay-server backup /mnt/backup/kidsplay-$(date +%F).tar.zst

# Incremental: any target not ending in .tar.zst is a directory.
uv run kidsplay-server backup /mnt/backup/kidsplay

# ...and re-hash the files already there, to catch bit rot (reads it all):
uv run kidsplay-server backup --verify /mnt/backup/kidsplay

# Database only:
uv run kidsplay-server backup --db-only /mnt/backup/kidsplay-db-$(date +%F).tar.zst
```

Backups are **safe while the server is running**, including during an import:

- The database is copied first, with SQLite's online backup API, so the copy
  shows one committed state.
- The ingest pipeline finishes writing a media file before it commits the row
  that points to it, and never changes a stored file afterwards. Every file
  the database copy references was therefore complete before the backup
  started.
- Each media file is then checked against the SHA-256 hash in its name. A file
  that is still being written fails that check and is left out, and no row in
  the database copy points to it.

**Cleaning up the store** (`kidsplay-server gc`, see
[LOUDNESS.md](LOUDNESS.md#cleaning-up-superseded-files)) is the one operation
that deletes media files, so it is built around this promise: it only deletes a
file that has been unreferenced for a whole grace period (seven days by
default). A backup's database copy can only reference a file that was in use
when the backup started, so any backup that finishes within the grace period
finds all its files. Keep the grace period longer than your longest backup.
An incremental backup directory keeps files the store no longer has; it only
ever adds.

**Directory targets are incremental.** The store is content-addressed, so a
file that is already in the backup directory never needs copying again. Each
run copies only new media, then replaces `kidsplay.db` with a fresh snapshot.
The directory always holds the latest state. By default a file that is already
there is trusted without being read; run with `--verify` now and then (say
monthly) to re-hash them all and rewrite any that no longer match the store.

A target name that looks like another archive format (`.tar`, `.tar.gz`,
`.tgz`, `.zst`, ...) is refused rather than silently made a directory: only
`.tar.zst` archives are supported. If you want history, snapshot the
directory with your filesystem or backup tool, or keep dated archives as well.

Put backups on a **different disk** from the server's data. A backup on the
same disk is lost together with it.

Exit status: `0` means the backup is complete. `1` means it failed, or the
database references media files that could not be backed up (they are missing
or corrupt in the store). The missing files are listed on stderr. Use `-v` to
also log files that were skipped as incomplete.

Restoring checks the backup's database with SQLite's `quick_check` before
touching anything, so a damaged database aborts the restore with the target
unchanged. The database file (the server's own and a restored one) is kept at
mode `0600`, because it holds device API keys.

Custom theme images, fonts and sounds live in the media store and are covered
by the same completeness check as photos and audio.

### Scheduling: cron

```cron
# m h dom mon dow  command
30 3 * * *  cd /opt/kidsplay && KIDSPLAY_DB_PATH=/srv/kidsplay/db.sqlite KIDSPLAY_MEDIA_STORE=/srv/kidsplay/media /usr/local/bin/uv run kidsplay-server backup /mnt/backup/kidsplay
```

### Scheduling: systemd timer

`/etc/systemd/system/kidsplay-backup.service`:

```ini
[Unit]
Description=KidsPlay server backup

[Service]
Type=oneshot
User=kidsplay
WorkingDirectory=/opt/kidsplay
Environment=KIDSPLAY_DB_PATH=/srv/kidsplay/db.sqlite
Environment=KIDSPLAY_MEDIA_STORE=/srv/kidsplay/media
ExecStart=/usr/local/bin/uv run kidsplay-server backup /mnt/backup/kidsplay
```

`/etc/systemd/system/kidsplay-backup.timer`:

```ini
[Unit]
Description=Nightly KidsPlay server backup

[Timer]
OnCalendar=*-*-* 03:30
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kidsplay-backup.timer
systemctl list-timers kidsplay-backup.timer   # next run
journalctl -u kidsplay-backup.service         # last result
```

A failed or incomplete backup leaves the unit in the `failed` state.

## Restoring

Stop the server first, so that nothing writes to the database while it is
being replaced.

```bash
uv run kidsplay-server restore /mnt/backup/kidsplay-2026-09-29.tar.zst
# or from an incremental directory:
uv run kidsplay-server restore /mnt/backup/kidsplay
```

The restore writes media files first and the database last. Each media file is
checked against its hash before it is placed.

Restore **refuses a target that already holds data**, meaning a database with
any rows, or any file in the media store. A server that has started once, and
so has an empty schema, counts as empty. To replace existing data, pass
`--force`. The database is then replaced completely. Media files already in the
store are kept, because the store is content-addressed and extra files are
harmless.

After a restore, devices sync with their existing API keys. There is nothing to
redo on the handhelds.

## Docker

`docker/docker-compose.yml` mounts `${KIDSPLAY_BACKUP_DIR:-./data/backups}` at
`/data/backups` in the container. Set `KIDSPLAY_BACKUP_DIR` in `docker/.env`
to a directory on another disk or NAS share.

Back up the running server:

```bash
docker compose -f docker/docker-compose.yml exec kidsplay-server \
    uv run --no-sync kidsplay-server backup /data/backups/kidsplay-$(date +%F).tar.zst

# incremental:
docker compose -f docker/docker-compose.yml exec kidsplay-server \
    uv run --no-sync kidsplay-server backup /data/backups/kidsplay
```

A host cron entry for the same job (`exec -T`, because cron has no TTY):

```cron
30 3 * * *  cd /opt/kidsplay && docker compose -f docker/docker-compose.yml exec -T kidsplay-server uv run --no-sync kidsplay-server backup /data/backups/kidsplay
```

Restore with the server stopped, in a one-off container that uses the same
volumes:

```bash
docker compose -f docker/docker-compose.yml stop
docker compose -f docker/docker-compose.yml run --rm --entrypoint uv kidsplay-server \
    run --no-sync kidsplay-server restore /data/backups/kidsplay-2026-09-29.tar.zst
docker compose -f docker/docker-compose.yml up -d
```

The container runs as root, so its backups are owned by root with mode `0600`
on the host.

## Archive format

A `.tar.zst` archive holds `media/<store path>` for each media file, then
`kidsplay.db` (the database snapshot), then `manifest.json` (format version,
creation time, `db_only`, file count). A directory backup has the same layout,
unpacked. Restore requires the manifest, so it rejects a truncated archive
before it touches the database.
