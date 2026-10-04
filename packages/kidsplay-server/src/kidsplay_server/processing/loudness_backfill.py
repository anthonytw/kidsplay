"""Library-wide loudness backfill, on the import queue.

``LoudnessBackfill`` does not normalize anything itself. ``start`` puts one
loudness job per item on the import queue, and the queue worker
(``queue_worker``) runs them, one ffmpeg job at a time, beside the imports and
the jobs each upload queued. So the backfill shares the queue's behaviour:
progress is kept in the database and survives a restart, and a big library
does not run ffmpeg beside the import worker on a small machine.

``status`` reports a run's progress from the job rows alone, so it is the
same after a restart. A run is a stretch of busy queue: a job created while an
earlier one was still unfinished belongs to the same run, one created after
everything had finished starts a new one. Uploads that normalize while a
backfill runs are therefore part of it.

Typical usage (the API owns one instance per app)::

    backfill = LoudnessBackfill(db_path)
    status = await backfill.start(None)   # the whole library
    status = await backfill.status()
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite

from kidsplay_models import NormalizeStatus, QueueItem
from kidsplay_models.media import MediaType
from kidsplay_models.queue import QueueStatus
from kidsplay_server.database import (
    configure_conn,
    create_loudness_job,
    init_db,
    list_loudness_jobs,
    prune_loudness_jobs,
)
from kidsplay_server.processing import queue_wakeup
from kidsplay_server.processing.pipeline import NormalizeOutcome
from kidsplay_server.processing.queue_worker import (
    LOUDNESS_HISTORY,
    LOUDNESS_OUTCOME_PREFIX,
)

logger = logging.getLogger(__name__)


async def list_audio_media_ids(db_path: Path) -> list[uuid.UUID]:
    """Return the IDs of every music and audiobook item, oldest first.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Media item IDs.
    """
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        async with conn.execute(
            "SELECT id FROM media_items WHERE media_type IN (?, ?) "
            "ORDER BY created_at, id",
            (MediaType.MUSIC.value, MediaType.AUDIOBOOK.value),
        ) as cur:
            rows = await cur.fetchall()
    return [uuid.UUID(r[0]) for r in rows]


class LoudnessBackfill:
    """Queues loudness normalization for existing items and reports progress.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def start(self, media_ids: list[uuid.UUID] | None) -> NormalizeStatus:
        """Queue normalization jobs and return the run's status.

        Items that already have an unfinished job are not queued twice.
        Items already at their type's current target are cheap: the worker
        skips them without decoding anything.

        Args:
            media_ids: Items to normalize, or ``None`` for every music and
                audiobook item.

        Returns:
            The status of the run, including jobs that were already queued.
        """
        ids = (
            await list_audio_media_ids(self._db_path)
            if media_ids is None
            else list(dict.fromkeys(media_ids))
        )
        now = datetime.now()
        async with aiosqlite.connect(self._db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            types = await _media_types(conn, ids)
            for media_id in ids:
                # An unknown ID still gets a job: the worker skips it, and the
                # run counts it as skipped.
                await create_loudness_job(
                    conn, media_id, types.get(media_id, MediaType.MUSIC), created_at=now
                )
            await prune_loudness_jobs(conn, now - LOUDNESS_HISTORY)
            await conn.commit()
        queue_wakeup.wake(self._db_path)
        logger.info("Loudness backfill queued for %d item(s)", len(ids))
        status = await self.status()
        if not ids and not status.running:
            # Nothing to do: report an empty run rather than an older one.
            return NormalizeStatus(started_at=now, finished_at=now)
        return status

    async def status(self) -> NormalizeStatus:
        """Report the progress of the current or last run.

        Returns:
            Counts from the loudness jobs of the run; all zero if there are
            none (jobs are kept for a week).
        """
        async with aiosqlite.connect(self._db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            jobs = await list_loudness_jobs(conn, datetime.now() - LOUDNESS_HISTORY)
        run = _last_run(jobs)
        if not run:
            return NormalizeStatus()

        status = NormalizeStatus(
            total=len(run), started_at=min(j.created_at for j in run)
        )
        for job in run:
            if job.status in (QueueStatus.PENDING, QueueStatus.RUNNING):
                status.running = True
            elif job.status == QueueStatus.FAILED:
                status.failed += 1
                status.add_error(f"{job.media_id}: {job.last_error}")
            elif job.log == f"{LOUDNESS_OUTCOME_PREFIX}{NormalizeOutcome.NORMALIZED}":
                status.normalized += 1
            elif job.log == f"{LOUDNESS_OUTCOME_PREFIX}{NormalizeOutcome.UNCHANGED}":
                status.unchanged += 1
            else:
                status.skipped += 1
        if not status.running:
            status.finished_at = max(
                (j.completed_at for j in run if j.completed_at is not None),
                default=status.started_at,
            )
        return status


def _end(job: QueueItem) -> datetime:
    """When a job stopped occupying the queue (never, while unfinished)."""
    if job.status in (QueueStatus.PENDING, QueueStatus.RUNNING):
        return datetime.max
    return job.completed_at or job.updated_at


def _last_run(jobs: list[QueueItem]) -> list[QueueItem]:
    """Return the jobs of the latest run: the last stretch of busy queue.

    Args:
        jobs: Loudness jobs, oldest first.

    Returns:
        The trailing jobs, each created before the ones ahead of it had all
        finished; empty if there are none.
    """
    run: list[QueueItem] = []
    latest_end = datetime.min
    for job in jobs:
        if run and latest_end < job.created_at:
            run, latest_end = [], datetime.min
        run.append(job)
        latest_end = max(latest_end, _end(job))
    return run


async def _media_types(
    conn: aiosqlite.Connection, ids: list[uuid.UUID]
) -> dict[uuid.UUID, MediaType]:
    """Look up the media type of each existing item in ``ids``."""
    types: dict[uuid.UUID, MediaType] = {}
    # SQLite caps the number of bound variables, so look up in chunks.
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        async with conn.execute(
            f"SELECT id, media_type FROM media_items WHERE id IN ({marks})",
            [str(i) for i in chunk],
        ) as cur:
            for row in await cur.fetchall():
                types[uuid.UUID(row[0])] = MediaType(row[1])
    return types
