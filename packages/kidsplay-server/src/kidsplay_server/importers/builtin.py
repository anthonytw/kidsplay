"""Importers that ship with the core server.

``LocalImporter`` reads files already on the server; ``HttpImporter`` downloads
a plain HTTP(S) file URL. Both are always installed and act as fallbacks:
plugin importers are asked first (see ``registry.discover_importers``).
"""

from pathlib import Path
from urllib.parse import urlparse

from kidsplay_server.processing.download import download_from_url

from .base import BaseImporter, FetchContext, FetchedItem, Importer


class LocalImporter(BaseImporter):
    """Import a file or a directory tree from the server's own filesystem."""

    name = "local"
    label = "Local file or folder"

    def can_handle(self, source: str) -> bool:
        """Return ``True`` for an existing server-side path.

        Args:
            source: A path (URLs are never handled).

        Returns:
            Whether *source* names an existing file or directory.
        """
        if "://" in source:
            return False
        return Path(source).exists()

    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        """Return the file, or every media file under the directory, in place.

        Nothing is copied into *workdir*: the pipeline reads the originals.

        Args:
            source: Existing file or directory path.
            workdir: Unused.
            ctx: Unused.

        Returns:
            One item per file, sorted by path.

        Raises:
            FileNotFoundError: If *source* no longer exists.
        """
        # Deferred: the pipeline imports the importer registry.
        from kidsplay_server.processing.pipeline import MEDIA_EXTENSIONS

        path = Path(source)
        if path.is_file():
            return [FetchedItem(path=path)]
        if path.is_dir():
            return [
                FetchedItem(path=p)
                for p in sorted(path.rglob("*"))
                if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
            ]
        raise FileNotFoundError(f"Path not found: {source}")


class HttpImporter(BaseImporter):
    """Download a single file from a plain HTTP(S) URL."""

    name = "http"
    label = "Web URL"

    def can_handle(self, source: str) -> bool:
        """Return ``True`` for any ``http://`` or ``https://`` URL.

        Args:
            source: A URL or path.

        Returns:
            Whether *source* is an HTTP(S) URL with a host.
        """
        parsed = urlparse(source)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        """Download *source* into *workdir*.

        Args:
            source: HTTP(S) URL.
            workdir: Directory to write the download into.
            ctx: Unused.

        Returns:
            The single downloaded file.

        Raises:
            RuntimeError: On an HTTP error status or a network failure.
        """
        return [FetchedItem(path=await download_from_url(source, workdir))]


def builtin_importers() -> list[Importer]:
    """Return fresh instances of the core importers, in resolution order.

    Returns:
        ``[LocalImporter(), HttpImporter()]``.
    """
    return [LocalImporter(), HttpImporter()]
