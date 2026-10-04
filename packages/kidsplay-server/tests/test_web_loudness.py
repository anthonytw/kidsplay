"""Tests for the loudness parts of the web UI (media details, backfill button).

An upload queues its normalization; the "Normalizing…" state shows until the
queue worker has done it (``drain_queue`` stands in for the worker here).
"""

import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_models.media import MediaType
from kidsplay_server.api.app import create_app
from kidsplay_server.processing.queue_worker import drain_queue
from kidsplay_server.web.routes import loudness_label

MakeTone = Callable[..., Path]  # the ``make_tone`` fixture from conftest.py


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def client(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=admin_headers,
    ) as c:
        yield c


class TestLoudnessLabel:
    def test_photo_has_none(self) -> None:
        assert loudness_label(MediaType.PHOTO, None, None, None) == ""

    def test_not_normalized(self) -> None:
        assert loudness_label("music", None, None, None) == ("Loudness: not normalized")

    def test_too_quiet(self) -> None:
        assert "too quiet" in loudness_label("audiobook", None, None, -16.0)

    def test_normalized(self) -> None:
        assert loudness_label("music", -27.84, 11.9, -16.0) == (
            "Loudness: -27.8 LUFS → -16 LUFS (+11.9 dB)"
        )
        assert "(-3.2 dB)" in loudness_label("music", -12.8, -3.2, -16.0)

    def test_linear_and_unrecorded_mode_read_alike(self) -> None:
        plain = loudness_label("music", -27.84, 11.9, -16.0)
        assert loudness_label("music", -27.84, 11.9, -16.0, "linear") == plain

    def test_dynamic_mode_says_peaks_were_limited(self) -> None:
        label = loudness_label("music", -27.84, 11.9, -16.0, "dynamic")
        assert label == (
            "Loudness: -27.8 LUFS → -16 LUFS (+11.9 dB, loud peaks limited)"
        )

    def test_capped_mode_shows_the_level_actually_reached(self) -> None:
        label = loudness_label("music", -30.0, 9.0, -16.0, "capped")
        assert label == (
            "Loudness: -30.0 LUFS → -21.0 LUFS (+9.0 dB, kept below the -16 LUFS "
            "target to avoid limiting)"
        )

    def test_normalizing_wins_over_everything_else(self) -> None:
        assert loudness_label("music", None, None, None, None, True) == (
            "Loudness: normalizing…"
        )
        assert loudness_label("music", -27.8, 11.9, -16.0, "linear", True) == (
            "Loudness: normalizing…"
        )

    def test_photo_is_never_normalizing(self) -> None:
        assert loudness_label(MediaType.PHOTO, None, None, None, None, True) == ""


def _row_flags(html: str) -> list[str]:
    """The ``data-normalizing`` value of each media row on the page."""
    return re.findall(r'data-normalizing="([^"]*)"\s+onclick=', html)


async def _ingest_tone(client: AsyncClient, path: Path) -> None:
    r = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": str(path),
            "media_type": "music",
            "playlist_title": "Tones",
        },
    )
    assert r.status_code == 200, r.text


async def test_media_page_shows_normalizing_until_the_worker_is_done(
    client: AsyncClient, app: FastAPI, make_tone: MakeTone
) -> None:
    await _ingest_tone(client, make_tone("t.mp3", -18))

    page = await client.get("/media")
    assert page.status_code == 200
    assert 'data-loudness="Loudness: normalizing…"' in page.text
    assert _row_flags(page.text) == ["1"]
    assert 'class="tag tag-processing normalizing-tag"' in page.text
    assert "LUFS → -16 LUFS" not in page.text

    await drain_queue(app.state.db_path, app.state.media_store, app.state.importers)
    page = await client.get("/media")
    assert _row_flags(page.text) == [""]
    assert 'class="tag tag-processing normalizing-tag"' not in page.text
    assert 'data-loudness="Loudness: ' in page.text
    assert "LUFS → -16 LUFS" in page.text


async def test_photos_are_never_marked_normalizing(
    client: AsyncClient, tmp_path: Path
) -> None:
    from PIL import Image

    photo = tmp_path / "p.png"
    Image.new("RGB", (8, 8)).save(photo)
    r = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": str(photo),
            "media_type": "photo",
            "playlist_title": "P",
        },
    )
    assert r.status_code == 200, r.text
    page = await client.get("/media")
    assert _row_flags(page.text) == [""]


async def test_media_page_has_the_backfill_button(client: AsyncClient) -> None:
    page = await client.get("/media")
    assert 'id="normalize-btn"' in page.text
    assert "/api/v1/media/normalize" in page.text
