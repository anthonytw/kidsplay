"""Tests for kidsplay_server.processing.download.

httpx calls are mocked with unittest.mock.patch so no real network is used.
The yt-dlp tests live with the plugin, in packages/kidsplay-importer-ytdlp.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kidsplay_server.processing.download import download_from_url

# ---------------------------------------------------------------------------
# download_from_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_from_url_success(tmp_path: Path) -> None:
    """File is written to dest_dir with the filename from the URL path."""
    content = b"fake audio data"

    # Build a mock response that streams content.
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "audio/mpeg"}

    async def _aiter_bytes(chunk_size: int = 65536) -> AsyncIterator[bytes]:
        yield content

    mock_response.aiter_bytes = _aiter_bytes

    mock_stream_cm = MagicMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_cm)

    mock_client_cm = MagicMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "kidsplay_server.processing.download.httpx.AsyncClient",
        return_value=mock_client_cm,
    ):
        result = await download_from_url("https://example.com/song.mp3", tmp_path)

    assert result == tmp_path / "song.mp3"
    assert result.read_bytes() == content


@pytest.mark.asyncio
async def test_download_from_url_uses_content_type_ext(tmp_path: Path) -> None:
    """When URL path has no extension, Content-Type determines the suffix."""
    content = b"image bytes"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/jpeg"}

    async def _aiter_bytes(chunk_size: int = 65536) -> AsyncIterator[bytes]:
        yield content

    mock_response.aiter_bytes = _aiter_bytes

    mock_stream_cm = MagicMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_cm)

    mock_client_cm = MagicMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "kidsplay_server.processing.download.httpx.AsyncClient",
        return_value=mock_client_cm,
    ):
        result = await download_from_url("https://example.com/download", tmp_path)

    assert result.suffix == ".jpg"
    assert result.read_bytes() == content


@pytest.mark.asyncio
async def test_download_from_url_http_error(tmp_path: Path) -> None:
    """A 404 response raises RuntimeError."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.headers = {}

    mock_stream_cm = MagicMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_cm)

    mock_client_cm = MagicMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "kidsplay_server.processing.download.httpx.AsyncClient",
            return_value=mock_client_cm,
        ),
        pytest.raises(RuntimeError, match="HTTP 404"),
    ):
        await download_from_url("https://example.com/missing.mp3", tmp_path)
