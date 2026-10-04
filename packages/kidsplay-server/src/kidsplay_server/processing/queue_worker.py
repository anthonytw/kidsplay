"""Background worker for the import queue.

Runs as an asyncio task within the FastAPI lifespan. Polls the
``import_queue`` table for pending jobs and processes them one at a time
with retry logic, running whichever importer each job names (or, for jobs
without one, the first importer that can handle the URL). Every attempt is
logged, including the importer's own debug output, for debugging.

The same queue carries the loudness-normalization jobs (``LOUDNESS_JOB``):
each audio ingest queues one, and so does the library backfill. Running them
here means one ffmpeg normalization at a time however many files were
uploaded, imports going first, and pending work resuming after a restart.
Shutting the worker down kills a running encode and puts its job back to
pending.

Importers see the attempt number, so a rate-limited one can back off further
on each retry. On final failure the job is marked FAILED with the full log
preserved. A job whose importer is not installed, or whose URL needs a plugin that is
not installed, fails at once.
"""

import asyncio
import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from kidsplay_models import QueueItem
from kidsplay_models.queue import LOUDNESS_JOB, QueueStatus
from kidsplay_server.database import (
    claim_next_queue_item,
    configure_conn,
    init_db,
    prune_loudness_jobs,
    update_queue_item,
)
from kidsplay_server.importers import (
    FetchContext,
    Importer,
    ImporterRegistry,
    SourceNeedsPluginError,
)
from kidsplay_server.processing import queue_wakeup
from kidsplay_server.processing.audio import LoudnessConfig, LoudnessError
from kidsplay_server.processing.pipeline import ingest_fetched, normalize_media_item
from kidsplay_server.storage import MediaStore

logger = logging.getLogger(__name__)

# How often to poll for new queue items (seconds).
_POLL_INTERVAL = 10

LOUDNESS_OUTCOME_PREFIX = "outcome: "
"""Start of the log of a finished loudness job; the rest is the
``NormalizeOutcome`` value (read back for the progress counts)."""

LOUDNESS_HISTORY = timedelta(days=7)
"""How long finished loudness jobs are kept."""


def _format_attempt_log(
    attempt: int,
    details: str = "",
    error: str | None = None,
) -> str:
    """Format a single attempt's log entry.

    Args:
        attempt: 1-based attempt number.
        details: Debug output the importer logged during the attempt.
        error: Error message if the attempt failed.

    Returns:
        Formatted log string for this attempt.
    """
    lines: list[str] = []
    sep = "=" * 60
    lines.append(sep)
    lines.append(f"ATTEMPT {attempt} — {datetime.now().isoformat()}")
    lines.append(sep)
    if details.strip():
        lines.append(details.strip())
    if error:
        lines.append(f"ERROR: {error}")
    lines.append("")
    return "\n".join(lines)


def resolve_importer(item: QueueItem, registry: ImporterRegistry) -> Importer | None:
    """Pick the importer for a queue item.

    Args:
        item: The queue item.
        registry: Installed importers.

    Returns:
        The importer named by ``item.importer``, or when the item names none
        (rows queued before importers existed), the first one that can
        handle ``item.url``. ``None`` if no suitable importer is installed.
    """
    if item.importer:
        return registry.get(item.importer)
    return registry.resolve(item.url)


async def _process_queue_item(
    item: QueueItem,
    registry: ImporterRegistry,
    db_path: Path,
    media_store: MediaStore,
    loudness: LoudnessConfig | None = None,
) -> None:
    """Process one claimed queue item: fetch, ingest, and update the DB.

    On failure, either re-queues as pending (if retries remain) or
    marks as failed. A missing importer fails the item without retries,
    since retrying cannot help until the plugin is installed.

    Args:
        item: The claimed item (status RUNNING, attempt already incremented).
        registry: Installed importers.
        db_path: Path to SQLite database.
        media_store: Content-addressed media store.
        loudness: Loudness normalization settings for audio.
    """
    if item.importer == LOUDNESS_JOB:
        await _process_loudness_item(item, db_path, media_store, loudness)
        return
    log = item.log
    error_msg: str | None = None
    importer: Importer | None = None
    try:
        registry.require_plugin(item.url)
    except SourceNeedsPluginError as exc:
        # Would otherwise resolve to the ``http`` importer, download a web
        # page and fail on every retry (including jobs queued by older code).
        error_msg = str(exc)
    else:
        importer = resolve_importer(item, registry)
        if importer is None:
            error_msg = (
                f"Importer {item.importer!r} is not installed"
                if item.importer
                else f"No installed importer can handle {item.url}"
            )
    if importer is None:
        logger.error("Queue item %s: %s", item.id, error_msg)
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await update_queue_item(
                conn,
                item.id,
                status=QueueStatus.FAILED,
                last_error=error_msg,
                log=log + _format_attempt_log(item.attempt, error=error_msg),
                completed_at=datetime.now(),
            )
            await conn.commit()
        return

    ctx = FetchContext(attempt=item.attempt, queued=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            items = await importer.fetch(item.url, Path(tmp), ctx)
            if not items:
                raise RuntimeError(f"{importer.label} importer fetched nothing")
            batch = await ingest_fetched(
                items,
                item.media_type,
                db_path,
                media_store,
                playlist_title=item.playlist_title,
                profile_ids=item.profile_ids,
                title_override=item.title_override,
                artist_override=item.artist_override,
                loudness=loudness,
            )
            if batch.failed:
                errors = [e for r in batch.results for e in r.errors]
                raise RuntimeError(f"Ingest failed: {errors}")

            log += _format_attempt_log(item.attempt, ctx.text)
            # Success — update queue item.
            async with aiosqlite.connect(db_path) as conn:
                await configure_conn(conn)
                await update_queue_item(
                    conn,
                    item.id,
                    status=QueueStatus.COMPLETED,
                    log=log,
                    media_id=batch.results[0].media_id,
                    completed_at=datetime.now(),
                )
                await conn.commit()

            logger.info(
                "Queue item %s completed (attempt %d): %s",
                item.id,
                item.attempt,
                item.url,
            )

    except Exception as exc:
        error_msg = str(exc)
        log += _format_attempt_log(item.attempt, ctx.text, error=error_msg)
        logger.warning(
            "Queue item %s attempt %d failed: %s",
            item.id,
            item.attempt,
            error_msg,
        )

        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            if item.attempt >= item.max_retries:
                await update_queue_item(
                    conn,
                    item.id,
                    status=QueueStatus.FAILED,
                    last_error=error_msg,
                    log=log,
                    completed_at=datetime.now(),
                )
                logger.error(
                    "Queue item %s exhausted all %d retries: %s",
                    item.id,
                    item.max_retries,
                    item.url,
                )
            else:
                # Re-queue for next attempt.
                await update_queue_item(
                    conn,
                    item.id,
                    status=QueueStatus.PENDING,
                    last_error=error_msg,
                    log=log,
                )
            await conn.commit()


async def _process_loudness_item(
    item: QueueItem,
    db_path: Path,
    media_store: MediaStore,
    loudness: LoudnessConfig | None,
) -> None:
    """Process one claimed loudness job: normalize its media item.

    A failed normalization (ffmpeg missing or failing, the stored audio
    missing) is retried until ``max_retries``; the item stays playable and
    "not normalized" either way. If the worker is cancelled mid-encode, the
    encode is killed and the job goes back to pending without using up an
    attempt, so it resumes at the next start.

    Args:
        item: The claimed job (status RUNNING, attempt already incremented).
        db_path: Path to SQLite database.
        media_store: Content-addressed media store.
        loudness: Loudness settings (the server settings if ``None``).
    """
    if item.media_id is None:
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await update_queue_item(
                conn,
                item.id,
                status=QueueStatus.FAILED,
                last_error="Loudness job has no media item",
                completed_at=datetime.now(),
            )
            await conn.commit()
        return
    try:
        outcome = await normalize_media_item(
            item.media_id, db_path, media_store, loudness
        )
    except asyncio.CancelledError:
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await update_queue_item(
                conn,
                item.id,
                status=QueueStatus.PENDING,
                attempt=max(item.attempt - 1, 0),
            )
            await conn.commit()
        raise
    except Exception as exc:
        # LoudnessError is the expected failure; anything else (a database
        # error) is retried the same way and logged with its traceback.
        error_msg = str(exc)
        if isinstance(exc, LoudnessError):
            logger.warning(
                "Loudness job %s attempt %d failed: %s",
                item.id,
                item.attempt,
                error_msg,
            )
        else:
            logger.exception("Loudness job %s crashed", item.id)
        final = item.attempt >= item.max_retries
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await update_queue_item(
                conn,
                item.id,
                status=QueueStatus.FAILED if final else QueueStatus.PENDING,
                last_error=error_msg,
                log=_format_attempt_log(item.attempt, error=error_msg),
                completed_at=datetime.now() if final else None,
            )
            await conn.commit()
        return
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await update_queue_item(
            conn,
            item.id,
            status=QueueStatus.COMPLETED,
            log=f"{LOUDNESS_OUTCOME_PREFIX}{outcome.value}",
            completed_at=datetime.now(),
        )
        await conn.commit()


async def drain_queue(
    db_path: Path,
    media_store: MediaStore,
    registry: ImporterRegistry,
    *,
    loudness: LoudnessConfig | None = None,
) -> int:
    """Process every pending job now, then return.

    What the worker does in the background, for callers that want the queue
    empty before they go on: the demo's seeding and the tests.

    Args:
        db_path: Path to SQLite database.
        media_store: Content-addressed media store.
        registry: Installed importers.
        loudness: Loudness settings for ingest and normalization.

    Returns:
        The number of jobs processed (a retried job counts each attempt).
    """
    processed = 0
    while True:
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            item = await claim_next_queue_item(conn)
        if item is None:
            return processed
        await _process_queue_item(item, registry, db_path, media_store, loudness)
        processed += 1


async def run_queue_worker(
    db_path: Path,
    media_store: MediaStore,
    registry: ImporterRegistry,
    *,
    shutdown_event: asyncio.Event | None = None,
    loudness: LoudnessConfig | None = None,
) -> None:
    """Main loop for the import queue worker.

    Polls the database for pending jobs and processes them sequentially.
    Runs until the ``shutdown_event`` is set or the task is cancelled.

    Args:
        db_path: Path to SQLite database.
        media_store: Content-addressed media store.
        registry: Installed importers.
        shutdown_event: Optional event to signal graceful shutdown.
        loudness: Loudness normalization settings for audio.
    """
    logger.info("Import queue worker started")
    stop = shutdown_event or asyncio.Event()

    # Reset any items stuck in RUNNING state from a prior crash.
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        async with conn.execute(
            "SELECT COUNT(*) FROM import_queue WHERE status = 'running'"
        ) as cur:
            row = await cur.fetchone()
        stuck = int(row[0]) if row else 0
        if stuck:
            logger.warning(
                "Resetting %d stuck RUNNING queue item(s) to PENDING",
                stuck,
            )
        await conn.execute(
            "UPDATE import_queue SET status = 'pending' WHERE status = 'running'"
        )
        await prune_loudness_jobs(conn, datetime.now() - LOUDNESS_HISTORY)
        await conn.commit()

    wake = queue_wakeup.register(db_path)
    try:
        while not stop.is_set():
            try:
                # Cleared before claiming, so a job queued from here on is
                # either claimed now or leaves the event set.
                wake.clear()
                async with aiosqlite.connect(db_path) as conn:
                    await configure_conn(conn)
                    item = await claim_next_queue_item(conn)

                if item is None:
                    if await _sleep_until_work(stop, wake):
                        break  # shutdown signalled
                    continue

                logger.info(
                    "Processing queue item %s (attempt %d/%d, importer=%s): %s",
                    item.id,
                    item.attempt,
                    item.max_retries,
                    item.importer or "auto",
                    item.url,
                )

                await _process_queue_item(
                    item, registry, db_path, media_store, loudness
                )

            except asyncio.CancelledError:
                logger.info("Import queue worker cancelled")
                raise
            except Exception:
                logger.exception("Unexpected error in queue worker")
                # Sleep before retrying the main loop on unexpected errors.
                if await _sleep_until_work(stop, wake):
                    break
    finally:
        queue_wakeup.unregister(db_path, wake)

    logger.info("Import queue worker stopped")


async def _sleep_until_work(stop: asyncio.Event, wake: asyncio.Event) -> bool:
    """Sleep until a job is queued, shutdown is signalled, or the poll is due.

    Args:
        stop: Set to shut the worker down.
        wake: Set by ``queue_wakeup.wake`` when a job is queued.

    Returns:
        True if shutdown was signalled.
    """
    waiting = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(wake.wait())]
    try:
        await asyncio.wait(
            waiting, timeout=_POLL_INTERVAL, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in waiting:
            task.cancel()
    return stop.is_set()
