"""Tests for photo crop coordinates flowing through the processing pipeline.

Verifies:
  - process_photo without crop (baseline)
  - process_photo with crop coordinates applied before resize
  - ingest_file with crop coordinates
  - POST /api/v1/media/upload with crop form fields
"""

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from PIL import Image

from kidsplay_server.processing.images import process_photo
from kidsplay_server.storage import MediaStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def media_store(tmp_path: Path) -> MediaStore:
    return MediaStore(tmp_path / "media")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _make_png(width: int = 400, height: int = 300, color: tuple = (255, 0, 0)) -> bytes:
    """Create a solid-colour PNG in a temp file."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _write_png(
    tmp_path: Path,
    width: int = 400,
    height: int = 300,
    color: tuple = (255, 0, 0),
) -> Path:
    p = tmp_path / "photo.png"
    p.write_bytes(_make_png(width, height, color))
    return p


# ---------------------------------------------------------------------------
# process_photo — crop parameter
# ---------------------------------------------------------------------------


def test_process_photo_no_crop(tmp_path: Path, media_store: MediaStore) -> None:
    """Without crop, photo is resized to fit 640×480."""
    src = _write_png(tmp_path, 800, 600)
    content_hash, rel_path = process_photo(src, 640, 480, media_store)
    assert content_hash
    assert rel_path.endswith(".webp")
    # Verify output dimensions fit within 640×480
    out = media_store.root / rel_path
    with Image.open(out) as img:
        assert img.width <= 640
        assert img.height <= 480


def test_process_photo_with_crop(tmp_path: Path, media_store: MediaStore) -> None:
    """With crop, only the cropped region is resized."""
    # 400x300 image, crop the top-left 100x100 quadrant
    src = _write_png(tmp_path, 400, 300)
    crop = {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}
    content_hash, rel_path = process_photo(src, 640, 480, media_store, crop=crop)
    assert content_hash
    out = media_store.root / rel_path
    with Image.open(out) as img:
        # Crop region is square (100x100), thumbnail won't exceed 640×480
        # and aspect ratio is 1:1, so output should also be square
        assert img.width == img.height


def test_process_photo_crop_different_from_no_crop(
    tmp_path: Path, media_store: MediaStore
) -> None:
    """A photo processed with crop produces different bytes than without."""
    src = _write_png(tmp_path, 400, 300, color=(200, 100, 50))

    # Nocrop: centred resize of full image
    hash_full, _ = process_photo(src, 640, 480, media_store)

    # Crop: only a sub-region
    crop = {"x": 10.0, "y": 10.0, "width": 80.0, "height": 80.0}
    hash_crop, _ = process_photo(src, 640, 480, media_store, crop=crop)

    assert hash_full != hash_crop


def test_process_photo_crop_fractional_coords(
    tmp_path: Path, media_store: MediaStore
) -> None:
    """Fractional crop coordinates are rounded to integers without error."""
    src = _write_png(tmp_path, 400, 300)
    crop = {"x": 10.7, "y": 20.3, "width": 150.9, "height": 100.1}
    content_hash, rel_path = process_photo(src, 640, 480, media_store, crop=crop)
    assert content_hash
    assert (media_store.root / rel_path).exists()


# ---------------------------------------------------------------------------
# ingest_file — crop flows through pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_file_photo_with_crop(
    tmp_path: Path, db_path: Path, media_store: MediaStore
) -> None:
    """ingest_file with crop produces a READY result."""
    from kidsplay_models.media import MediaType
    from kidsplay_models.processing import ProcessingStatus
    from kidsplay_server.processing.pipeline import ingest_file

    src = tmp_path / "img.png"
    src.write_bytes(_make_png(400, 300))

    result = await ingest_file(
        src,
        MediaType.PHOTO,
        db_path,
        media_store,
        playlist_title="Test",
        crop={"x": 0.0, "y": 0.0, "width": 200.0, "height": 150.0},
    )
    assert result.processing_status == ProcessingStatus.READY
    assert result.media_id is not None


# ---------------------------------------------------------------------------
# POST /api/v1/media/upload — crop form fields
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    from kidsplay_server.api.app import create_app

    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def client(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    from httpx import ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=admin_headers,
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_upload_photo_with_crop(client: AsyncClient) -> None:
    """Uploading a photo with crop_* form fields returns a READY result."""
    img_bytes = _make_png(400, 300)
    resp = await client.post(
        "/api/v1/media/upload",
        files={"file": ("photo.png", img_bytes, "image/png")},
        data={
            "media_type": "photo",
            "playlist_title": "Cropped Album",
            "crop_x": "10",
            "crop_y": "10",
            "crop_width": "200",
            "crop_height": "150",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["successful"] == 1
    assert body["results"][0]["processing_status"] == "ready"


@pytest.mark.asyncio
async def test_upload_photo_partial_crop_ignored(client: AsyncClient) -> None:
    """Partial crop (only some fields present) is ignored — no error."""
    img_bytes = _make_png(200, 150)
    resp = await client.post(
        "/api/v1/media/upload",
        files={"file": ("photo2.png", img_bytes, "image/png")},
        data={
            "media_type": "photo",
            "playlist_title": "Album",
            "crop_x": "0",
            # crop_y, crop_width, crop_height intentionally missing
        },
    )
    assert resp.status_code == 200
    assert resp.json()["successful"] == 1
