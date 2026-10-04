"""Tests for POST /api/v1/media/preview with the yt-dlp plugin installed.

yt-dlp subprocess calls are mocked — no real network or process is invoked.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_server.api.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc(returncode: int, stdout: bytes, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _single_video_json(
    title: str = "Beethoven - Moonlight Sonata",
    uploader: str = "ClassicsChannel",
    duration: float = 1020.0,
    thumbnail: str = "https://img.yt/thumb.jpg",
    url: str = "https://www.youtube.com/watch?v=abc",
) -> bytes:
    return json.dumps(
        {
            "_type": "video",
            "title": title,
            "uploader": uploader,
            "duration": duration,
            "thumbnail": thumbnail,
            "webpage_url": url,
        }
    ).encode()


def _playlist_json(
    title: str = "Great Classics",
    entries: list[dict] | None = None,
) -> bytes:
    if entries is None:
        entries = [
            {
                "title": "Artist A - Track 1",
                "uploader": "Artist A",
                "duration": 240.0,
                "thumbnail": "https://img.yt/t1.jpg",
                "webpage_url": "https://www.youtube.com/watch?v=t1",
            },
            {
                "title": "Instrumental Piece",
                "uploader": "Artist B",
                "duration": 180.0,
                "thumbnail": None,
                "webpage_url": "https://www.youtube.com/watch?v=t2",
            },
        ]
    return json.dumps(
        {"_type": "playlist", "title": title, "entries": entries}
    ).encode()


# ---------------------------------------------------------------------------
# Single video
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_single_video_title_split(client: AsyncClient) -> None:
    """Title containing '-' is split into artist and track title."""
    proc = _make_proc(0, _single_video_json(title="Beethoven - Moonlight Sonata"))
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        resp = await client.post(
            "/api/v1/media/preview",
            json={"url": "https://www.youtube.com/watch?v=abc"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_playlist"] is False
    assert len(body["tracks"]) == 1
    track = body["tracks"][0]
    assert track["artist"] == "Beethoven"
    assert track["title"] == "Moonlight Sonata"
    assert track["duration_seconds"] == 1020.0
    assert track["thumbnail_url"] == "https://img.yt/thumb.jpg"


@pytest.mark.asyncio
async def test_preview_single_video_no_dash_uses_uploader(
    client: AsyncClient,
) -> None:
    """Title without '-': uploader becomes artist, full title stays."""
    proc = _make_proc(
        0,
        _single_video_json(title="Moonlight Sonata", uploader="ClassicsChannel"),
    )
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        resp = await client.post(
            "/api/v1/media/preview",
            json={"url": "https://www.youtube.com/watch?v=abc"},
        )
    assert resp.status_code == 200
    track = resp.json()["tracks"][0]
    assert track["artist"] == "ClassicsChannel"
    assert track["title"] == "Moonlight Sonata"


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_playlist_returns_tracks(client: AsyncClient) -> None:
    """Playlist JSON returns is_playlist=True and all entry titles parsed."""
    proc = _make_proc(0, _playlist_json())
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        resp = await client.post(
            "/api/v1/media/preview",
            json={"url": "https://www.youtube.com/playlist?list=PL123"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_playlist"] is True
    assert body["playlist_title"] == "Great Classics"
    assert len(body["tracks"]) == 2

    t0 = body["tracks"][0]
    assert t0["artist"] == "Artist A"
    assert t0["title"] == "Track 1"

    t1 = body["tracks"][1]
    assert t1["artist"] == "Artist B"
    assert t1["title"] == "Instrumental Piece"


@pytest.mark.asyncio
async def test_preview_playlist_null_entries_skipped(client: AsyncClient) -> None:
    """None entries in a playlist are silently skipped."""
    raw = json.dumps(
        {
            "_type": "playlist",
            "title": "Mixed",
            "entries": [
                None,
                {
                    "title": "Good Track",
                    "uploader": "Someone",
                    "duration": 200.0,
                    "thumbnail": None,
                    "webpage_url": "https://www.youtube.com/watch?v=ok",
                },
            ],
        }
    ).encode()
    proc = _make_proc(0, raw)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        resp = await client.post(
            "/api/v1/media/preview",
            json={"url": "https://www.youtube.com/playlist?list=PL123"},
        )
    assert resp.status_code == 200
    assert len(resp.json()["tracks"]) == 1


# ---------------------------------------------------------------------------
# Count / cap / normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_reports_total_and_truncation(client: AsyncClient) -> None:
    """playlist_count > returned entries => truncated with total_available set."""
    entries = [
        {
            "title": f"Artist - Track {i}",
            "uploader": "Artist",
            "duration": 100.0,
            "thumbnail": None,
            "webpage_url": f"https://www.youtube.com/watch?v=t{i}",
        }
        for i in range(2)
    ]
    raw = json.dumps(
        {"_type": "playlist", "title": "Big", "playlist_count": 137, "entries": entries}
    ).encode()
    proc = _make_proc(0, raw)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        resp = await client.post(
            "/api/v1/media/preview",
            json={"url": "https://www.youtube.com/playlist?list=PL123", "max_items": 2},
        )
    body = resp.json()
    assert body["total_available"] == 137
    assert body["truncated"] is True
    assert len(body["tracks"]) == 2


@pytest.mark.asyncio
async def test_preview_passes_playlist_end_cap(client: AsyncClient) -> None:
    """max_items is forwarded to yt-dlp as --playlist-end."""
    exec_mock = AsyncMock(return_value=_make_proc(0, _playlist_json()))
    with patch("asyncio.create_subprocess_exec", new=exec_mock):
        await client.post(
            "/api/v1/media/preview",
            json={"url": "https://www.youtube.com/playlist?list=PL123", "max_items": 7},
        )
    cmd = list(exec_mock.call_args.args)
    assert "--playlist-end" in cmd
    assert cmd[cmd.index("--playlist-end") + 1] == "7"


@pytest.mark.asyncio
async def test_preview_normalizes_radio_url(client: AsyncClient) -> None:
    """A radio/mix URL is stripped to the single video before yt-dlp runs."""
    exec_mock = AsyncMock(return_value=_make_proc(0, _single_video_json()))
    with patch("asyncio.create_subprocess_exec", new=exec_mock):
        await client.post(
            "/api/v1/media/preview",
            json={
                "url": (
                    "https://m.youtube.com/watch?v=Sd4SJVsTulc"
                    "&list=RDSd4SJVsTulc&start_radio=1&pp=oAcB"
                )
            },
        )
    passed_url = list(exec_mock.call_args.args)[-1]
    assert passed_url == "https://m.youtube.com/watch?v=Sd4SJVsTulc"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_ytdlp_nonzero_exit(client: AsyncClient) -> None:
    """yt-dlp non-zero exit code returns 422."""
    proc = _make_proc(1, b"", b"ERROR: Video unavailable")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        resp = await client.post(
            "/api/v1/media/preview",
            json={"url": "https://www.youtube.com/watch?v=bad"},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PREVIEW_FAILED"


@pytest.mark.asyncio
async def test_preview_invalid_json(client: AsyncClient) -> None:
    """yt-dlp returning non-JSON output returns 422."""
    proc = _make_proc(0, b"not json at all")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        resp = await client.post(
            "/api/v1/media/preview",
            json={"url": "https://www.youtube.com/watch?v=bad"},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PREVIEW_FAILED"
