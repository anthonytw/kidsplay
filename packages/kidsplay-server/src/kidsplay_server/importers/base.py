"""The importer contract: how a media source hands files to the core.

An *importer* knows how to fetch media from one kind of source (a server-side
path, a plain HTTP URL, YouTube, …) into a scratch directory. Everything after
that (hashing, transcoding, thumbnails, storage) is the core pipeline's job, so
an importer only ever *fetches*.

Importers are discovered through the ``kidsplay.importers`` entry-point group
(see ``kidsplay_server.importers.registry``) and described in
``docs/IMPORTERS.md``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


@dataclass
class FetchedItem:
    """One file an importer fetched, plus whatever metadata it knows.

    Attributes:
        path: The fetched media file. Usually inside the ``workdir`` passed to
            ``Importer.fetch``; the pipeline reads it before that directory is
            removed.
        title: Title to use when the caller gave no override. ``None`` lets
            the pipeline read it from the file's tags.
        artist: Artist to use when the caller gave no override.
        thumbnail: Artwork image used when the file has no embedded artwork.
    """

    path: Path
    title: str | None = None
    artist: str | None = None
    thumbnail: Path | None = None


@dataclass
class FetchContext:
    """Per-call information passed to ``Importer.fetch``.

    Attributes:
        attempt: 1-based attempt number. Above 1 only when the import queue
            retries a failed fetch, so a rate-limited importer can back off.
        queued: ``True`` when the fetch runs in the background queue worker
            rather than inside an HTTP request.
        log_lines: Free-form debug output. The queue stores it with the job so
            the operator can see what happened on each attempt.
    """

    attempt: int = 1
    queued: bool = False
    log_lines: list[str] = field(default_factory=list)

    def log(self, text: str) -> None:
        """Append *text* to the attempt log shown on the queue page.

        Args:
            text: One or more lines of debug output.
        """
        self.log_lines.append(text)

    @property
    def text(self) -> str:
        """The collected log output as a single string."""
        return "\n".join(self.log_lines)


@runtime_checkable
class Importer(Protocol):
    """A pluggable media source.

    Subclass ``BaseImporter`` for sensible defaults, or implement every member
    below directly.

    Attributes:
        name: Stable machine name, unique across installed importers. Stored
            with queued jobs, so do not rename it once released.
        label: Human-readable name shown in the web UI and the CLI.
        requires_queue: ``True`` for slow or rate-limited sources. The web UI
            sends them through the background import queue, which retries
            failed attempts, instead of fetching inside the HTTP request.
    """

    name: str
    label: str
    requires_queue: bool

    def can_handle(self, source: str) -> bool:
        """Return ``True`` if this importer can fetch *source*.

        Args:
            source: A URL or a server-side path, as the user entered it.

        Returns:
            Whether ``fetch`` should be tried for *source*.
        """
        ...

    def normalize(self, source: str) -> str:
        """Return the canonical form of *source* to store and fetch.

        Args:
            source: A source this importer can handle.

        Returns:
            The cleaned-up source (e.g. with tracking parameters removed).
        """
        ...

    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        """Fetch *source* into *workdir*.

        Args:
            source: A source this importer can handle.
            workdir: Empty scratch directory, deleted after ingest.
            ctx: Attempt number and a log for debug output.

        Returns:
            The fetched files, at least one.

        Raises:
            Exception: Any error fails the fetch; its message is shown to the
                user and, for queued jobs, triggers a retry.
        """
        ...


class BaseImporter(ABC):
    """Convenience base class supplying the optional parts of ``Importer``.

    Subclasses set ``name`` and ``label`` and implement ``can_handle`` and
    ``fetch``; ``requires_queue`` defaults to ``False`` and ``normalize`` to
    the identity.
    """

    name: str = ""
    label: str = ""
    requires_queue: bool = False

    @abstractmethod
    def can_handle(self, source: str) -> bool:
        """Return ``True`` if this importer can fetch *source*.

        Args:
            source: A URL or a server-side path, as the user entered it.

        Returns:
            Whether ``fetch`` should be tried for *source*.
        """

    def normalize(self, source: str) -> str:
        """Return *source* unchanged.

        Args:
            source: A source this importer can handle.

        Returns:
            *source* itself.
        """
        return source

    @abstractmethod
    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        """Fetch *source* into *workdir*.

        Args:
            source: A source this importer can handle.
            workdir: Empty scratch directory, deleted after ingest.
            ctx: Attempt number and a log for debug output.

        Returns:
            The fetched files, at least one.
        """


# ---------------------------------------------------------------------------
# Preview (optional capability)
# ---------------------------------------------------------------------------


class TrackPreview(BaseModel):
    """Metadata for a single track found by a preview."""

    url: str
    title: str
    artist: str
    duration_seconds: float | None = None
    thumbnail_url: str | None = None


class PreviewDebug(BaseModel):
    """Debug information about how the preview was produced."""

    command: list[str]
    returncode: int
    stderr: str


class ImportPreview(BaseModel):
    """What a source contains, fetched without downloading any media.

    ``tracks`` is capped at the requested ``max_items``. ``total_available`` is
    the full length of the source list when known (``None`` otherwise), and
    ``truncated`` is ``True`` when more tracks exist than were returned, so the
    UI can show "X of N" and offer to fetch more.
    """

    is_playlist: bool
    playlist_title: str | None = None
    tracks: list[TrackPreview]
    total_available: int | None = None
    truncated: bool = False
    debug: PreviewDebug


class PreviewError(Exception):
    """Raised by ``SupportsPreview.preview`` when a preview cannot be produced.

    Attributes:
        detail: Human-readable reason shown to the user.
        debug: Optional details of the failed attempt.
    """

    def __init__(self, detail: str, debug: PreviewDebug | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.debug = debug


@runtime_checkable
class SupportsPreview(Protocol):
    """Optional importer capability: list a source's tracks before importing.

    The web UI shows a preview-and-queue form for every importer that
    implements this.
    """

    async def preview(self, source: str, max_items: int) -> ImportPreview:
        """Describe *source* without downloading it.

        Args:
            source: A source the importer can handle.
            max_items: Maximum number of playlist entries to return.

        Returns:
            The preview.

        Raises:
            PreviewError: If the source cannot be previewed.
        """
        ...


# ---------------------------------------------------------------------------
# Description for the API
# ---------------------------------------------------------------------------


class ImporterInfo(BaseModel):
    """Public description of an installed importer (``GET /importers``)."""

    name: str
    label: str
    requires_queue: bool
    supports_preview: bool

    @classmethod
    def of(cls, importer: Importer) -> "ImporterInfo":
        """Describe *importer*.

        Args:
            importer: An installed importer.

        Returns:
            Its public description.
        """
        return cls(
            name=importer.name,
            label=importer.label,
            requires_queue=importer.requires_queue,
            supports_preview=isinstance(importer, SupportsPreview),
        )
