"""Tests for kidsplay_importer_ytdlp.importer.YtDlpImporter.

yt-dlp itself is mocked; these tests cover how the importer drives it, how it
is registered, and how it plugs into the server's queue and API.
"""

import io
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import mutagen.id3
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from kidsplay_importer_ytdlp import YtDlpImporter
from kidsplay_importer_ytdlp.ytdlp import YtDlpResult
from kidsplay_models import QueueItem
from kidsplay_models.media import MediaType
from kidsplay_models.queue import QueueStatus
from kidsplay_server.api.app import create_app
from kidsplay_server.database import (
    configure_conn,
    create_queue_item,
    get_queue_item,
    init_db,
)
from kidsplay_server.importers import (
    FetchContext,
    FetchedItem,
    ImporterInfo,
    discover_importers,
)
from kidsplay_server.processing.queue_worker import _process_queue_item
from kidsplay_server.storage import MediaStore

_URL = "https://www.youtube.com/watch?v=abc123"
_FULL = "kidsplay_importer_ytdlp.importer.extract_from_youtube_full"


def _result(workdir: Path) -> YtDlpResult:
    audio = workdir / "Song.mp3"
    thumb = workdir / "Song.jpg"
    return YtDlpResult(
        audio_path=audio,
        thumbnail_path=thumb,
        command=["uv", "run", "yt-dlp", _URL],
        stdout="[download] 100%",
        stderr="WARNING: something",
        returncode=0,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_entry_point_registers_importer_before_builtins() -> None:
    registry = discover_importers()
    names = [i.name for i in registry]
    assert names.index("ytdlp") < names.index("http")
    resolved = registry.resolve(_URL)
    assert isinstance(resolved, YtDlpImporter)
    assert ImporterInfo.of(resolved).model_dump() == {
        "name": "ytdlp",
        "label": "YouTube",
        "requires_queue": True,
        "supports_preview": True,
    }


def test_can_handle_and_normalize() -> None:
    importer = YtDlpImporter()
    assert importer.can_handle("https://youtu.be/abc")
    assert not importer.can_handle("https://example.com/a.mp3")
    assert importer.normalize(_URL + "&list=RDabc&start_radio=1") == _URL


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


async def test_direct_fetch_uses_default_settings(tmp_path: Path) -> None:
    extract = AsyncMock(return_value=(tmp_path / "a.mp3", None))
    ctx = FetchContext()
    with patch("kidsplay_importer_ytdlp.importer.extract_from_youtube", new=extract):
        items = await YtDlpImporter().fetch(_URL, tmp_path, ctx)
    extract.assert_awaited_once_with(_URL, tmp_path)
    assert items == [FetchedItem(path=tmp_path / "a.mp3")]
    assert ctx.text == ""


async def test_queued_fetch_logs_command_and_backs_off(tmp_path: Path) -> None:
    extract = AsyncMock(return_value=_result(tmp_path))
    ctx = FetchContext(attempt=3, queued=True)
    with patch(_FULL, new=extract):
        items = await YtDlpImporter().fetch(_URL, tmp_path, ctx)

    assert items == [
        FetchedItem(path=tmp_path / "Song.mp3", thumbnail=tmp_path / "Song.jpg")
    ]
    kwargs = extract.call_args.kwargs
    assert kwargs["verbose"] is True
    assert kwargs["sleep"].as_args()[:2] == ["--sleep-requests", "3"]
    assert f"COMMAND: uv run yt-dlp {_URL}" in ctx.text
    assert "EXIT CODE: 0" in ctx.text
    assert "--- STDERR ---\nWARNING: something" in ctx.text


async def test_queued_fetch_retries_auth_error_with_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File")
    monkeypatch.setenv("KIDSPLAY_YT_COOKIES", str(cookies))
    extract = AsyncMock(
        side_effect=[RuntimeError("ERROR: Private video"), _result(tmp_path)]
    )
    with patch(_FULL, new=extract):
        await YtDlpImporter().fetch(_URL, tmp_path, FetchContext(queued=True))
    assert extract.await_count == 2
    assert extract.call_args.kwargs["use_cookies"] is True


async def test_queued_fetch_auth_error_without_cookies_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIDSPLAY_YT_COOKIES", raising=False)
    extract = AsyncMock(side_effect=RuntimeError("ERROR: Private video"))
    with patch(_FULL, new=extract), pytest.raises(RuntimeError, match="Private"):
        await YtDlpImporter().fetch(_URL, tmp_path, FetchContext(queued=True))
    assert extract.await_count == 1


# ---------------------------------------------------------------------------
# Server integration: queue worker and API
# ---------------------------------------------------------------------------


def _write_mp3(path: Path) -> None:
    tags = mutagen.id3.ID3()
    tags.add(mutagen.id3.TIT2(encoding=3, text=["Song"]))
    tags.save(str(path))
    with path.open("ab") as f:
        f.write((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 4)


async def test_queue_worker_runs_youtube_job(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    store = MediaStore(tmp_path / "media")
    item = QueueItem(
        url=_URL,
        importer="ytdlp",
        media_type=MediaType.MUSIC,
        playlist_title="Songs",
        status=QueueStatus.RUNNING,
        attempt=1,
    )
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await create_queue_item(conn, item)
        await conn.commit()

    async def _fake_extract(url: str, dest_dir: Path, **kwargs: object) -> YtDlpResult:
        result = _result(dest_dir)
        _write_mp3(result.audio_path)
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), color=(1, 2, 3)).save(buf, format="JPEG")
        assert result.thumbnail_path is not None
        result.thumbnail_path.write_bytes(buf.getvalue())
        return result

    with patch(_FULL, new=_fake_extract):
        await _process_queue_item(item, discover_importers(), db_path, store)

    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        done = await get_queue_item(conn, item.id)
    assert done is not None
    assert done.status == QueueStatus.COMPLETED
    assert done.media_id is not None
    assert "COMMAND: uv run yt-dlp" in done.log


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def client(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=admin_headers
    ) as c:
        yield c


async def test_api_lists_youtube_importer(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/importers")
    assert "ytdlp" in [i["name"] for i in resp.json()]


async def test_api_queue_normalizes_youtube_url(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/queue",
        json={
            "url": _URL + "&list=RDabc123&start_radio=1&pp=x",
            "media_type": "music",
            "playlist_title": "Songs",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["url"] == _URL
    assert resp.json()["importer"] == "ytdlp"


async def test_import_page_shows_youtube_tab(client: AsyncClient) -> None:
    resp = await client.get("/media/import")
    assert resp.status_code == 200
    assert 'id="tab-youtube"' in resp.text
    assert "YouTube URL (video or playlist)" in resp.text
