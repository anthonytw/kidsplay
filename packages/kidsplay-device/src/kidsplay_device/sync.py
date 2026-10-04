"""Device sync client.

Implements the pull-based sync protocol:

1. ``GET /api/v1/devices/{id}/manifest`` with ``If-None-Match`` caching
2. Store the profile settings and the sync interval from the manifest and
   apply them, so a volume cap or bedtime change never waits behind downloads
3. Delete local files for items removed from the manifest
4. Diff manifest files against local disk and download new/changed files to
   ``media_root``
5. Update local SQLite from manifest media entries
6. Store the theme (its asset files are among the downloads)
7. Store the new manifest hash

The manifest response names the server (``X-KidsPlay-Server-Id``). A device
only syncs from the server it paired with: an answer with another id (or none)
is refused before anything is stored, see ``SyncClient._check_server_identity``.

Profile settings are parsed *separately and leniently* from the rest of the
manifest. A newer server may send a value this device cannot read (a new
``bedtime_mode``, an out-of-range ``max_volume``, a new weekday key) or a
settings ``version`` it does not support; that must not stop media from
syncing. The device then keeps its last good settings, logs a warning and
does not record the manifest hash, so the settings are retried next cycle.

The theme is parsed the same way: a broken or unknown theme is logged and
dropped (the player then shows the default theme), and media syncs regardless.
Its asset files are ordinary entries of the manifest's ``files``, so they are
downloaded, verified and kept like media, and work offline.

Every response's ``Date`` header is recorded as the server's time; the
player uses it to decide whether the local clock can be trusted for bedtime.
With the local transport (server on this machine) the server's clock *is* the
device's clock, so nothing is recorded: it could not confirm the time, and
would wrongly mark a clock restored from the last shutdown as trusted.

With ``sync_transport = "local"`` step 3 links files from the server's media
store instead of downloading them (see ``local_transport``).

``SyncClient`` is designed to run in a background thread via
``asyncio.run(client.run_sync_loop())``.  It never raises — all errors
are caught, logged, and retried at the next interval.
"""

import asyncio
import hashlib
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from kidsplay_models import (
    PROFILE_SETTINGS_VERSION,
    SERVER_ID_HEADER,
    ProfileSettings,
    ThemeDefinition,
    short_server_id,
)
from kidsplay_models.sync import SyncFileEntry, SyncManifest, SyncMediaEntry

from .config import DeviceConfig
from .database import (
    delete_media_item,
    get_all_media_ids,
    get_media_file_paths,
    get_sync_interval,
    get_sync_state,
    init_db,
    set_last_server_time,
    set_profile_settings,
    set_sync_interval,
    set_sync_state,
    set_theme_definition,
    upsert_media_item,
)
from .local_transport import LocalFetchError, fetch_from_store

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3

_SERVER_ID_KEY = "server_id"
"""``sync_state`` key of the server id a device learned by itself."""

_LOCAL_RETRY_SECONDS = 30
"""Retry delay after a failed sync with the local transport. The server is on
this machine and usually still starting when the player boots, so waiting a
whole sync interval would leave a fresh boot without media for 15 minutes."""


class SyncClient:
    """Syncs media from the KidsPlay server to local storage.

    Args:
        config: Device configuration (server URL, credentials, paths).
        http_client: Optional pre-configured ``httpx.AsyncClient``.  If
            provided it is used directly for all requests (useful in tests
            with ``ASGITransport``).  If ``None``, a new client is created
            per sync cycle.
        on_settings: Called (from the sync thread) with the profile settings
            of every new manifest.
        on_server_time: Called (from the sync thread) with the server's
            time, from the ``Date`` header of each manifest response.
        on_theme: Called (from the sync thread) with the profile's theme of
            every new manifest, or None when it has none (or an unreadable
            one).
        on_identity: Called (from the sync thread) with True when the server
            answering is not the one this device paired with, and with False
            when that is resolved. See ``_check_server_identity``.
    """

    def __init__(
        self,
        config: DeviceConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        on_settings: Callable[[ProfileSettings], None] | None = None,
        on_server_time: Callable[[datetime], None] | None = None,
        on_theme: Callable[[ThemeDefinition | None], None] | None = None,
        on_identity: Callable[[bool], None] | None = None,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._on_settings = on_settings
        self._on_server_time = on_server_time
        self._on_theme = on_theme
        self._on_identity = on_identity
        self._identity_bad: bool | None = None
        self._sync_interval: int = config.sync_interval_seconds

    @property
    def sync_interval_seconds(self) -> int:
        """Seconds to wait between syncs.

        The server's value from the last manifest when it sent one, else
        ``config.sync_interval_seconds``.
        """
        return self._sync_interval

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def sync(self) -> None:
        """Run one full sync cycle.

        Fetches the manifest, downloads new files, removes deleted files,
        and updates local SQLite.  All errors are caught and logged so a
        failed sync never crashes the caller.
        """
        await self._sync_once()

    def _delay_after(self, ok: bool) -> float:
        """Seconds to wait before the next sync.

        Args:
            ok: Whether the sync that just ran succeeded.

        Returns:
            The sync interval, or with the local transport after a failure
            (the server is probably still starting) a shorter retry delay.
        """
        if not ok and self._config.is_local:
            return min(self._sync_interval, _LOCAL_RETRY_SECONDS)
        return self._sync_interval

    async def _sync_once(self) -> bool:
        """Run one sync cycle.

        Returns:
            False if the cycle failed or some files could not be fetched
            (already logged), so the caller retries sooner.
        """
        try:
            async with self._client_ctx() as client:
                return await self._run_sync(client)
        except Exception:
            logger.warning("Sync cycle failed", exc_info=True)
            return False

    async def download_file(self, content_hash: str, dest_path: Path) -> None:
        """Download a single file from the server, verified by SHA-256.

        Retries up to 3 times on any failure.  Raises on exhausted retries
        so the caller can decide whether to skip or abort.

        Args:
            content_hash: SHA-256 hex digest; used to build the URL and
                verify the downloaded content.
            dest_path: Local path where the file should be written.

        Raises:
            Exception: After all retry attempts are exhausted.
        """
        async with self._client_ctx() as client:
            await self._do_download(client, content_hash, dest_path)

    def run_sync_loop(self) -> None:
        """Run the sync loop forever in the calling thread.

        Calls ``sync()`` immediately on startup, then every
        ``config.sync_interval_seconds``.  Designed to be passed to
        ``threading.Thread(target=client.run_sync_loop, daemon=True).start()``.
        """

        self._load_sync_interval()

        async def _loop() -> None:
            while True:
                ok = await self._sync_once()
                await asyncio.sleep(self._delay_after(ok))

        asyncio.run(_loop())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_sync_interval(self) -> None:
        """Use the server-sent interval persisted by an earlier sync, if any."""
        try:
            conn = _open_db(self._config)
            try:
                stored = get_sync_interval(conn)
            finally:
                conn.close()
        except Exception:
            logger.warning("Could not read the stored sync interval", exc_info=True)
            return
        if stored is not None:
            self._sync_interval = stored

    def _note_server_time(
        self, conn: sqlite3.Connection, response: httpx.Response
    ) -> None:
        """Record the server's clock from a response's ``Date`` header.

        Does nothing with the local transport: the server is this machine, so
        its ``Date`` is the device's own (possibly restored) clock.
        """
        if self._config.is_local:
            return
        header = response.headers.get("date")
        if not header:
            return
        try:
            server_time = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            logger.debug("Unparseable Date header %r", header)
            return
        if server_time.tzinfo is None:
            return
        set_last_server_time(conn, server_time)
        conn.commit()
        if self._on_server_time is not None:
            self._on_server_time(server_time)

    @asynccontextmanager
    async def _client_ctx(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield an httpx.AsyncClient for making requests.

        If an injected client exists, yields it directly (no lifecycle
        management — the caller owns it).  Otherwise creates a new client
        scoped to this context.
        """
        if self._http_client is not None:
            yield self._http_client
        else:
            async with httpx.AsyncClient(
                base_url=self._config.server_url,
                timeout=30.0,
            ) as client:
                yield client

    def _auth_headers(self) -> dict[str, str]:
        """Bearer header for the device sync endpoints.

        Applied per-request rather than as a client default, because an
        injected client (tests, ASGITransport) is used as-is -- hanging auth
        off client construction meant the injected path sent no credentials
        at all, so the sync tests silently stopped covering authentication
        the moment the server began enforcing it.
        """
        return {"Authorization": f"Bearer {self._config.api_key}"}

    def _check_server_identity(
        self, conn: sqlite3.Connection, resp: httpx.Response
    ) -> bool:
        """Accept the manifest only from the server this device belongs to.

        The id pairing delivered (``config.server_id``) is what counts. A device
        without one (set up by hand, or paired before ids existed) pins the
        first id it sees and holds every later server to it. A server that
        answers with another id, or with none, is refused before anything from
        it is stored: no settings, no clock, no media, no deletions. The device
        keeps playing what it has. Pairing again pins the new server.

        Args:
            conn: The device database.
            resp: The manifest response (200 or 304).

        Returns:
            True if the sync may go on.
        """
        seen = resp.headers.get(SERVER_ID_HEADER)
        pinned = self._config.server_id or get_sync_state(conn, _SERVER_ID_KEY)
        if pinned is None:
            if seen:
                set_sync_state(conn, _SERVER_ID_KEY, seen)
                logger.info("Pinned server identity %s", short_server_id(seen))
            self._report_identity(False)
            return True
        if seen == pinned:
            self._report_identity(False)
            return True
        logger.warning(
            "The server at %s answered as %s, but this player belongs to %s; "
            "not syncing. Pair the player again to use a different server.",
            self._config.server_url,
            short_server_id(seen) if seen else "no identity",
            short_server_id(pinned),
        )
        self._report_identity(True)
        return False

    def _report_identity(self, bad: bool) -> None:
        if bad != self._identity_bad:
            self._identity_bad = bad
            if self._on_identity is not None:
                self._on_identity(bad)

    async def _run_sync(self, client: httpx.AsyncClient) -> bool:
        """Core sync logic.  Raises on unrecoverable errors.

        Args:
            client: Open httpx client to use for all requests.

        Returns:
            True if everything the manifest lists is now on the device (or
            the manifest was unchanged); False if some files could not be
            fetched. Then the manifest hash is not stored, so the next cycle
            refetches the manifest (not a 304) and retries the missing files,
            and media items that need a missing file are not stored yet.
        """
        conn = _open_db(self._config)
        try:
            last_hash = get_sync_state(conn, "last_manifest_hash")

            # Fetch manifest (conditional GET).
            req_headers: dict[str, str] = self._auth_headers()
            if last_hash:
                req_headers["If-None-Match"] = last_hash

            resp = await client.get(
                f"/api/v1/devices/{self._config.device_id}/manifest",
                headers=req_headers,
            )

            if resp.status_code in (200, 304):
                # Before anything from this server is believed or stored.
                if not self._check_server_identity(conn, resp):
                    return False
                self._note_server_time(conn, resp)

            if resp.status_code == 304:
                logger.debug("Manifest unchanged (304) — nothing to sync")
                return True

            resp.raise_for_status()
            payload = resp.json()
            # Settings are validated on their own (see the module docstring);
            # an older server that sends none means "defaults".
            raw_settings: object = ProfileSettings().model_dump(mode="json")
            if isinstance(payload, dict) and "profile_settings" in payload:
                raw_settings = payload.pop("profile_settings")
            # The theme too: whatever it holds must not block media.
            raw_theme = (
                payload.pop("theme", None) if isinstance(payload, dict) else None
            )
            manifest = SyncManifest.model_validate(payload)
            settings = _parse_profile_settings(raw_settings)
            theme = _parse_theme(raw_theme)

            # ---- settings ----------------------------------------------
            # Before any download: a volume cap or bedtime change must not wait
            # behind a large batch of media. Persisted so they apply offline and
            # on the next boot.
            if settings is not None:
                set_profile_settings(conn, settings)
            set_sync_interval(conn, manifest.sync_interval_seconds)
            conn.commit()
            self._sync_interval = (
                manifest.sync_interval_seconds or self._config.sync_interval_seconds
            )
            if settings is not None and self._on_settings is not None:
                self._on_settings(settings)

            # ---- deletions -----------------------------------------------
            manifest_media_ids = {str(e.media_id) for e in manifest.media}
            local_ids = get_all_media_ids(conn)

            for media_id in local_ids - manifest_media_ids:
                paths = get_media_file_paths(conn, media_id)
                for rel_path in paths:
                    _delete_local_file(self._config.media_root, rel_path)
                delete_media_item(conn, media_id)
                logger.debug("Removed media item %s", media_id)

            conn.commit()

            # ---- downloads -----------------------------------------------
            manifest_rel_paths = {e.relative_path for e in manifest.files}
            failed: dict[str, str] = {}  # relative_path -> error
            missing = [
                e
                for e in manifest.files
                if not (self._config.media_root / e.relative_path).exists()
            ]
            store_problem = (
                self._local_store_problem()
                if missing and self._config.is_local
                else None
            )
            if store_problem is not None:
                # One clear error instead of a failure per file.
                logger.error("Cannot sync from the media store: %s", store_problem)
                failed = {e.relative_path: store_problem for e in missing}
            else:
                for file_entry in missing:
                    dest = self._config.media_root / file_entry.relative_path
                    if self._config.is_local:
                        error = self._fetch_local(file_entry, dest)
                    else:
                        error = await self._fetch_http(client, file_entry, dest)
                    if error is not None:
                        failed[file_entry.relative_path] = error
                if failed:
                    first_path, first_error = next(iter(failed.items()))
                    logger.warning(
                        "%d of %d files could not be fetched; will retry. "
                        "First failure: %s: %s",
                        len(failed),
                        len(manifest.files),
                        first_path,
                        first_error,
                    )

            # ---- prune unexpected local files ----------------------------
            _prune_local_files(self._config.media_root, manifest_rel_paths)

            # ---- update metadata ----------------------------------------
            # An item with a file that did not arrive is left out (the player
            # would list something it cannot show or play); the next cycle
            # refetches the manifest and adds it once its files are there.
            for media_entry in manifest.media:
                if failed and not failed.keys().isdisjoint(_entry_paths(media_entry)):
                    logger.debug("Deferring media item %s", media_entry.media_id)
                    continue
                upsert_media_item(conn, media_entry)

            # ---- theme ---------------------------------------------------
            # After the downloads: its assets are among the files.
            set_theme_definition(conn, theme)
            conn.commit()
            if self._on_theme is not None:
                self._on_theme(theme)

            # ---- store new hash -----------------------------------------
            # Not stored when the settings were rejected or a file could not
            # be fetched: the next cycle then refetches the manifest instead
            # of a 304, so settings this device cannot read yet, and missing
            # files, are picked up once they can be.
            if settings is not None and not failed:
                set_sync_state(conn, "last_manifest_hash", manifest.manifest_hash)
            set_sync_state(conn, "last_sync_at", datetime.now().isoformat())
            conn.commit()

            logger.info(
                "Sync complete: %d media items, %d files",
                len(manifest.media),
                len(manifest.files),
            )
            return not failed

        finally:
            conn.close()

    def _local_store_problem(self) -> str | None:
        """Why the server's media store cannot be used, or None if it can."""
        store = self._config.server_media_store
        assert store is not None  # DeviceConfig requires it for "local"
        store = store.expanduser()
        if not store.exists():
            return f"server_media_store {store} does not exist"
        if not store.is_dir():
            return f"server_media_store {store} is not a directory"
        return None

    async def _fetch_http(
        self, client: httpx.AsyncClient, entry: SyncFileEntry, dest: Path
    ) -> str | None:
        """Download one file; return an error message, or None on success."""
        try:
            await self._do_download(client, entry.content_hash, dest)
        except Exception as exc:
            logger.debug(
                "Failed to download %s after %d attempts",
                entry.relative_path,
                _MAX_RETRIES,
                exc_info=True,
            )
            return f"{type(exc).__name__}: {exc}"
        logger.debug("Downloaded %s", entry.relative_path)
        return None

    def _fetch_local(self, entry: SyncFileEntry, dest: Path) -> str | None:
        """Link or copy one file from the media store; never raises.

        Returns:
            An error message, or None on success.
        """
        store = self._config.server_media_store
        assert store is not None  # DeviceConfig requires it for "local"
        try:
            how = fetch_from_store(
                store,
                entry.relative_path,
                entry.content_hash,
                entry.size_bytes,
                dest,
            )
        except LocalFetchError as exc:
            logger.debug(
                "Failed to fetch %s from the media store",
                entry.relative_path,
                exc_info=True,
            )
            return str(exc)
        logger.debug(
            "%s %s", "Linked" if how == "link" else "Copied", entry.relative_path
        )
        return None

    async def _do_download(
        self,
        client: httpx.AsyncClient,
        content_hash: str,
        dest_path: Path,
    ) -> None:
        """Download a file by hash, verify SHA-256, retry up to 3 times.

        Args:
            client: Open httpx client.
            content_hash: Expected SHA-256 hex digest of the file.
            dest_path: Where to write the verified file.

        Raises:
            Exception: After all ``_MAX_RETRIES`` attempts fail.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.get(
                    f"/api/v1/sync/file/{content_hash}",
                    headers=self._auth_headers(),
                )
                resp.raise_for_status()

                actual_hash = hashlib.sha256(resp.content).hexdigest()
                if actual_hash != content_hash:
                    raise ValueError(
                        f"Hash mismatch for {content_hash}: got {actual_hash}"
                    )

                dest_path.parent.mkdir(parents=True, exist_ok=True)
                # Write a new file and rename it into place, never into an
                # existing one: with the local transport dest_path may be a
                # hard link to a media-store file, which must not change.
                part = dest_path.with_name(dest_path.name + ".part")
                part.write_bytes(resp.content)
                os.replace(part, dest_path)
                return

            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    logger.debug(
                        "Download attempt %d/%d failed for %s: %s",
                        attempt + 1,
                        _MAX_RETRIES,
                        content_hash,
                        exc,
                    )

        # _MAX_RETRIES >= 1, so reaching here means at least one attempt failed.
        assert last_exc is not None
        raise last_exc


# ---------------------------------------------------------------------------
# Module-level helpers (not methods so they're easier to test in isolation)
# ---------------------------------------------------------------------------


def _lenient_ui_sounds(raw: object) -> object:
    """Drop a ``ui_sounds`` that is not a boolean, so the default (on) applies.

    A junk value for this nice-to-have must not make the device discard the
    rest of the settings (volume cap, bedtime).

    Args:
        raw: The ``profile_settings`` JSON value from the manifest.

    Returns:
        ``raw``, without a non-boolean ``ui_sounds`` key.
    """
    if isinstance(raw, dict) and not isinstance(raw.get("ui_sounds", True), bool):
        logger.warning(
            "Ignoring invalid ui_sounds %r; button sounds stay on", raw["ui_sounds"]
        )
        return {k: v for k, v in raw.items() if k != "ui_sounds"}
    return raw


def _parse_profile_settings(raw: object) -> ProfileSettings | None:
    """Parse the manifest's profile settings, tolerating unreadable ones.

    Args:
        raw: The ``profile_settings`` JSON value from the manifest.

    Returns:
        The settings, or None (after logging a warning) if they are invalid
        or of a newer ``version`` than this device supports. The caller then
        keeps the previously stored settings.
    """
    if isinstance(raw, dict):
        version = raw.get("version", PROFILE_SETTINGS_VERSION)
        if (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version > PROFILE_SETTINGS_VERSION
        ):
            logger.warning(
                "Profile settings version %d is newer than supported (%d); "
                "keeping the previous settings",
                version,
                PROFILE_SETTINGS_VERSION,
            )
            return None
    raw = _lenient_ui_sounds(raw)
    try:
        return ProfileSettings.model_validate(raw)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        logger.warning(
            "Ignoring unreadable profile settings; keeping the previous settings: %s",
            exc,
        )
        return None


def _parse_theme(raw: object) -> ThemeDefinition | None:
    """Parse the manifest's theme, tolerating an unreadable one.

    Args:
        raw: The ``theme`` JSON value from the manifest (None if absent).

    Returns:
        The theme, or None if there is none or it is invalid (logged). The
        player then shows the built-in theme of the profile's chosen id, or
        the default theme.
    """
    if raw is None:
        return None
    try:
        return ThemeDefinition.model_validate(raw)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        logger.warning("Ignoring unreadable theme; using the default: %s", exc)
        return None


def _entry_paths(entry: SyncMediaEntry) -> set[str]:
    """Every file path a media item refers to."""
    paths = {entry.audio_path, entry.photo_path, *entry.thumbnail_paths.values()}
    return {p for p in paths if p}


def _open_db(config: DeviceConfig) -> sqlite3.Connection:
    """Ensure the DB directory exists and return an initialised connection."""
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    return init_db(config.db_path)


def _delete_local_file(media_root: Path, rel_path: str) -> None:
    """Delete a file under ``media_root``, logging but not raising on error."""
    try:
        (media_root / rel_path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete local file %s", rel_path, exc_info=True)


def _prune_local_files(media_root: Path, keep_paths: set[str]) -> None:
    """Delete any files under ``media_root`` not in ``keep_paths``.

    Walks the entire tree.  Empty directories are left in place; the OS
    filesystem overhead is negligible for our small collections.

    Args:
        media_root: Root of the device's media directory.
        keep_paths: Set of POSIX relative paths the manifest says to keep.
    """
    if not media_root.exists():
        return
    for path in media_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(media_root).as_posix()
        except ValueError:
            continue
        if rel not in keep_paths:
            try:
                path.unlink(missing_ok=True)
                logger.debug("Pruned stale local file %s", rel)
            except OSError:
                logger.warning("Could not prune %s", rel, exc_info=True)
