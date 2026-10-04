"""End-to-end tests for ingest_url with YouTube URLs (moved from the server).

yt-dlp is mocked so no real network or process is invoked. The pipeline
itself (DB writes, MediaStore) runs against real temp-dir fixtures, and the
importer is found through the installed ``kidsplay.importers`` entry point.
"""

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import mutagen.id3
import pytest
from PIL import Image

from kidsplay_models.media import MediaType
from kidsplay_models.processing import ProcessingStatus
from kidsplay_server.processing.pipeline import ingest_url
from kidsplay_server.storage import MediaStore

# ---------------------------------------------------------------------------
# File helpers (shared with test_processing_pipeline.py style)
# ---------------------------------------------------------------------------

_SINGLE_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
_MPEG_FRAME = _SINGLE_FRAME * 4


def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(100, 150, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _make_mp3(path: Path, *, title: str = "Test", album: str = "Album") -> Path:
    tags = mutagen.id3.ID3()
    tags.add(mutagen.id3.TIT2(encoding=3, text=[title]))
    tags.add(mutagen.id3.TALB(encoding=3, text=[album]))
    tags.save(str(path))
    with path.open("ab") as f:
        f.write(_MPEG_FRAME)
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
# ingest_url — YouTube path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_url_youtube_success(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    """YouTube URL: yt-dlp extracts MP3, thumbnail used as artwork."""
    mp3_path = tmp_path / "Song Title.mp3"
    thumb_path = tmp_path / "Song Title.jpg"
    _make_mp3(mp3_path, title="Song Title", album="My Album")
    thumb_path.write_bytes(_png_bytes())

    with patch(
        "kidsplay_importer_ytdlp.importer.extract_from_youtube",
        new=AsyncMock(return_value=(mp3_path, thumb_path)),
    ):
        result = await ingest_url(
            "https://www.youtube.com/watch?v=abc123",
            MediaType.MUSIC,
            db_path,
            media_store,
            playlist_title="My Album",
        )

    assert result.processing_status == ProcessingStatus.READY
    assert not result.skipped
    assert result.title == "Song Title"


@pytest.mark.asyncio
async def test_ingest_url_youtube_no_thumbnail(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    """YouTube URL without thumbnail still ingests successfully."""
    mp3_path = tmp_path / "No Thumb.mp3"
    _make_mp3(mp3_path, title="No Thumb", album="Album")

    with patch(
        "kidsplay_importer_ytdlp.importer.extract_from_youtube",
        new=AsyncMock(return_value=(mp3_path, None)),
    ):
        result = await ingest_url(
            "https://youtu.be/abc123",
            MediaType.MUSIC,
            db_path,
            media_store,
            playlist_title="Album",
        )

    assert result.processing_status == ProcessingStatus.READY


@pytest.mark.asyncio
async def test_ingest_url_youtube_failure_propagates(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    """yt-dlp failure is captured as a FAILED result, not an exception."""
    with patch(
        "kidsplay_importer_ytdlp.importer.extract_from_youtube",
        new=AsyncMock(
            side_effect=RuntimeError("yt-dlp failed (exit 1): Video unavailable")
        ),
    ):
        result = await ingest_url(
            "https://youtu.be/bad",
            MediaType.MUSIC,
            db_path,
            media_store,
            playlist_title="Playlist",
        )

    assert result.processing_status == ProcessingStatus.FAILED
    assert any("yt-dlp" in e for e in result.errors)
