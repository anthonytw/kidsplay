"""Tests for kidsplay_server.processing.queue_worker.

A fake importer "fetches" a generated PNG into the work directory, so the
real pipeline, database and media store run end to end in tmp_path.
"""

import asyncio
import io
import uuid
from pathlib import Path

import aiosqlite
import pytest
from PIL import Image

from kidsplay_models import QueueItem
from kidsplay_models.media import MediaType
from kidsplay_models.queue import QueueStatus
from kidsplay_server.database import (
    configure_conn,
    create_queue_item,
    get_queue_item,
    init_db,
)
from kidsplay_server.importers import (
    BaseImporter,
    FetchContext,
    FetchedItem,
    ImporterRegistry,
    builtin_importers,
)
from kidsplay_server.processing.queue_worker import (
    _process_queue_item,
    resolve_importer,
    run_queue_worker,
)
from kidsplay_server.storage import MediaStore


class _PhotoImporter(BaseImporter):
    """Writes a PNG into the workdir; optionally fails instead."""

    name = "fakephoto"
    label = "Fake photos"
    requires_queue = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.contexts: list[FetchContext] = []

    def can_handle(self, source: str) -> bool:
        return source.startswith("fake://")

    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        self.contexts.append(ctx)
        ctx.log(f"fake fetch of {source}")
        if self.fail:
            raise RuntimeError("remote said no")
        path = workdir / "pic.png"
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), color=(10, 20, len(self.contexts))).save(
            buf, format="PNG"
        )
        path.write_bytes(buf.getvalue())
        return [FetchedItem(path=path, title="Fetched title")]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def media_store(tmp_path: Path) -> MediaStore:
    return MediaStore(tmp_path / "media")


async def _insert(db_path: Path, item: QueueItem) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await create_queue_item(conn, item)
        await conn.commit()


async def _get(db_path: Path, item_id: uuid.UUID) -> QueueItem:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        item = await get_queue_item(conn, item_id)
    assert item is not None
    return item


def _item(
    *, importer: str | None = "fakephoto", attempt: int = 1, max_retries: int = 3
) -> QueueItem:
    return QueueItem(
        url="fake://pic",
        importer=importer,
        media_type=MediaType.PHOTO,
        playlist_title="Holiday",
        status=QueueStatus.RUNNING,
        attempt=attempt,
        max_retries=max_retries,
    )


def _registry(importer: BaseImporter) -> ImporterRegistry:
    return ImporterRegistry([importer, *builtin_importers()])


# ---------------------------------------------------------------------------
# resolve_importer
# ---------------------------------------------------------------------------


def test_resolve_importer_by_name_then_by_url() -> None:
    fake = _PhotoImporter()
    registry = _registry(fake)
    assert resolve_importer(_item(), registry) is fake
    # Legacy rows (queued before importers existed) have no name.
    assert resolve_importer(_item(importer=None), registry) is fake
    assert resolve_importer(_item(importer="ytdlp"), registry) is None


# ---------------------------------------------------------------------------
# _process_queue_item
# ---------------------------------------------------------------------------


async def test_success_ingests_and_completes(
    db_path: Path, media_store: MediaStore
) -> None:
    fake = _PhotoImporter()
    item = _item()
    await _insert(db_path, item)

    await _process_queue_item(item, _registry(fake), db_path, media_store)

    done = await _get(db_path, item.id)
    assert done.status == QueueStatus.COMPLETED
    assert done.media_id is not None
    assert done.completed_at is not None
    assert "ATTEMPT 1" in done.log
    assert "fake fetch of fake://pic" in done.log
    ctx = fake.contexts[0]
    assert ctx.queued and ctx.attempt == 1


async def test_failure_with_retries_left_requeues(
    db_path: Path, media_store: MediaStore
) -> None:
    item = _item(attempt=1, max_retries=3)
    await _insert(db_path, item)

    await _process_queue_item(
        item, _registry(_PhotoImporter(fail=True)), db_path, media_store
    )

    after = await _get(db_path, item.id)
    assert after.status == QueueStatus.PENDING
    assert after.last_error == "remote said no"
    assert "fake fetch of fake://pic" in after.log
    assert "ERROR: remote said no" in after.log


async def test_failure_on_last_attempt_fails(
    db_path: Path, media_store: MediaStore
) -> None:
    item = _item(attempt=3, max_retries=3)
    await _insert(db_path, item)

    await _process_queue_item(
        item, _registry(_PhotoImporter(fail=True)), db_path, media_store
    )

    after = await _get(db_path, item.id)
    assert after.status == QueueStatus.FAILED
    assert after.completed_at is not None


async def test_missing_importer_fails_without_retry(
    db_path: Path, media_store: MediaStore
) -> None:
    """A job whose plugin was uninstalled fails at once, with a clear error."""
    item = _item(importer="ytdlp", attempt=1, max_retries=5)
    await _insert(db_path, item)

    await _process_queue_item(
        item, ImporterRegistry(builtin_importers()), db_path, media_store
    )

    after = await _get(db_path, item.id)
    assert after.status == QueueStatus.FAILED
    assert after.last_error == "Importer 'ytdlp' is not installed"


@pytest.mark.parametrize("importer", [None, "http"])
async def test_youtube_job_without_plugin_fails_at_once(
    db_path: Path, media_store: MediaStore, importer: str | None
) -> None:
    """Jobs queued by older code (importer NULL or http) do not retry 5 times."""
    item = _item(importer=importer, attempt=1, max_retries=5)
    item.url = "https://www.youtube.com/watch?v=abc"
    await _insert(db_path, item)

    await _process_queue_item(
        item, ImporterRegistry(builtin_importers()), db_path, media_store
    )

    after = await _get(db_path, item.id)
    assert after.status == QueueStatus.FAILED
    assert after.last_error is not None
    assert "yt-dlp plugin" in after.last_error


async def test_legacy_item_without_importer_resolves_by_url(
    db_path: Path, media_store: MediaStore
) -> None:
    item = _item(importer=None)
    await _insert(db_path, item)

    await _process_queue_item(item, _registry(_PhotoImporter()), db_path, media_store)

    assert (await _get(db_path, item.id)).status == QueueStatus.COMPLETED


# ---------------------------------------------------------------------------
# run_queue_worker
# ---------------------------------------------------------------------------


async def test_worker_processes_pending_and_resets_stuck(
    db_path: Path, media_store: MediaStore
) -> None:
    pending = _item()
    pending.status = QueueStatus.PENDING
    pending.attempt = 0
    stuck = _item()  # RUNNING from a "crash"
    stuck.attempt = 0
    await _insert(db_path, pending)
    await _insert(db_path, stuck)

    stop = asyncio.Event()
    task = asyncio.create_task(
        run_queue_worker(
            db_path, media_store, _registry(_PhotoImporter()), shutdown_event=stop
        )
    )
    try:
        for _ in range(200):
            states = {(await _get(db_path, i.id)).status for i in (pending, stuck)}
            if states == {QueueStatus.COMPLETED}:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    assert (await _get(db_path, pending.id)).status == QueueStatus.COMPLETED
    # The RUNNING item was reset to PENDING at startup, then processed.
    assert (await _get(db_path, stuck.id)).status == QueueStatus.COMPLETED
