"""End-to-end tests for ingest_url in kidsplay_server.processing.pipeline.

Downloads are mocked so no real network is used. The pipeline itself (DB
writes, MediaStore) runs against real temp-dir fixtures. The YouTube tests
live with the yt-dlp plugin, in packages/kidsplay-importer-ytdlp.
"""

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from kidsplay_models.media import MediaType
from kidsplay_models.processing import ProcessingStatus
from kidsplay_server.importers import (
    BaseImporter,
    FetchContext,
    FetchedItem,
    ImporterRegistry,
    builtin_importers,
)
from kidsplay_server.processing.pipeline import (
    ingest_fetched,
    ingest_url,
    ingest_url_batch,
)
from kidsplay_server.storage import MediaStore

# ---------------------------------------------------------------------------
# File helpers (shared with test_processing_pipeline.py style)
# ---------------------------------------------------------------------------


def _png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(100, 150, 200)).save(buf, format="PNG")
    return buf.getvalue()


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
# ingest_url — direct download path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_url_direct_download(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    """Non-YouTube URL: file is downloaded then ingested as a photo."""
    png_path = tmp_path / "photo.png"
    png_path.write_bytes(_png_bytes(200, 200))

    with patch(
        "kidsplay_server.importers.builtin.download_from_url",
        new=AsyncMock(return_value=png_path),
    ):
        result = await ingest_url(
            "https://example.com/photo.png",
            MediaType.PHOTO,
            db_path,
            media_store,
            playlist_title="Vacation",
        )

    assert result.processing_status == ProcessingStatus.READY
    assert result.title == "photo"


@pytest.mark.asyncio
async def test_ingest_url_direct_download_failure(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    """Download failure is captured as a FAILED result."""
    with patch(
        "kidsplay_server.importers.builtin.download_from_url",
        new=AsyncMock(side_effect=RuntimeError("Download failed: HTTP 404 for ...")),
    ):
        result = await ingest_url(
            "https://example.com/missing.mp3",
            MediaType.MUSIC,
            db_path,
            media_store,
            playlist_title="Playlist",
        )

    assert result.processing_status == ProcessingStatus.FAILED
    assert result.errors


# ---------------------------------------------------------------------------
# ingest_url — importer selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_url_without_matching_importer_fails(
    db_path: Path, media_store: MediaStore
) -> None:
    result = await ingest_url(
        "gopher://example.com/a.mp3",
        MediaType.MUSIC,
        db_path,
        media_store,
        registry=ImporterRegistry(builtin_importers()),
    )
    assert result.processing_status == ProcessingStatus.FAILED
    assert result.errors == [
        "No installed importer can handle gopher://example.com/a.mp3"
    ]


@pytest.mark.asyncio
async def test_ingest_url_uses_given_registry(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    """The first matching importer in the registry fetches the URL."""
    png_path = tmp_path / "shot.png"
    png_path.write_bytes(_png_bytes(50, 50))

    class _Custom(BaseImporter):
        name = "custom"
        label = "Custom"

        def can_handle(self, source: str) -> bool:
            return source.startswith("https://custom.example/")

        async def fetch(
            self, source: str, workdir: Path, ctx: FetchContext
        ) -> list[FetchedItem]:
            return [FetchedItem(path=png_path, title="From importer")]

    result = await ingest_url(
        "https://custom.example/x",
        MediaType.PHOTO,
        db_path,
        media_store,
        playlist_title="Pics",
        registry=ImporterRegistry([_Custom(), *builtin_importers()]),
    )
    assert result.processing_status == ProcessingStatus.READY
    assert result.title == "From importer"


@pytest.mark.asyncio
async def test_ingest_url_empty_fetch_fails(
    db_path: Path, media_store: MediaStore
) -> None:
    class _Empty(BaseImporter):
        name = "empty"
        label = "Empty"

        def can_handle(self, source: str) -> bool:
            return True

        async def fetch(
            self, source: str, workdir: Path, ctx: FetchContext
        ) -> list[FetchedItem]:
            return []

    result = await ingest_url(
        "https://example.com/a",
        MediaType.PHOTO,
        db_path,
        media_store,
        registry=ImporterRegistry([_Empty()]),
    )
    assert result.processing_status == ProcessingStatus.FAILED
    assert result.errors == ["Empty importer fetched nothing"]


# ---------------------------------------------------------------------------
# ingest_fetched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_fetched_counts_and_overrides(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    a = tmp_path / "a.png"
    a.write_bytes(_png_bytes(40, 40))
    b = tmp_path / "b.png"
    b.write_bytes(_png_bytes(41, 41))
    items = [
        FetchedItem(path=a, title="Item title"),
        FetchedItem(path=b, title="Other", thumbnail=tmp_path / "missing.jpg"),
        FetchedItem(path=a),  # duplicate content: skipped
    ]
    batch = await ingest_fetched(
        items, MediaType.PHOTO, db_path, media_store, playlist_title="P"
    )
    assert (batch.total_files, batch.successful, batch.skipped) == (3, 2, 1)
    assert batch.results[0].title == "Item title"
    # An unreadable thumbnail is logged, not fatal.
    assert batch.results[1].processing_status == ProcessingStatus.READY

    c = tmp_path / "c.png"
    c.write_bytes(_png_bytes(42, 42))
    batch = await ingest_fetched(
        [FetchedItem(path=c, title="Item title")],
        MediaType.PHOTO,
        db_path,
        media_store,
        title_override="Caller title",
    )
    assert batch.results[0].title == "Caller title"


@pytest.mark.asyncio
async def test_ingest_url_youtube_without_plugin_fails_clearly(
    db_path: Path, media_store: MediaStore
) -> None:
    result = await ingest_url(
        "https://www.youtube.com/watch?v=abc",
        MediaType.MUSIC,
        db_path,
        media_store,
        registry=ImporterRegistry(builtin_importers()),
    )
    assert result.processing_status == ProcessingStatus.FAILED
    assert "yt-dlp plugin" in result.errors[0]


@pytest.mark.asyncio
async def test_ingest_url_batch_reports_every_fetched_file(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    """An importer that fetches several files gets one result for each."""

    class _Album(BaseImporter):
        name = "album"
        label = "Album"

        def can_handle(self, source: str) -> bool:
            return source.startswith("album://")

        async def fetch(
            self, source: str, workdir: Path, ctx: FetchContext
        ) -> list[FetchedItem]:
            paths = []
            for i in range(3):
                path = workdir / f"pic{i}.png"
                path.write_bytes(_png_bytes(10 + i, 10 + i))
                paths.append(FetchedItem(path=path))
            return paths

    registry = ImporterRegistry([_Album(), *builtin_importers()])
    batch = await ingest_url_batch(
        "album://x", MediaType.PHOTO, db_path, media_store, registry=registry
    )
    assert batch.total_files == 3
    assert batch.successful == 3
    assert len({r.media_id for r in batch.results}) == 3

    first = await ingest_url(
        "album://x", MediaType.PHOTO, db_path, media_store, registry=registry
    )
    assert first.skipped
