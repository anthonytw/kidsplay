"""The YouTube importer registered under the ``kidsplay.importers`` group."""

import logging
from pathlib import Path

from kidsplay_server.importers import (
    BaseImporter,
    FetchContext,
    FetchedItem,
    ImportPreview,
)

from .preview import preview_youtube
from .ytdlp import (
    YtDlpResult,
    cookies_available,
    extract_from_youtube,
    extract_from_youtube_full,
    is_auth_error,
    is_youtube_url,
    normalize_youtube_url,
    sleep_config_for_attempt,
)

logger = logging.getLogger(__name__)


def _format_result(result: YtDlpResult) -> str:
    """Render a yt-dlp run (command, exit code, output) for the queue log."""
    lines = [
        f"COMMAND: {' '.join(result.command)}",
        f"EXIT CODE: {result.returncode}",
    ]
    if result.stdout.strip():
        lines += ["--- STDOUT ---", result.stdout.strip()]
    if result.stderr.strip():
        lines += ["--- STDERR ---", result.stderr.strip()]
    return "\n".join(lines)


async def _fetch_queued(url: str, workdir: Path, ctx: FetchContext) -> YtDlpResult:
    """Download one URL with attempt-appropriate rate limiting.

    Tries without cookies first. If yt-dlp reports an authentication error
    (private video, sign-in required) and a cookies file is configured,
    retries once with cookies before propagating the failure.

    Args:
        url: YouTube URL.
        workdir: Directory for output files.
        ctx: Fetch context; ``ctx.attempt`` picks the sleep settings.

    Returns:
        YtDlpResult with audio path, thumbnail, and debug info.
    """
    sleep = sleep_config_for_attempt(ctx.attempt)
    try:
        return await extract_from_youtube_full(url, workdir, sleep=sleep, verbose=True)
    except RuntimeError as exc:
        if is_auth_error(str(exc)) and cookies_available():
            logger.info(
                "Auth error on attempt %d, retrying with cookies: %s",
                ctx.attempt,
                url,
            )
            return await extract_from_youtube_full(
                url, workdir, sleep=sleep, verbose=True, use_cookies=True
            )
        raise


class YtDlpImporter(BaseImporter):
    """Import audio from YouTube with yt-dlp.

    Queued fetches back off further on each retry and fall back to the
    ``KIDSPLAY_YT_COOKIES`` cookies file when YouTube asks for a sign-in.
    """

    name = "ytdlp"
    label = "YouTube"
    requires_queue = True

    def can_handle(self, source: str) -> bool:
        """Return ``True`` for ``youtube.com`` and ``youtu.be`` URLs.

        Args:
            source: A URL or path.

        Returns:
            Whether *source* is a YouTube URL.
        """
        return is_youtube_url(source)

    def normalize(self, source: str) -> str:
        """Strip radio/mix and tracking parameters from a YouTube URL.

        Args:
            source: A YouTube URL.

        Returns:
            The cleaned URL (see ``normalize_youtube_url``).
        """
        return normalize_youtube_url(source)

    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        """Extract the audio of one video as MP3, plus its thumbnail.

        Args:
            source: YouTube video URL.
            workdir: Directory yt-dlp writes into.
            ctx: Fetch context. Queued fetches use per-attempt sleep settings,
                verbose output and the cookie fallback, and log the yt-dlp
                command and output; direct fetches use yt-dlp's conservative
                default sleep.

        Returns:
            The extracted MP3, with the thumbnail when yt-dlp wrote one.

        Raises:
            RuntimeError: If yt-dlp fails or writes no MP3.
        """
        if ctx.queued:
            result = await _fetch_queued(source, workdir, ctx)
            ctx.log(_format_result(result))
            audio, thumbnail = result.audio_path, result.thumbnail_path
        else:
            audio, thumbnail = await extract_from_youtube(source, workdir)
        return [FetchedItem(path=audio, thumbnail=thumbnail)]

    async def preview(self, source: str, max_items: int) -> ImportPreview:
        """List the video or playlist entries at *source* without downloading.

        Args:
            source: YouTube video or playlist URL.
            max_items: Maximum number of playlist entries to return.

        Returns:
            The preview.

        Raises:
            PreviewError: If yt-dlp fails or returns unexpected output.
        """
        return await preview_youtube(source, max_items)
