"""Loudness normalization on the import queue.

Uploads store the file and queue a job; the queue worker runs the jobs one at a
time, resumes them after a restart and stops a running encode at shutdown.
Most tests use real ffmpeg on generated tones; the concurrency and shutdown
tests stub the encoder so they can control its timing.
"""

import asyncio
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from kidsplay_models import ProcessedFile, QueueItem
from kidsplay_models.media import MediaType
from kidsplay_models.processing import ProcessingStatus
from kidsplay_models.queue import LOUDNESS_JOB, QueueStatus
from kidsplay_server.database import (
    claim_next_queue_item,
    configure_conn,
    create_loudness_job,
    create_queue_item,
    get_queue_item,
    init_db,
    list_normalizing_media_ids,
    list_processed_files,
    list_queue_items,
    prune_loudness_jobs,
    update_queue_item,
)
from kidsplay_server.importers import ImporterRegistry
from kidsplay_server.processing import pipeline
from kidsplay_server.processing import queue_worker as worker_module
from kidsplay_server.processing.audio import LoudnessConfig, LoudnessError
from kidsplay_server.processing.pipeline import NormalizeOutcome, ingest_file
from kidsplay_server.processing.queue_worker import (
    LOUDNESS_OUTCOME_PREFIX,
    drain_queue,
    run_queue_worker,
)
from kidsplay_server.processing.resources import JobCancelledError, run_limited
from kidsplay_server.storage import MediaStore

MakeTone = Callable[..., Path]  # the ``make_tone`` fixture from conftest.py

ON = LoudnessConfig()
OFF = LoudnessConfig(enabled=False)
NO_IMPORTERS = ImporterRegistry([])


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


async def _ingest(
    path: Path, db_path: Path, store: MediaStore, loudness: LoudnessConfig = ON
) -> uuid.UUID:
    result = await ingest_file(path, MediaType.MUSIC, db_path, store, loudness=loudness)
    assert result.processing_status == ProcessingStatus.READY, result.errors
    assert result.media_id is not None
    return result.media_id


async def _loudness_jobs(db_path: Path) -> list[QueueItem]:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        jobs = await list_queue_items(conn, include_loudness=True)
    return [j for j in jobs if j.importer == LOUDNESS_JOB]


async def _files(db_path: Path, media_id: uuid.UUID) -> dict[str, ProcessedFile]:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        return {pf.file_type: pf for pf in await list_processed_files(conn, media_id)}


async def _queue_jobs(db_path: Path, count: int) -> list[uuid.UUID]:
    """Queue ``count`` loudness jobs for made-up items; return their IDs."""
    ids = [uuid.uuid4() for _ in range(count)]
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        for media_id in ids:
            await create_loudness_job(conn, media_id, MediaType.MUSIC)
        await conn.commit()
    return ids


async def _wait_for(condition: Callable[[], object], seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


# ---------------------------------------------------------------------------
# Ingest queues the job and does not encode
# ---------------------------------------------------------------------------


class TestIngestQueuesTheJob:
    async def test_job_is_queued_with_the_item_and_ffmpeg_does_not_run(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("ingest must not encode")

        monkeypatch.setattr(pipeline, "normalize_loudness", boom)
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store)

        # Playable at once: the stored original is the audio file.
        assert set(await _files(db_path, media_id)) == {"audio"}
        (job,) = await _loudness_jobs(db_path)
        assert (job.media_id, job.status) == (media_id, QueueStatus.PENDING)
        assert job.url == f"loudness:{media_id}"
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            assert await list_normalizing_media_ids(conn) == {media_id}

    async def test_job_is_not_listed_as_an_import(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        await _ingest(make_tone("t.mp3", -18), db_path, store)
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            assert await list_queue_items(conn) == []
            assert len(await list_queue_items(conn, include_loudness=True)) == 1

    async def test_nothing_is_queued_when_disabled(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        await _ingest(make_tone("t.mp3", -18), db_path, store, OFF)
        assert await _loudness_jobs(db_path) == []

    async def test_ingest_reads_the_switch_when_given_no_config(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """What ingest itself decides from the settings is whether to queue the
        job at all (the ``KIDSPLAY_LOUDNORM`` switch); the target is read later,
        by the job. Called without a config, it resolves the real one."""
        monkeypatch.setenv("KIDSPLAY_LOUDNORM", "disabled")
        result = await ingest_file(
            make_tone("t.mp3", -18), MediaType.MUSIC, db_path, store
        )
        assert result.processing_status == ProcessingStatus.READY, result.errors
        assert await _loudness_jobs(db_path) == []

    async def test_nothing_is_queued_without_ffmpeg(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tone = make_tone("t.mp3", -18)
        monkeypatch.setenv("PATH", "")
        await _ingest(tone, db_path, store)
        assert await _loudness_jobs(db_path) == []

    async def test_photos_queue_nothing(
        self, db_path: Path, store: MediaStore, tmp_path: Path
    ) -> None:
        from PIL import Image

        photo = tmp_path / "p.png"
        Image.new("RGB", (8, 8)).save(photo)
        result = await ingest_file(photo, MediaType.PHOTO, db_path, store, loudness=ON)
        assert result.processing_status == ProcessingStatus.READY
        assert await _loudness_jobs(db_path) == []

    async def test_duplicate_upload_queues_no_second_job(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        tone = make_tone("t.mp3", -18)
        await _ingest(tone, db_path, store)
        again = await ingest_file(tone, MediaType.MUSIC, db_path, store, loudness=ON)
        assert again.skipped
        assert len(await _loudness_jobs(db_path)) == 1

    async def test_an_item_is_not_queued_twice(self, db_path: Path) -> None:
        (media_id,) = await _queue_jobs(db_path, 1)
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            assert await create_loudness_job(conn, media_id, MediaType.MUSIC) is None
            # A finished job does not block a new one.
            (job,) = await list_queue_items(conn, include_loudness=True)
            await update_queue_item(conn, job.id, status=QueueStatus.COMPLETED)
            assert await create_loudness_job(conn, media_id, MediaType.MUSIC)


# ---------------------------------------------------------------------------
# The worker runs the jobs
# ---------------------------------------------------------------------------


class TestWorkerRunsJobs:
    async def test_drain_normalizes_and_records_the_outcome(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store)
        assert await drain_queue(db_path, store, NO_IMPORTERS, loudness=ON) == 1

        assert set(await _files(db_path, media_id)) == {"audio", "audio_source"}
        (job,) = await _loudness_jobs(db_path)
        assert job.status == QueueStatus.COMPLETED
        assert job.log == f"{LOUDNESS_OUTCOME_PREFIX}{NormalizeOutcome.NORMALIZED}"
        assert job.completed_at is not None

    async def test_at_most_one_normalization_at_a_time(
        self, db_path: Path, store: MediaStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Acceptance: on a single worker, jobs never overlap."""
        await _queue_jobs(db_path, 4)
        running = peak = calls = 0

        async def fake(media_id: uuid.UUID, *args: object) -> NormalizeOutcome:
            nonlocal running, peak, calls
            running += 1
            calls += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return NormalizeOutcome.NORMALIZED

        monkeypatch.setattr(worker_module, "normalize_media_item", fake)
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_queue_worker(db_path, store, NO_IMPORTERS, shutdown_event=stop)
        )
        try:
            await _wait_for(lambda: _all_done(db_path), seconds=10)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert (calls, peak) == (4, 1)

    async def test_imports_go_before_normalization(self, db_path: Path) -> None:
        """A link a parent just added does not wait behind a library backfill."""
        old = datetime.now() - timedelta(minutes=5)
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            await create_loudness_job(
                conn, uuid.uuid4(), MediaType.MUSIC, created_at=old
            )
            legacy = QueueItem(
                url="fake://x", media_type=MediaType.MUSIC, playlist_title="P"
            )  # no importer, as rows queued before importers existed
            await create_queue_item(conn, legacy)
            await conn.commit()
            first = await claim_next_queue_item(conn)
            second = await claim_next_queue_item(conn)
        assert first is not None and first.id == legacy.id
        assert second is not None and second.importer == LOUDNESS_JOB

    async def test_pending_normalization_resumes_after_a_restart(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        """Acceptance: a job left RUNNING by a crash runs when the server is
        back."""
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store)
        (job,) = await _loudness_jobs(db_path)
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await update_queue_item(conn, job.id, status=QueueStatus.RUNNING, attempt=1)
            await conn.commit()

        stop = asyncio.Event()
        task = asyncio.create_task(
            run_queue_worker(
                db_path, store, NO_IMPORTERS, shutdown_event=stop, loudness=ON
            )
        )
        try:
            await _wait_for(lambda: _all_done(db_path))
        finally:
            stop.set()
            await asyncio.wait_for(task, 10)
        assert set(await _files(db_path, media_id)) == {"audio", "audio_source"}

    async def test_job_queued_while_the_worker_sleeps_starts_at_once(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        """No waiting for the poll: ingest wakes the worker."""
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_queue_worker(
                db_path, store, NO_IMPORTERS, shutdown_event=stop, loudness=ON
            )
        )
        try:
            await asyncio.sleep(0.5)  # the worker finds the queue empty and sleeps
            started = time.monotonic()
            media_id = await _ingest(make_tone("t.mp3", -18), db_path, store)
            await _wait_for(lambda: _all_done(db_path), 8)
            assert time.monotonic() - started < 8  # the poll interval is 10 s
        finally:
            stop.set()
            await asyncio.wait_for(task, 10)
        assert "audio_source" in await _files(db_path, media_id)

    async def test_failure_retries_then_fails_and_the_item_stays_playable(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store)
        attempts: list[int] = []

        async def fail(media_id: uuid.UUID, *args: object) -> NormalizeOutcome:
            attempts.append(1)
            raise LoudnessError("ffmpeg exited with 1: boom")

        monkeypatch.setattr(worker_module, "normalize_media_item", fail)
        await drain_queue(db_path, store, NO_IMPORTERS, loudness=ON)

        (job,) = await _loudness_jobs(db_path)
        assert job.status == QueueStatus.FAILED
        assert len(attempts) == job.attempt == job.max_retries == 2
        assert job.last_error == "ffmpeg exited with 1: boom"
        assert set(await _files(db_path, media_id)) == {"audio"}

    async def test_unexpected_error_is_retried_like_any_other(
        self, db_path: Path, store: MediaStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _queue_jobs(db_path, 1)
        calls = []

        async def flaky(media_id: uuid.UUID, *args: object) -> NormalizeOutcome:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return NormalizeOutcome.NORMALIZED

        monkeypatch.setattr(worker_module, "normalize_media_item", flaky)
        await drain_queue(db_path, store, NO_IMPORTERS, loudness=ON)
        (job,) = await _loudness_jobs(db_path)
        assert job.status == QueueStatus.COMPLETED and job.attempt == 2

    async def test_item_deleted_before_its_turn_is_skipped(
        self, db_path: Path, store: MediaStore
    ) -> None:
        await _queue_jobs(db_path, 1)
        await drain_queue(db_path, store, NO_IMPORTERS, loudness=ON)
        (job,) = await _loudness_jobs(db_path)
        assert job.status == QueueStatus.COMPLETED
        assert job.log == f"{LOUDNESS_OUTCOME_PREFIX}{NormalizeOutcome.SKIPPED}"

    async def test_job_without_a_media_item_fails(self, db_path: Path) -> None:
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            item = QueueItem(
                url="loudness:none",
                importer=LOUDNESS_JOB,
                media_type=MediaType.MUSIC,
                playlist_title="",
                status=QueueStatus.RUNNING,
                attempt=1,
            )
            await create_queue_item(conn, item)
            await conn.commit()
        await worker_module._process_loudness_item(
            item, db_path, MediaStore(db_path.parent), ON
        )
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            failed = await get_queue_item(conn, item.id)
        assert failed is not None and failed.status == QueueStatus.FAILED

    async def test_finished_jobs_are_pruned_after_a_week(self, db_path: Path) -> None:
        await _queue_jobs(db_path, 2)
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            old, fresh = await list_queue_items(conn, include_loudness=True)
            await update_queue_item(
                conn,
                old.id,
                status=QueueStatus.COMPLETED,
                completed_at=datetime.now() - timedelta(days=8),
            )
            await update_queue_item(
                conn,
                fresh.id,
                status=QueueStatus.COMPLETED,
                completed_at=datetime.now(),
            )
            assert (
                await prune_loudness_jobs(conn, datetime.now() - timedelta(days=7)) == 1
            )
            remaining = await list_queue_items(conn, include_loudness=True)
        assert [j.id for j in remaining] == [fresh.id]

    async def test_unfinished_jobs_are_never_pruned(self, db_path: Path) -> None:
        await _queue_jobs(db_path, 1)
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            assert (
                await prune_loudness_jobs(conn, datetime.now() + timedelta(days=1)) == 0
            )


async def _all_done(db_path: Path) -> bool:
    jobs = await _loudness_jobs(db_path)
    return bool(jobs) and all(
        j.status in (QueueStatus.COMPLETED, QueueStatus.FAILED) for j in jobs
    )


# ---------------------------------------------------------------------------
# Shutdown does not wait for an encode
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_shutdown_stops_the_encode_and_requeues_the_job(
        self, db_path: Path, store: MediaStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (media_id,) = await _queue_jobs(db_path, 1)
        started = threading.Event()
        saw_cancel = threading.Event()

        async def encoding(*args: object, **kwargs: object) -> NormalizeOutcome:
            # Stands in for normalize_media_item awaiting the encode thread.
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                saw_cancel.set()
                raise
            return NormalizeOutcome.NORMALIZED

        monkeypatch.setattr(worker_module, "normalize_media_item", encoding)
        task = asyncio.create_task(run_queue_worker(db_path, store, NO_IMPORTERS))
        await _wait_for(started.is_set)

        begun = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert time.monotonic() - begun < 2
        assert saw_cancel.is_set()

        (job,) = await _loudness_jobs(db_path)
        assert job.media_id == media_id
        # Back to pending, and the interrupted attempt does not count.
        assert (job.status, job.attempt) == (QueueStatus.PENDING, 0)

    async def test_a_real_encode_is_killed_promptly(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        """Shutdown used to sit through the whole ffmpeg run (minutes, for an
        audiobook)."""
        long_tone = make_tone("audiobook-long.mp3", -18, seconds=1200)
        await _ingest(long_tone, db_path, store)
        task = asyncio.create_task(
            run_queue_worker(db_path, store, NO_IMPORTERS, loudness=ON)
        )

        def ffmpeg_running() -> bool:
            listing = subprocess.run(
                ["pgrep", "-f", f"ffmpeg.*{store.root}"],
                capture_output=True,
                check=False,
            )
            return listing.returncode == 0

        await _wait_for(ffmpeg_running)
        begun = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert time.monotonic() - begun < 3

        # The killed ffmpeg is gone, and the job is waiting for the next start.
        await _wait_for(lambda: not ffmpeg_running(), seconds=5)
        (job,) = await _loudness_jobs(db_path)
        assert (job.status, job.attempt) == (QueueStatus.PENDING, 0)

    async def test_cancelled_normalization_leaves_no_child_running(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """uvicorn exits right after cancelling the worker; the child must
        already be dead then, not at the worker thread's next poll (an orphan
        would run beside the next start's ffmpeg)."""
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store, OFF)
        pid_file = tmp_path / "child.pid"

        def child(*args: object, cancel: threading.Event, **kwargs: object) -> None:
            run_limited(
                ["sh", "-c", f"echo $$ > {pid_file}; exec sleep 60"], cancel=cancel
            )

        monkeypatch.setattr(pipeline, "normalize_loudness", child)
        task = asyncio.create_task(
            pipeline.normalize_media_item(media_id, db_path, store, ON)
        )
        await _wait_for(lambda: pid_file.exists() and pid_file.read_text().strip())
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # No waiting: the process must be gone the moment the task is.
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_cancelling_normalize_media_item_raises_and_stores_nothing(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store, OFF)
        entered = threading.Event()

        def slow(*args: object, cancel: threading.Event, **kwargs: object) -> None:
            entered.set()
            while not cancel.wait(0.01):
                pass
            raise JobCancelledError("cancelled")

        monkeypatch.setattr(pipeline, "normalize_loudness", slow)
        before = sorted(p for p in store.root.rglob("*") if p.is_file())
        task = asyncio.create_task(
            pipeline.normalize_media_item(media_id, db_path, store, ON)
        )
        await _wait_for(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sorted(p for p in store.root.rglob("*") if p.is_file()) == before
        assert set(await _files(db_path, media_id)) == {"audio"}
