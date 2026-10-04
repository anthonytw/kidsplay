"""Tests for loudness normalization in the ingest pipeline and the backfill.

Covers ``ingest_file`` with normalization on, off, without ffmpeg and on
failure, and ``normalize_media_item``: repointing rows at a new store file,
keeping the original, idempotency, and target changes. Uses real ffmpeg on
tones from the ``make_tone`` fixture.

Ingest only stores the file and queues a normalization job; the helper
``_ingest`` then runs the queue (``drain_queue``), as the server's worker
would, so a test sees the finished result. ``TestIngestIsFast`` tests the
queued state itself.
"""

import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path

import aiosqlite
import pytest

from kidsplay_models import MediaItem, ProcessedFile
from kidsplay_models.media import MediaType
from kidsplay_models.processing import ProcessingStatus
from kidsplay_server.database import (
    configure_conn,
    delete_media_item,
    get_media_item,
    list_processed_files,
)
from kidsplay_server.importers import ImporterRegistry
from kidsplay_server.processing import pipeline
from kidsplay_server.processing.audio import (
    LoudnessConfig,
    LoudnessError,
    LoudnessTarget,
    measure_loudness,
)
from kidsplay_server.processing.pipeline import (
    NormalizeOutcome,
    ingest_file,
    normalize_media_item,
)
from kidsplay_server.processing.queue_worker import drain_queue
from kidsplay_server.storage import MediaStore

MakeTone = Callable[..., Path]  # the ``make_tone`` fixture from conftest.py

ON = LoudnessConfig()
OFF = LoudnessConfig(enabled=False)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


async def _files(db_path: Path, media_id: uuid.UUID) -> dict[str, ProcessedFile]:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        return {pf.file_type: pf for pf in await list_processed_files(conn, media_id)}


async def _item(db_path: Path, media_id: uuid.UUID) -> MediaItem:
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        item = await get_media_item(conn, media_id)
    assert item is not None
    return item


def _snapshot(store: MediaStore) -> dict[str, tuple[bytes, int]]:
    """Map every store file to its bytes and mtime."""
    return {
        str(p.relative_to(store.root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in store.root.rglob("*")
        if p.is_file()
    }


async def _drain(db_path: Path, store: MediaStore, loudness: LoudnessConfig) -> int:
    """Run the queued normalization jobs, as the queue worker would."""
    return await drain_queue(db_path, store, ImporterRegistry([]), loudness=loudness)


async def _ingest(
    path: Path,
    db_path: Path,
    store: MediaStore,
    loudness: LoudnessConfig = ON,
    media_type: MediaType = MediaType.MUSIC,
    *,
    drain: bool = True,
) -> uuid.UUID:
    result = await ingest_file(path, media_type, db_path, store, loudness=loudness)
    assert result.processing_status == ProcessingStatus.READY, result.errors
    assert result.media_id is not None
    if drain:
        await _drain(db_path, store, loudness)
    return result.media_id


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class TestIngestNormalizes:
    async def test_two_tones_12db_apart_within_1lu(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        """Acceptance: after ingest both tones measure within ±1 LU of target."""
        quiet = make_tone("quiet.mp3", -18)
        loud = make_tone("loud.mp3", -6)
        sources = [measure_loudness(quiet), measure_loudness(loud)]
        assert sources[1].integrated_lufs - sources[0].integrated_lufs == (
            pytest.approx(12, abs=0.5)
        )

        for path in (quiet, loud):
            media_id = await _ingest(path, db_path, store)
            audio = (await _files(db_path, media_id))["audio"]
            out = measure_loudness(store.get_absolute_path(audio.relative_path))
            assert out.integrated_lufs == pytest.approx(-16.0, abs=1.0)
            assert out.true_peak_dbtp <= -1.5

    async def test_rows_and_fields(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        tone = make_tone("quiet.mp3", -18)
        source_hash = store.compute_hash(tone)
        media_id = await _ingest(tone, db_path, store)

        files = await _files(db_path, media_id)
        assert set(files) == {"audio", "audio_source"}
        # The original is kept byte-for-byte; the device gets a new MP3.
        assert files["audio_source"].content_hash == source_hash
        assert store.compute_hash(
            store.get_absolute_path(files["audio_source"].relative_path)
        ) == (source_hash)
        assert files["audio"].content_hash != source_hash
        assert files["audio"].mime_type == "audio/mpeg"
        assert files["audio"].relative_path.endswith(".mp3")

        item = await _item(db_path, media_id)
        assert item.content_hash == source_hash  # dedup key is unchanged
        assert item.loudness_target_lufs == -16.0
        assert item.loudness_target_true_peak_dbtp == -1.5
        assert item.loudness_source_lufs == pytest.approx(-39.75, abs=1.0)
        assert item.loudness_source_true_peak_dbtp is not None
        assert item.loudness_gain_db == pytest.approx(23.75, abs=1.0)

    async def test_mode_is_recorded(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        """A plain gain and a limited one used to look the same afterwards."""
        plain = await _ingest(make_tone("quiet.mp3", -18), db_path, store)
        limited = await _ingest(
            make_tone("spiky.wav", 0, spike=True, frequency=500), db_path, store
        )
        assert (await _item(db_path, plain)).loudness_mode == "linear"
        assert (await _item(db_path, limited)).loudness_mode == "dynamic"

    async def test_never_limit_setting_records_capped(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        cfg = LoudnessConfig(allow_limiting=False)
        media_id = await _ingest(
            make_tone("spiky.wav", 0, spike=True), db_path, store, cfg
        )
        item = await _item(db_path, media_id)
        assert item.loudness_mode == "capped"
        # The target is what was asked for, so the item counts as up to date
        # and a repeated backfill does not redo it.
        assert item.loudness_target_lufs == -16.0
        assert (
            await normalize_media_item(media_id, db_path, store, cfg)
            == NormalizeOutcome.SKIPPED
        )

    async def test_allowing_limiting_again_renormalizes_capped_items(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        never = LoudnessConfig(allow_limiting=False)
        media_id = await _ingest(
            make_tone("spiky.wav", 0, spike=True), db_path, store, never
        )
        capped = await _item(db_path, media_id)
        assert capped.loudness_mode == "capped"

        # Still forbidden: up to date. Allowed again: redone, then stable.
        assert (
            await normalize_media_item(media_id, db_path, store, never)
            == NormalizeOutcome.SKIPPED
        )
        assert (
            await normalize_media_item(media_id, db_path, store, ON)
            == NormalizeOutcome.NORMALIZED
        )
        redone = await _item(db_path, media_id)
        assert redone.loudness_mode == "dynamic"
        assert redone.loudness_gain_db is not None
        assert capped.loudness_gain_db is not None
        assert redone.loudness_gain_db > capped.loudness_gain_db
        assert (
            await normalize_media_item(media_id, db_path, store, ON)
            == NormalizeOutcome.SKIPPED
        )

    async def test_mode_follows_a_renormalization(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        media_id = await _ingest(make_tone("spiky.wav", 0, spike=True), db_path, store)
        assert (await _item(db_path, media_id)).loudness_mode == "dynamic"
        outcome = await normalize_media_item(
            media_id, db_path, store, LoudnessConfig(target_lufs=-20.0)
        )
        assert outcome == NormalizeOutcome.NORMALIZED
        assert (await _item(db_path, media_id)).loudness_mode in ("linear", "dynamic")

    async def test_audiobook_uses_its_own_target(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        cfg = LoudnessConfig(audiobook_target_lufs=-12.0)
        media_id = await _ingest(
            make_tone("book.mp3", -18), db_path, store, cfg, MediaType.AUDIOBOOK
        )
        audio = (await _files(db_path, media_id))["audio"]
        out = measure_loudness(store.get_absolute_path(audio.relative_path))
        assert out.integrated_lufs == pytest.approx(-12.0, abs=1.0)
        assert (await _item(db_path, media_id)).loudness_target_lufs == -12.0

    async def test_duplicate_is_still_skipped(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        tone = make_tone("t.mp3", -6)
        await _ingest(tone, db_path, store)
        again = await ingest_file(tone, MediaType.MUSIC, db_path, store, loudness=ON)
        assert again.skipped


class TestIngestWithoutNormalization:
    async def _assert_unchanged(
        self, db_path: Path, store: MediaStore, media_id: uuid.UUID, source: Path
    ) -> None:
        files = await _files(db_path, media_id)
        assert set(files) == {"audio"}
        assert files["audio"].content_hash == store.compute_hash(source)
        item = await _item(db_path, media_id)
        assert item.loudness_target_lufs is None
        assert item.loudness_gain_db is None

    async def test_disabled(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("ffmpeg must not run when disabled")

        monkeypatch.setattr(pipeline, "normalize_loudness", boom)
        tone = make_tone("t.mp3", -6)
        media_id = await _ingest(tone, db_path, store, OFF)
        await self._assert_unchanged(db_path, store, media_id, tone)

    async def test_no_ffmpeg_keeps_ingest_working(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Without ffmpeg, MP3 ingest still succeeds: stored as-is, with a warning."""
        tone = make_tone("t.mp3", -6)
        monkeypatch.setenv("PATH", "")
        with caplog.at_level(logging.WARNING):
            media_id = await _ingest(tone, db_path, store)
        await self._assert_unchanged(db_path, store, media_id, tone)
        assert "ffmpeg not found" in caplog.text

    async def test_failure_keeps_ingest_working(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def fail(*args: object, **kwargs: object) -> None:
            raise LoudnessError("ffmpeg exited with 1: boom")

        monkeypatch.setattr(pipeline, "normalize_loudness", fail)
        tone = make_tone("t.mp3", -6)
        with caplog.at_level(logging.WARNING):
            media_id = await _ingest(tone, db_path, store)
        await self._assert_unchanged(db_path, store, media_id, tone)
        assert "Loudness job" in caplog.text
        assert "ffmpeg exited with 1: boom" in caplog.text

    async def test_silent_audio_kept_but_marked_processed(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        # -120 dB puts the tone far below the -70 LUFS absolute gate.
        tone = make_tone("silent.mp3", -120)
        media_id = await _ingest(tone, db_path, store)
        files = await _files(db_path, media_id)
        assert set(files) == {"audio"}
        assert files["audio"].content_hash == store.compute_hash(tone)
        item = await _item(db_path, media_id)
        assert item.loudness_target_lufs == -16.0
        assert item.loudness_source_lufs is None
        assert item.loudness_gain_db is None
        assert item.loudness_mode is None


# ---------------------------------------------------------------------------
# Backfill of one item
# ---------------------------------------------------------------------------


class TestNormalizeMediaItem:
    async def test_normalizes_unnormalized_item(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        tone = make_tone("t.mp3", -18)
        media_id = await _ingest(tone, db_path, store, OFF)
        before_files = await _files(db_path, media_id)
        before_store = _snapshot(store)

        outcome = await normalize_media_item(media_id, db_path, store, ON)
        assert outcome == NormalizeOutcome.NORMALIZED

        files = await _files(db_path, media_id)
        # The old audio row now holds the original, unchanged.
        assert files["audio_source"].id == before_files["audio"].id
        assert files["audio_source"].relative_path == (
            before_files["audio"].relative_path
        )
        assert files["audio"].relative_path not in before_store
        out = measure_loudness(store.get_absolute_path(files["audio"].relative_path))
        assert out.integrated_lufs == pytest.approx(-16.0, abs=1.0)
        # No store file was rewritten or deleted; one was added.
        after_store = _snapshot(store)
        assert {k: after_store[k] for k in before_store} == before_store
        assert set(after_store) - set(before_store) == {files["audio"].relative_path}

        item = await _item(db_path, media_id)
        assert item.loudness_target_lufs == -16.0
        assert item.loudness_gain_db == pytest.approx(23.75, abs=1.0)

    async def test_idempotent(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Acceptance: a second run re-encodes nothing."""
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store, OFF)
        assert (
            await normalize_media_item(media_id, db_path, store, ON)
            == NormalizeOutcome.NORMALIZED
        )
        files, snapshot = await _files(db_path, media_id), _snapshot(store)
        updated_at = (await _item(db_path, media_id)).updated_at

        calls: list[object] = []
        monkeypatch.setattr(pipeline, "normalize_loudness", calls.append)
        outcome = await normalize_media_item(media_id, db_path, store, ON)

        assert outcome == NormalizeOutcome.SKIPPED
        assert calls == []
        assert await _files(db_path, media_id) == files
        assert _snapshot(store) == snapshot
        assert (await _item(db_path, media_id)).updated_at == updated_at

    async def test_ingested_normalized_is_skipped(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store)
        assert (
            await normalize_media_item(media_id, db_path, store, ON)
            == NormalizeOutcome.SKIPPED
        )

    async def test_new_target_renormalizes_from_original(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        tone = make_tone("t.mp3", -18)
        media_id = await _ingest(tone, db_path, store)
        first = await _files(db_path, media_id)
        before_store = _snapshot(store)

        louder = LoudnessConfig(target_lufs=-12.0)
        outcome = await normalize_media_item(media_id, db_path, store, louder)
        assert outcome == NormalizeOutcome.NORMALIZED

        files = await _files(db_path, media_id)
        assert files["audio_source"] == first["audio_source"]
        assert files["audio"].id == first["audio"].id  # repointed, not re-added
        assert files["audio"].relative_path != first["audio"].relative_path
        # The superseded normalized file is left for garbage collection.
        after_store = _snapshot(store)
        assert {k: after_store[k] for k in before_store} == before_store
        out = measure_loudness(store.get_absolute_path(files["audio"].relative_path))
        assert out.integrated_lufs == pytest.approx(-12.0, abs=1.0)
        item = await _item(db_path, media_id)
        assert item.loudness_target_lufs == -12.0
        # Measured from the original, not from the earlier normalized copy.
        assert item.loudness_source_lufs == pytest.approx(-39.75, abs=1.0)

    async def test_media_type_change_uses_new_target(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        cfg = LoudnessConfig(audiobook_target_lufs=-12.0)
        media_id = await _ingest(make_tone("t.mp3", -18), db_path, store, cfg)
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "UPDATE media_items SET media_type = 'audiobook' WHERE id = ?",
                (str(media_id),),
            )
            await conn.commit()
        outcome = await normalize_media_item(media_id, db_path, store, cfg)
        assert outcome == NormalizeOutcome.NORMALIZED
        assert (await _item(db_path, media_id)).loudness_target_lufs == -12.0

    async def test_silent_item_unchanged_then_skipped(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        media_id = await _ingest(make_tone("s.mp3", -120), db_path, store, OFF)
        files = await _files(db_path, media_id)
        assert (
            await normalize_media_item(media_id, db_path, store, ON)
            == NormalizeOutcome.UNCHANGED
        )
        assert await _files(db_path, media_id) == files
        assert (
            await normalize_media_item(media_id, db_path, store, ON)
            == NormalizeOutcome.SKIPPED
        )

    async def test_photo_and_missing_item_skipped(
        self, db_path: Path, store: MediaStore, tmp_path: Path
    ) -> None:
        from PIL import Image

        photo = tmp_path / "p.png"
        Image.new("RGB", (10, 10)).save(photo)
        media_id = await _ingest(photo, db_path, store, ON, MediaType.PHOTO)
        assert (
            await normalize_media_item(media_id, db_path, store, ON)
            == NormalizeOutcome.SKIPPED
        )
        assert (
            await normalize_media_item(uuid.uuid4(), db_path, store, ON)
            == NormalizeOutcome.SKIPPED
        )

    async def test_no_ffmpeg_raises(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        media_id = await _ingest(make_tone("t.mp3", -6), db_path, store, OFF)
        monkeypatch.setenv("PATH", "")
        with pytest.raises(LoudnessError, match="ffmpeg"):
            await normalize_media_item(media_id, db_path, store, ON)
        assert (await _item(db_path, media_id)).loudness_target_lufs is None

    async def test_missing_store_file_raises(
        self, make_tone: MakeTone, db_path: Path, store: MediaStore
    ) -> None:
        media_id = await _ingest(make_tone("t.mp3", -6), db_path, store, OFF)
        audio = (await _files(db_path, media_id))["audio"]
        os.remove(store.get_absolute_path(audio.relative_path))
        with pytest.raises(LoudnessError, match="missing"):
            await normalize_media_item(media_id, db_path, store, ON)

    async def test_item_deleted_while_encoding_is_skipped(
        self,
        make_tone: MakeTone,
        db_path: Path,
        store: MediaStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        media_id = await _ingest(make_tone("t.mp3", -6), db_path, store, OFF)
        real = pipeline._normalize_to_store

        async def delete_then_normalize(
            source: Path,
            target: LoudnessTarget,
            media_store: MediaStore,
            **kwargs: bool,
        ) -> pipeline._LoudnessOutcome:
            async with aiosqlite.connect(db_path) as conn:
                await configure_conn(conn)
                await delete_media_item(conn, media_id)
            return await real(source, target, media_store, **kwargs)

        monkeypatch.setattr(pipeline, "_normalize_to_store", delete_then_normalize)
        outcome = await normalize_media_item(media_id, db_path, store, ON)
        assert outcome == NormalizeOutcome.SKIPPED
        assert await _files(db_path, media_id) == {}
