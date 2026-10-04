"""Tests for kidsplay_server.processing.loudness_backfill.

The backfill only queues loudness jobs on the import queue and reports their
progress; the queue worker runs them. The scenarios run end to end with real
ffmpeg on generated tones, with ``drain_queue`` standing in for the worker;
failure accounting uses a stubbed ``normalize_media_item``.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from kidsplay_models.media import MediaType
from kidsplay_models.queue import LOUDNESS_JOB, QueueStatus
from kidsplay_server.database import (
    configure_conn,
    get_media_item,
    init_db,
    list_processed_files,
    list_queue_items,
    update_queue_item,
)
from kidsplay_server.importers import ImporterRegistry
from kidsplay_server.processing import queue_worker
from kidsplay_server.processing.audio import (
    LoudnessConfig,
    LoudnessError,
    measure_true_peak,
)
from kidsplay_server.processing.loudness_backfill import (
    LoudnessBackfill,
    list_audio_media_ids,
)
from kidsplay_server.processing.pipeline import NormalizeOutcome, ingest_file
from kidsplay_server.processing.queue_worker import drain_queue
from kidsplay_server.storage import MediaStore

MakeTone = Callable[..., Path]  # the ``make_tone`` fixture from conftest.py

OFF = LoudnessConfig(enabled=False)
ON = LoudnessConfig()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


async def _drain(db_path: Path, store: MediaStore) -> int:
    return await drain_queue(db_path, store, ImporterRegistry([]), loudness=ON)


async def _ingest(
    path: Path,
    db_path: Path,
    store: MediaStore,
    media_type: MediaType = MediaType.MUSIC,
) -> uuid.UUID:
    result = await ingest_file(path, media_type, db_path, store, loudness=OFF)
    assert result.media_id is not None
    return result.media_id


async def _jobs(db_path: Path) -> list:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        return await list_queue_items(conn, include_loudness=True)


async def test_list_audio_media_ids(
    make_tone: MakeTone, db_path: Path, store: MediaStore, tmp_path: Path
) -> None:
    from PIL import Image

    song = await _ingest(make_tone("a.mp3", -6), db_path, store)
    book = await _ingest(make_tone("b.mp3", -9), db_path, store, MediaType.AUDIOBOOK)
    photo = tmp_path / "p.png"
    Image.new("RGB", (8, 8)).save(photo)
    await _ingest(photo, db_path, store, MediaType.PHOTO)
    assert await list_audio_media_ids(db_path) == [song, book]


async def test_empty_library(db_path: Path) -> None:
    backfill = LoudnessBackfill(db_path)
    started = await backfill.start(None)
    assert not started.running and started.total == 0
    assert started.started_at is not None and started.finished_at is not None
    assert await _jobs(db_path) == []


async def test_status_before_any_run(db_path: Path) -> None:
    status = await LoudnessBackfill(db_path).status()
    assert status.started_at is None and not status.running and status.total == 0


async def test_start_queues_one_job_per_item_and_returns_at_once(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> None:
    ids = [
        await _ingest(make_tone(f"t{i}.mp3", -6, frequency=400 + i), db_path, store)
        for i in range(3)
    ]
    backfill = LoudnessBackfill(db_path)
    started = await backfill.start(None)
    assert started.running and started.total == 3
    assert (started.normalized, started.skipped, started.failed) == (0, 0, 0)

    jobs = await _jobs(db_path)
    assert {j.media_id for j in jobs} == set(ids)
    assert all(
        j.importer == LOUDNESS_JOB and j.status == QueueStatus.PENDING for j in jobs
    )
    # All jobs of one request share a creation time: that groups the run.
    assert len({j.created_at for j in jobs}) == 1


async def test_all_then_again_is_idempotent(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> None:
    """Acceptance: the second full run skips every item."""
    for i, volume in enumerate((-18, -6)):
        await _ingest(make_tone(f"t{i}.mp3", volume, frequency=400 + i), db_path, store)
    backfill = LoudnessBackfill(db_path)

    await backfill.start(None)
    await _drain(db_path, store)
    first = await backfill.status()
    assert not first.running
    assert (first.total, first.normalized, first.skipped, first.failed) == (2, 2, 0, 0)
    store_files = sorted(p for p in store.root.rglob("*") if p.is_file())

    await backfill.start(None)
    await _drain(db_path, store)
    second = await backfill.status()
    assert (second.total, second.normalized, second.skipped) == (2, 0, 2)
    assert sorted(p for p in store.root.rglob("*") if p.is_file()) == store_files


async def test_selected_ids_deduplicated(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> None:
    media_id = await _ingest(make_tone("t.mp3", -6), db_path, store)
    backfill = LoudnessBackfill(db_path)
    await backfill.start([media_id, media_id, uuid.uuid4()])
    await _drain(db_path, store)
    status = await backfill.status()
    assert (status.total, status.normalized, status.skipped) == (2, 1, 1)


async def test_item_with_a_pending_job_is_not_queued_twice(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> None:
    media_id = await _ingest(make_tone("t.mp3", -6), db_path, store)
    backfill = LoudnessBackfill(db_path)
    await backfill.start([media_id])
    second = await backfill.start([media_id])
    assert second.total == 1
    assert len(await _jobs(db_path)) == 1


async def test_unchanged_counted(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> None:
    await _ingest(make_tone("silent.mp3", -120), db_path, store)
    backfill = LoudnessBackfill(db_path)
    await backfill.start(None)
    await _drain(db_path, store)
    assert (await backfill.status()).unchanged == 1


async def test_failures_recorded_and_run_continues(
    db_path: Path, store: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad, good = uuid.uuid4(), uuid.uuid4()

    async def fake(media_id: uuid.UUID, *args: object) -> NormalizeOutcome:
        if media_id == bad:
            raise LoudnessError("ffmpeg exited with 1")
        return NormalizeOutcome.NORMALIZED

    monkeypatch.setattr(queue_worker, "normalize_media_item", fake)
    backfill = LoudnessBackfill(db_path)
    await backfill.start([bad, good])
    await _drain(db_path, store)
    status = await backfill.status()
    assert (status.failed, status.normalized) == (1, 1)
    assert status.errors == [f"{bad}: ffmpeg exited with 1"]
    assert status.errors_omitted == 0
    assert not status.running


async def test_many_failures_are_capped_and_counted(
    db_path: Path, store: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error list stays short however many items fail."""

    async def fail(media_id: uuid.UUID, *args: object) -> NormalizeOutcome:
        raise LoudnessError("ffmpeg exited with 1")

    monkeypatch.setattr(queue_worker, "normalize_media_item", fail)
    backfill = LoudnessBackfill(db_path)
    await backfill.start([uuid.uuid4() for _ in range(35)])
    await _drain(db_path, store)
    status = await backfill.status()
    assert status.failed == 35
    assert len(status.errors) == status.MAX_ERRORS == 20
    assert status.errors_omitted == 15


async def test_run_spans_uploads_that_arrive_while_it_is_busy(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> None:
    """A job queued while others are unfinished belongs to the same run; one
    queued after everything finished starts a new run."""
    first = await _ingest(make_tone("a.mp3", -6), db_path, store)
    backfill = LoudnessBackfill(db_path)
    await backfill.start([first])
    late = await ingest_file(
        make_tone("b.mp3", -9, frequency=500),
        MediaType.MUSIC,
        db_path,
        store,
        loudness=ON,
    )
    assert late.media_id is not None
    assert (await backfill.status()).total == 2

    await _drain(db_path, store)
    done = await backfill.status()
    assert (done.total, done.normalized, done.running) == (2, 2, False)

    later = await ingest_file(
        make_tone("c.mp3", -12, frequency=600),
        MediaType.MUSIC,
        db_path,
        store,
        loudness=ON,
    )
    assert later.media_id is not None
    fresh = await backfill.status()
    assert (fresh.total, fresh.running) == (1, True)


async def test_status_survives_a_restart(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> None:
    """Progress lives in the database, not in the process."""
    media_id = await _ingest(make_tone("t.mp3", -6), db_path, store)
    await LoudnessBackfill(db_path).start([media_id])
    restarted = LoudnessBackfill(db_path)  # a new process, same database
    status = await restarted.status()
    assert (status.running, status.total) == (True, 1)
    await _drain(db_path, store)
    assert (await restarted.status()).normalized == 1


async def test_old_finished_jobs_are_pruned(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> None:
    media_id = await _ingest(make_tone("t.mp3", -6), db_path, store)
    backfill = LoudnessBackfill(db_path)
    await backfill.start([media_id])
    await _drain(db_path, store)
    (job,) = await _jobs(db_path)
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await update_queue_item(
            conn, job.id, completed_at=datetime.now() - timedelta(days=30)
        )
        await conn.commit()
    await backfill.start([])  # any start prunes
    assert await _jobs(db_path) == []


async def test_previously_failed_item_is_normalized_by_a_backfill(
    clipped_master: Path,
    db_path: Path,
    store: MediaStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#61: a loud master that failed ("true peak still above ...") is picked
    up again by the next backfill and normalized under the ceiling."""
    media_id = await _ingest(clipped_master, db_path, store)
    backfill = LoudnessBackfill(db_path)

    real = queue_worker.normalize_media_item

    async def failing(*args: object) -> NormalizeOutcome:
        raise LoudnessError("x.mp3: true peak still above -1.5 dBTP after 3 attempts")

    monkeypatch.setattr(queue_worker, "normalize_media_item", failing)
    await backfill.start(None)
    await _drain(db_path, store)
    assert (await backfill.status()).failed == 1
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        before = await get_media_item(conn, media_id)
    assert before is not None and before.loudness_target_lufs is None

    monkeypatch.setattr(queue_worker, "normalize_media_item", real)
    await backfill.start(None)
    await _drain(db_path, store)
    status = await backfill.status()
    assert (status.normalized, status.failed) == (1, 0)

    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        item = await get_media_item(conn, media_id)
        files = {pf.file_type: pf for pf in await list_processed_files(conn, media_id)}
    assert item is not None and item.loudness_source_lufs is not None
    assert item.loudness_target_lufs == ON.target_lufs
    assert item.loudness_mode == "dynamic"
    assert item.loudness_gain_db is not None
    # Stored gain is what was applied: a little less than the plain 16 - 8.1.
    assert item.loudness_gain_db < ON.target_lufs - item.loudness_source_lufs
    stored = store.get_absolute_path(files["audio"].relative_path)
    assert measure_true_peak(stored) <= ON.true_peak_dbtp
    # The original stays; the normalized MP3 is a new store file.
    assert files["audio"].content_hash != files["audio_source"].content_hash
    assert store.get_absolute_path(files["audio_source"].relative_path).is_file()
