"""End-to-end tests for kidsplay_server.processing.pipeline.

Uses real SQLite (via tmp_path) and a real MediaStore. No mocking.
Each test creates actual media files, runs the pipeline, then inspects
both the database state and the filesystem layout.
"""

import io
from pathlib import Path

import aiosqlite
import mutagen.id3
import pytest
from PIL import Image

from kidsplay_models.media import MediaType
from kidsplay_models.processing import ProcessingStatus, ThumbnailSize
from kidsplay_server.database import (
    configure_conn,
    get_media_item,
    init_db,
    list_processed_files,
)
from kidsplay_server.processing.pipeline import ingest_directory, ingest_file
from kidsplay_server.storage import MediaStore

# ---------------------------------------------------------------------------
# Helpers — file factories
# ---------------------------------------------------------------------------

# Four silent MPEG1/Layer3 frames — mutagen needs >= 2 consecutive valid
# frames before accepting a sync; 4 triggers the non-sketchy exit branch.
_SINGLE_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
_MPEG_FRAME = _SINGLE_FRAME * 4


def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(100, 150, 200)).save(buf, format="PNG")
    return buf.getvalue()


def make_mp3(
    path: Path,
    *,
    title: str = "Test Song",
    artist: str = "Test Artist",
    album: str = "Test Album",
    with_artwork: bool = True,
) -> Path:
    """Write a minimal valid MP3 with ID3 tags.

    ID3 tags are saved first (creating the file), then a single MPEG
    frame is appended so mutagen can detect format and duration.
    """
    tags = mutagen.id3.ID3()
    tags.add(mutagen.id3.TIT2(encoding=3, text=[title]))
    tags.add(mutagen.id3.TPE1(encoding=3, text=[artist]))
    tags.add(mutagen.id3.TALB(encoding=3, text=[album]))
    if with_artwork:
        tags.add(
            mutagen.id3.APIC(
                encoding=3,
                mime="image/png",
                type=3,
                desc="",
                data=_png_bytes(8, 8),
            )
        )
    tags.save(str(path))
    with path.open("ab") as f:
        f.write(_MPEG_FRAME)
    return path


def make_png(path: Path, width: int = 200, height: int = 200, *, seed: int = 0) -> Path:
    """Write a PNG file; use ``seed`` to produce distinct content."""
    color = (100 + seed * 30, 150, 200 - seed * 20)
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def media_store(tmp_path: Path) -> MediaStore:
    return MediaStore(tmp_path / "media")


# ---------------------------------------------------------------------------
# ingest_file — audio
# ---------------------------------------------------------------------------


class TestIngestFileAudio:
    async def test_success_returns_ready_status(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3")
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)
        assert result.processing_status == ProcessingStatus.READY
        assert not result.skipped
        assert result.errors == []

    async def test_success_sets_media_id(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3")
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)
        assert result.media_id is not None

    async def test_title_from_tag(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3", title="La Bamba")
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)
        assert result.title == "La Bamba"

    async def test_media_item_written_to_db(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3", title="My Song", artist="My Artist")
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            item = await get_media_item(conn, result.media_id)

        assert item is not None
        assert item.title == "My Song"
        assert item.artist == "My Artist"
        assert item.media_type == MediaType.MUSIC
        assert item.processing_status == ProcessingStatus.READY

    async def test_audio_processed_file_written(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3")
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            pfs = await list_processed_files(conn, result.media_id)

        types = {pf.file_type for pf in pfs}
        assert "audio" in types

    async def test_thumbnails_written_when_artwork_present(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3", with_artwork=True)
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            pfs = await list_processed_files(conn, result.media_id)

        types = {pf.file_type for pf in pfs}
        assert "thumbnail_small" in types
        assert "thumbnail_medium" in types
        assert "thumbnail_large" in types

    async def test_thumbnail_files_exist_on_disk(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3", with_artwork=True)
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            pfs = await list_processed_files(conn, result.media_id)

        for pf in pfs:
            assert media_store.exists(pf.relative_path), f"Missing: {pf.relative_path}"

    async def test_no_thumbnails_without_artwork(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        """No artwork → no thumbnail ProcessedFiles, but ingest still succeeds."""
        src = make_mp3(tmp_path / "song.mp3", with_artwork=False)
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)
        assert result.processing_status == ProcessingStatus.READY

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            pfs = await list_processed_files(conn, result.media_id)

        types = {pf.file_type for pf in pfs}
        assert "audio" in types
        assert "thumbnail_small" not in types
        assert "thumbnail_medium" not in types
        assert "thumbnail_large" not in types

    async def test_playlist_title_from_album_tag(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3", album="Pica-Pica")
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            item = await get_media_item(conn, result.media_id)

        assert item is not None
        assert item.playlist_title == "Pica-Pica"

    async def test_playlist_title_override(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3", album="Original Album")
        result = await ingest_file(
            src, MediaType.MUSIC, db_path, media_store, playlist_title="Override"
        )

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            item = await get_media_item(conn, result.media_id)

        assert item is not None
        assert item.playlist_title == "Override"

    async def test_audio_stored_in_media_store(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3")
        result = await ingest_file(src, MediaType.MUSIC, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            pfs = await list_processed_files(conn, result.media_id)

        audio_pf = next(pf for pf in pfs if pf.file_type == "audio")
        assert media_store.exists(audio_pf.relative_path)
        assert audio_pf.relative_path.startswith("audio/")

    async def test_duplicate_returns_skipped(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3")
        r1 = await ingest_file(src, MediaType.MUSIC, db_path, media_store)
        r2 = await ingest_file(src, MediaType.MUSIC, db_path, media_store)

        assert not r1.skipped
        assert r2.skipped
        assert r2.media_id == r1.media_id

    async def test_duplicate_does_not_create_new_db_record(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "song.mp3")
        await ingest_file(src, MediaType.MUSIC, db_path, media_store)
        await ingest_file(src, MediaType.MUSIC, db_path, media_store)

        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            await init_db(conn)
            async with conn.execute("SELECT COUNT(*) FROM media_items") as cur:
                row = await cur.fetchone()

        assert row is not None
        assert row[0] == 1

    async def test_audiobook_media_type_preserved(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_mp3(tmp_path / "chapter.mp3")
        result = await ingest_file(src, MediaType.AUDIOBOOK, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            item = await get_media_item(conn, result.media_id)

        assert item is not None
        assert item.media_type == MediaType.AUDIOBOOK

    async def test_missing_file_returns_failed(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        result = await ingest_file(
            tmp_path / "nonexistent.mp3", MediaType.MUSIC, db_path, media_store
        )
        assert result.processing_status == ProcessingStatus.FAILED
        assert result.errors != []


# ---------------------------------------------------------------------------
# ingest_file — photo
# ---------------------------------------------------------------------------


class TestIngestFilePhoto:
    async def test_success_returns_ready_status(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png")
        result = await ingest_file(src, MediaType.PHOTO, db_path, media_store)
        assert result.processing_status == ProcessingStatus.READY
        assert not result.skipped

    async def test_title_from_filename_stem(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "sunset_01.png")
        result = await ingest_file(src, MediaType.PHOTO, db_path, media_store)
        assert result.title == "sunset_01"

    async def test_playlist_from_parent_directory(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        album_dir = tmp_path / "Beach Trip 2025"
        album_dir.mkdir()
        src = make_png(album_dir / "photo.png")
        result = await ingest_file(src, MediaType.PHOTO, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            item = await get_media_item(conn, result.media_id)

        assert item is not None
        assert item.playlist_title == "Beach Trip 2025"

    async def test_resized_photo_and_three_thumbnails_written(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png", 800, 600)
        result = await ingest_file(src, MediaType.PHOTO, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            pfs = await list_processed_files(conn, result.media_id)

        types = {pf.file_type for pf in pfs}
        assert types == {
            "photo_resized",
            "thumbnail_small",
            "thumbnail_medium",
            "thumbnail_large",
        }

    async def test_all_photo_files_exist_on_disk(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png")
        result = await ingest_file(src, MediaType.PHOTO, db_path, media_store)

        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            pfs = await list_processed_files(conn, result.media_id)

        for pf in pfs:
            assert media_store.exists(pf.relative_path), f"Missing: {pf.relative_path}"

    async def test_photo_duplicate_skipped(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "photo.png")
        r1 = await ingest_file(src, MediaType.PHOTO, db_path, media_store)
        r2 = await ingest_file(src, MediaType.PHOTO, db_path, media_store)
        assert not r1.skipped
        assert r2.skipped


# ---------------------------------------------------------------------------
# ingest_directory
# ---------------------------------------------------------------------------


class TestIngestDirectory:
    async def test_processes_all_mp3_files(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        for i in range(3):
            make_mp3(music_dir / f"track_{i:02d}.mp3", title=f"Track {i}")

        batch = await ingest_directory(music_dir, MediaType.MUSIC, db_path, media_store)

        assert batch.total_files == 3
        assert batch.successful == 3
        assert batch.failed == 0
        assert batch.skipped == 0

    async def test_batch_counts_successful(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        make_mp3(music_dir / "a.mp3", title="A")
        make_mp3(music_dir / "b.mp3", title="B")

        batch = await ingest_directory(music_dir, MediaType.MUSIC, db_path, media_store)

        assert len(batch.results) == 2
        assert all(r.processing_status == ProcessingStatus.READY for r in batch.results)

    async def test_batch_skips_duplicates(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        make_mp3(music_dir / "song.mp3")

        await ingest_directory(music_dir, MediaType.MUSIC, db_path, media_store)
        batch2 = await ingest_directory(
            music_dir, MediaType.MUSIC, db_path, media_store
        )

        assert batch2.skipped == 1
        assert batch2.successful == 0

    async def test_batch_ignores_non_audio_extensions(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        make_mp3(music_dir / "song.mp3")
        (music_dir / "cover.jpg").write_bytes(b"not_audio")
        (music_dir / "readme.txt").write_text("ignore me")

        batch = await ingest_directory(music_dir, MediaType.MUSIC, db_path, media_store)

        assert batch.total_files == 1

    async def test_batch_walks_subdirectories(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        music_dir = tmp_path / "music"
        sub = music_dir / "sub"
        sub.mkdir(parents=True)
        make_mp3(music_dir / "track1.mp3", title="Track 1")
        make_mp3(sub / "track2.mp3", title="Track 2")

        batch = await ingest_directory(music_dir, MediaType.MUSIC, db_path, media_store)

        assert batch.total_files == 2
        assert batch.successful == 2

    async def test_photo_directory_processes_png_files(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        photo_dir = tmp_path / "photos"
        photo_dir.mkdir()
        for i in range(2):
            make_png(photo_dir / f"photo_{i}.png", seed=i)

        batch = await ingest_directory(photo_dir, MediaType.PHOTO, db_path, media_store)

        assert batch.total_files == 2
        assert batch.successful == 2

    async def test_end_to_end_db_records_correct(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        """Full pipeline: directory → DB has media items with processed files."""
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        make_mp3(music_dir / "song_a.mp3", title="Song A", album="Test Album")
        make_mp3(music_dir / "song_b.mp3", title="Song B", album="Test Album")

        batch = await ingest_directory(music_dir, MediaType.MUSIC, db_path, media_store)

        assert batch.successful == 2

        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            async with conn.execute("SELECT COUNT(*) FROM media_items") as cur:
                row = await cur.fetchone()
                assert row is not None
                count = row[0]
            async with conn.execute("SELECT COUNT(*) FROM processed_files") as cur:
                row = await cur.fetchone()
                assert row is not None
                pf_count = row[0]

        assert count == 2
        # Each song has 1 audio + 3 thumbnails = 4 ProcessedFiles × 2 songs = 8
        assert pf_count == 8

    async def test_end_to_end_thumbnail_dimensions(
        self, db_path: Path, media_store: MediaStore, tmp_path: Path
    ) -> None:
        """Thumbnails on disk must fit within their declared bounds."""
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        make_mp3(music_dir / "song.mp3", with_artwork=True)

        batch = await ingest_directory(music_dir, MediaType.MUSIC, db_path, media_store)

        result = batch.results[0]
        assert result.media_id is not None
        async with aiosqlite.connect(db_path) as conn:
            await configure_conn(conn)
            pfs = await list_processed_files(conn, result.media_id)

        size_map = {
            "thumbnail_small": ThumbnailSize.SMALL,
            "thumbnail_medium": ThumbnailSize.MEDIUM,
            "thumbnail_large": ThumbnailSize.LARGE,
        }
        for pf in pfs:
            if pf.file_type not in size_map:
                continue
            size = size_map[pf.file_type]
            path = media_store.get_absolute_path(pf.relative_path)
            with Image.open(path) as img:
                w, h = img.size
            assert w <= size.width, f"{pf.file_type}: width {w} > {size.width}"
            assert h <= size.height, f"{pf.file_type}: height {h} > {size.height}"
