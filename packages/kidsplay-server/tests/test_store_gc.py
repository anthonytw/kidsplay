"""Tests for kidsplay_server.store_gc and ``kidsplay-server gc``.

Uses a real normalization (ffmpeg on a generated tone) to make a genuinely
superseded file, and the real backup to prove the grace period protects it.
"""

import asyncio
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from click.testing import CliRunner

from kidsplay_models import ProcessedFile
from kidsplay_models.media import MediaType
from kidsplay_server import cli
from kidsplay_server.backup import create_backup
from kidsplay_server.database import (
    configure_conn,
    create_processed_file,
    init_db,
    list_processed_files,
)
from kidsplay_server.importers import ImporterRegistry
from kidsplay_server.processing.audio import LoudnessConfig
from kidsplay_server.processing.pipeline import ingest_file, normalize_media_item
from kidsplay_server.processing.queue_worker import drain_queue
from kidsplay_server.storage import MediaStore
from kidsplay_server.store_gc import DEFAULT_GRACE, collect_garbage

MakeTone = Callable[..., Path]  # the ``make_tone`` fixture from conftest.py

T0 = datetime(2026, 9, 1, 12, 0)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _files(store: MediaStore) -> set[str]:
    return {
        p.relative_to(store.root).as_posix()
        for p in store.root.rglob("*")
        if p.is_file()
    }


async def _library_with_a_superseded_file(
    make_tone: MakeTone, db_path: Path, store: MediaStore
) -> tuple[uuid.UUID, str]:
    """Ingest and normalize a tone, then move it to a new target.

    Returns:
        The media ID and the relative path of the now-unreferenced file.
    """
    result = await ingest_file(
        make_tone("t.mp3", -18),
        MediaType.MUSIC,
        db_path,
        store,
        loudness=LoudnessConfig(),
    )
    assert result.media_id is not None
    await drain_queue(db_path, store, ImporterRegistry([]), loudness=LoudnessConfig())
    old = await _audio_path(db_path, result.media_id)
    outcome = await normalize_media_item(
        result.media_id, db_path, store, LoudnessConfig(target_lufs=-12.0)
    )
    assert outcome.value == "normalized"
    assert await _audio_path(db_path, result.media_id) != old
    return result.media_id, old


async def _audio_path(db_path: Path, media_id: uuid.UUID) -> str:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        files = {pf.file_type: pf for pf in await list_processed_files(conn, media_id)}
    return files["audio"].relative_path


async def _pending(db_path: Path) -> set[str]:
    async with (
        aiosqlite.connect(db_path) as conn,
        conn.execute("SELECT relative_path FROM store_gc_pending") as cur,
    ):
        return {row[0] for row in await cur.fetchall()}


class TestGrace:
    async def test_superseded_file_is_deleted_only_after_the_grace_period(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        media_id, stale = await _library_with_a_superseded_file(
            make_tone, db_path, store
        )
        before = _files(store)

        first = await collect_garbage(db_path, store.root, now=T0)
        assert (first.deleted, first.newly_unreferenced) == (0, 1)
        assert _files(store) == before  # the first run only starts the clock
        assert await _pending(db_path) == {stale}

        during = await collect_garbage(
            db_path, store.root, now=T0 + DEFAULT_GRACE - timedelta(minutes=1)
        )
        assert (during.deleted, during.in_grace) == (0, 1)
        assert stale in _files(store)

        after = await collect_garbage(
            db_path, store.root, now=T0 + DEFAULT_GRACE + timedelta(minutes=1)
        )
        assert (after.deleted, after.deleted_paths) == (1, [stale])
        assert after.freed_bytes > 0
        assert _files(store) == before - {stale}
        assert await _pending(db_path) == set()
        # Everything the item still uses is intact.
        assert (store.root / await _audio_path(db_path, media_id)).is_file()

    async def test_first_run_never_deletes_however_old_the_file(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        """Age on disk proves nothing: a file may have become unreferenced a
        second ago. Only a full grace period seen by GC counts."""
        _, stale = await _library_with_a_superseded_file(make_tone, db_path, store)
        result = await collect_garbage(
            db_path, store.root, grace=timedelta(0), now=T0 + timedelta(days=400)
        )
        assert result.deleted == 0
        assert stale in _files(store)

    async def test_custom_grace(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        _, stale = await _library_with_a_superseded_file(make_tone, db_path, store)
        grace = timedelta(hours=1)
        await collect_garbage(db_path, store.root, grace=grace, now=T0)
        result = await collect_garbage(
            db_path, store.root, grace=grace, now=T0 + timedelta(hours=2)
        )
        assert result.deleted_paths == [stale]

    async def test_a_file_that_is_referenced_again_starts_over(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        media_id, stale = await _library_with_a_superseded_file(
            make_tone, db_path, store
        )
        await collect_garbage(db_path, store.root, now=T0)
        assert await _pending(db_path) == {stale}

        # Something points at it again (e.g. re-normalizing to the old target
        # produced identical bytes).
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await create_processed_file(
                conn,
                ProcessedFile(
                    media_id=media_id,
                    content_hash=Path(stale).stem,
                    file_type="audio_source",
                    relative_path=stale,
                    size_bytes=1,
                ),
            )
            await conn.commit()
        result = await collect_garbage(db_path, store.root, now=T0 + timedelta(days=1))
        assert result.newly_unreferenced == 0
        assert await _pending(db_path) == set()

        late = await collect_garbage(db_path, store.root, now=T0 + timedelta(days=30))
        assert late.deleted == 0
        assert stale in _files(store)

    async def test_a_pending_file_that_vanished_is_forgotten(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        _, stale = await _library_with_a_superseded_file(make_tone, db_path, store)
        await collect_garbage(db_path, store.root, now=T0)
        (store.root / stale).unlink()
        await collect_garbage(db_path, store.root, now=T0 + timedelta(days=1))
        assert await _pending(db_path) == set()


class TestWhatIsNeverTouched:
    async def test_referenced_files_survive_any_number_of_runs(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        result = await ingest_file(
            make_tone("t.mp3", -18),
            MediaType.MUSIC,
            db_path,
            store,
            loudness=LoudnessConfig(enabled=False),
        )
        assert result.media_id is not None
        before = _files(store)
        for day in (0, 10, 20):
            gc = await collect_garbage(
                db_path, store.root, now=T0 + timedelta(days=day)
            )
            assert gc.deleted == 0 and gc.newly_unreferenced == 0
        assert _files(store) == before

    async def test_only_the_pipeline_directories_are_collected(
        self, db_path: Path, store: MediaStore
    ) -> None:
        for rel in ("themes/ab/x.webp", "notes.txt", "audio/ab/x.mp3"):
            path = store.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        link = store.root / "audio" / "ab" / "link.mp3"
        link.symlink_to(store.root / "notes.txt")

        await collect_garbage(db_path, store.root, now=T0)
        result = await collect_garbage(db_path, store.root, now=T0 + timedelta(days=9))
        assert result.deleted_paths == ["audio/ab/x.mp3"]
        assert (store.root / "themes/ab/x.webp").exists()
        assert (store.root / "notes.txt").exists()
        assert link.is_symlink()

    async def test_a_theme_asset_row_protects_its_file(
        self, db_path: Path, store: MediaStore
    ) -> None:
        path = store.root / "photos" / "ab" / "asset.webp"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            await conn.execute(
                "INSERT INTO themes (id, name, colors, created_at, updated_at) "
                "VALUES ('t', 'T', '{}', '2026-01-01', '2026-01-01')"
            )
            await conn.execute(
                "INSERT INTO theme_assets (theme_id, role, content_hash, "
                "relative_path, mime_type, size_bytes) VALUES "
                "('t', 'background', 'h', 'photos/ab/asset.webp', 'image/webp', 1)"
            )
            await conn.commit()
        await collect_garbage(db_path, store.root, now=T0)
        result = await collect_garbage(db_path, store.root, now=T0 + timedelta(days=30))
        assert result.deleted == 0 and path.exists()

    async def test_dry_run_changes_nothing(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        _, stale = await _library_with_a_superseded_file(make_tone, db_path, store)
        before = _files(store)
        dry = await collect_garbage(db_path, store.root, dry_run=True, now=T0)
        assert dry.newly_unreferenced == 1
        assert await _pending(db_path) == set()  # the clock did not start

        await collect_garbage(db_path, store.root, now=T0)
        would = await collect_garbage(
            db_path, store.root, dry_run=True, now=T0 + timedelta(days=30)
        )
        assert would.deleted_paths == [stale]
        assert _files(store) == before
        assert await _pending(db_path) == {stale}


class TestBackupSafety:
    async def test_a_backup_started_before_the_change_finds_its_files(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore, tmp_path: Path
    ) -> None:
        """The interaction the grace period exists for.

        A backup copies the database, then the files it references. Its copy
        of the database predates the re-normalization, so it still wants the
        old file; GC must not have removed it while the backup runs.
        """
        result = await ingest_file(
            make_tone("t.mp3", -18),
            MediaType.MUSIC,
            db_path,
            store,
            loudness=LoudnessConfig(),
        )
        assert result.media_id is not None
        await drain_queue(
            db_path, store, ImporterRegistry([]), loudness=LoudnessConfig()
        )
        snapshot = tmp_path / "snapshot.db"  # the backup's copy of the database
        with sqlite3.connect(db_path) as live, sqlite3.connect(snapshot) as copy:
            live.backup(copy)
        old = await _audio_path(db_path, result.media_id)

        await normalize_media_item(
            result.media_id, db_path, store, LoudnessConfig(target_lufs=-12.0)
        )
        await collect_garbage(db_path, store.root, now=T0)
        await collect_garbage(db_path, store.root, now=T0 + timedelta(days=1))

        backup = create_backup(snapshot, store.root, tmp_path / "backup")
        assert backup.missing_referenced == []
        assert (tmp_path / "backup" / "media" / old).is_file()

        # Once the grace period has passed, a backup that started that long
        # ago would lose the file: the period bounds how long a backup may run.
        await collect_garbage(db_path, store.root, now=T0 + DEFAULT_GRACE * 2)
        assert old not in _files(store)
        shutil.rmtree(tmp_path / "backup")
        late = create_backup(snapshot, store.root, tmp_path / "backup")
        assert late.missing_referenced == [old]


class TestCommand:
    async def _run(self, *args: str | Path) -> str:
        # The command starts its own event loop, so it cannot run on ours.
        result = await asyncio.to_thread(
            CliRunner().invoke, cli.main, [str(a) for a in args]
        )
        assert result.exit_code == 0, result.output
        return result.output

    async def test_gc_command_reports_and_deletes_after_the_grace(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        _, stale = await _library_with_a_superseded_file(make_tone, db_path, store)
        common = ("--db-path", db_path, "--media-store", store.root)

        first = await self._run("gc", *common)
        assert "1 newly unreferenced" in first and "deleted 0" in first
        assert stale in _files(store)

        # A zero-day grace: the second run deletes what the first one saw.
        second = await self._run("gc", "--grace-days", "0", *common)
        assert "deleted 1" in second and stale in second
        assert stale not in _files(store)

    async def test_gc_command_dry_run(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        _, stale = await _library_with_a_superseded_file(make_tone, db_path, store)
        common = ("--db-path", db_path, "--media-store", store.root)
        await self._run("gc", *common)
        out = await self._run("gc", "--grace-days", "0", "--dry-run", *common)
        assert "would delete 1" in out
        assert stale in _files(store)

    def test_negative_grace_is_refused(self, db_path: Path, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli.main,
            ["gc", "--grace-days", "-1", "--db-path", str(db_path)]
            + ["--media-store", str(tmp_path / "m")],
        )
        assert result.exit_code == 2
