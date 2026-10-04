"""Plain HTTP download for URL-based ingest.

``download_from_url`` writes the file into a caller-supplied directory
(typically a ``tempfile.TemporaryDirectory``) and raises ``RuntimeError`` on
failure so callers can surface a clear error message. Other sources, such as
YouTube, are handled by importer plugins (see ``kidsplay_server.importers``).
"""

import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/wav": ".wav",
}


async def download_from_url(url: str, dest_dir: Path) -> Path:
    """Download a file from *url* into *dest_dir*.

    The filename is taken from the URL path component. When the URL path
    has no recognisable extension, the Content-Type header is used to
    pick one. Falls back to no extension if neither provides a match.

    Args:
        url: HTTP/HTTPS URL to fetch.
        dest_dir: Directory to write the downloaded file into.

    Returns:
        Path to the downloaded file inside *dest_dir*.

    Raises:
        RuntimeError: If the server returns a 4xx/5xx status, or if a
            network-level error occurs.
    """
    try:
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=300) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Download failed: HTTP {resp.status_code} for {url}"
                )
            url_path = urlparse(url).path
            name = Path(url_path).name or "download"
            if not Path(name).suffix:
                ct = resp.headers.get("content-type", "")
                ext = _CONTENT_TYPE_EXT.get(ct.split(";")[0].strip(), "")
                name = f"download{ext}"
            dest = dest_dir / name
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Download failed for {url}: {exc}") from exc

    logger.info("Downloaded %s → %s (%d bytes)", url, dest.name, dest.stat().st_size)
    return dest
