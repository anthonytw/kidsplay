"""yt-dlp wrappers: URL matching and normalization, cookies, audio extraction.

``extract_from_youtube`` runs yt-dlp as a subprocess, writing the MP3 and
thumbnail into a caller-supplied directory (typically a
``tempfile.TemporaryDirectory``), and raises ``RuntimeError`` on failure so
callers can surface a clear error message without inspecting subprocess state.

Environment variables
---------------------
KIDSPLAY_YT_COOKIES
    Optional path to a Netscape-format cookies.txt file passed to yt-dlp
    via ``--cookies``.  Export it from a logged-in browser session using the
    "Get cookies.txt LOCALLY" extension (Chrome) or "cookies.txt" (Firefox).
    When unset, yt-dlp runs without authentication (some videos may fail).
"""

import logging
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from kidsplay_server.importers import YOUTUBE_HOSTS
from kidsplay_server.processing.resources import run_limited_async

logger = logging.getLogger(__name__)


def cookies_path() -> Path | None:
    """Return the configured yt-dlp cookies file, if any.

    Read from ``KIDSPLAY_YT_COOKIES`` on every call, so tests and long-running
    servers see changes without a restart.

    Returns:
        The configured path (which may not exist), or ``None`` when unset.
    """
    value = os.environ.get("KIDSPLAY_YT_COOKIES", "")
    return Path(value) if value else None


def cookies_available() -> bool:
    """Return ``True`` if a cookies file is configured and exists.

    Returns:
        Whether ``yt_dlp_cmd(..., use_cookies=True)`` would add ``--cookies``.
    """
    path = cookies_path()
    return path is not None and path.is_file()


def yt_dlp_base_cmd() -> list[str]:
    """The command that starts the yt-dlp installed next to this server.

    Runs the venv's own ``yt-dlp`` script by absolute path, with the venv's
    ``bin`` first on ``PATH`` so yt-dlp finds the ``deno`` it uses for YouTube.
    Nothing here needs ``uv`` at run time: a systemd unit (all-in-one) has no
    ``~/.local/bin`` on its ``PATH``, and ``uv run`` would also re-sync the
    environment. Falls back to ``python -m yt_dlp`` when the script is missing.

    Returns:
        The command prefix, to which yt-dlp arguments are appended.
    """
    # Not resolved: the venv's python is a symlink to the system one, and
    # resolving it would point at /usr/bin instead of the venv.
    bin_dir = Path(sys.executable).parent
    search_path = os.pathsep.join([str(bin_dir), os.environ.get("PATH", os.defpath)])
    script = bin_dir / "yt-dlp"
    program = [str(script)] if script.is_file() else [sys.executable, "-m", "yt_dlp"]
    return ["env", f"PATH={search_path}", *program]


def yt_dlp_cmd(*args: str, use_cookies: bool = False) -> list[str]:
    """Build a yt-dlp command with optional cookie flags.

    Starts with :func:`yt_dlp_base_cmd` and, when *use_cookies* is True
    and ``KIDSPLAY_YT_COOKIES`` is configured and the file exists, appends
    ``["--cookies", <path>]`` before any caller-supplied *args*.

    Cookies are omitted by default; pass ``use_cookies=True`` only as a
    fallback when authentication is required (e.g. private videos).

    Args:
        *args: Additional yt-dlp flags and positional arguments.
        use_cookies: When ``True``, append ``--cookies <path>`` if a cookies
            file is configured and exists.

    Returns:
        Complete command list, to run with ``run_limited_async``.
    """
    cmd = yt_dlp_base_cmd()
    path = cookies_path()
    if use_cookies and path is not None and path.is_file():
        cmd += ["--cookies", str(path)]
    cmd += list(args)
    return cmd


_AUTH_ERROR_PHRASES: tuple[str, ...] = (
    "Private video",
    "Sign in if you've been granted access",
    "Use --cookies",
    "--cookies-from-browser",
)


def is_auth_error(text: str) -> bool:
    """Return ``True`` if *text* contains a yt-dlp authentication error.

    Used to decide whether to retry a failed download with cookie
    authentication.

    Args:
        text: Error message or stderr output from yt-dlp.

    Returns:
        ``True`` when the text indicates authentication is required.
    """
    lower = text.lower()
    return any(phrase.lower() in lower for phrase in _AUTH_ERROR_PHRASES)


def is_youtube_url(url: str) -> bool:
    """Return True if *url* points to a YouTube video.

    Args:
        url: URL string to test.

    Returns:
        ``True`` for the hosts in ``YOUTUBE_HOSTS`` (youtube.com, youtu.be, ...).
    """
    hostname = urlparse(url).hostname or ""
    return hostname in YOUTUBE_HOSTS


# Query params that turn a plain "watch one video" link into an auto-playing
# radio/mix or carry tracking noise. Dropping these keeps a single-song URL a
# single song instead of an unbounded mix.
_YOUTUBE_STRIP_PARAMS: frozenset[str] = frozenset(
    {"start_radio", "pp", "index", "feature"}
)


def _is_radio_list(list_id: str) -> bool:
    """Return True if a YouTube ``list`` id is an auto-generated radio/mix.

    YouTube reserves the ``RD`` prefix for radio and "Mix" playlists (``RDMM``,
    ``RDCLAK``, ``RDEM``, …), which are dynamically generated and effectively
    unbounded. User/curated playlists use other prefixes (``PL``, ``OLAK``,
    ``UU``, ``LL``, ``FL``, ``WL``) and are preserved.

    Args:
        list_id: Value of the ``list`` query parameter.

    Returns:
        ``True`` for radio/mix lists that should be stripped.
    """
    return list_id.startswith("RD")


def normalize_youtube_url(url: str) -> str:
    """Strip radio/mix and tracking params from a YouTube URL.

    Removes ``start_radio``, ``pp``, ``index`` and ``feature`` query params, and
    drops a ``list`` param when it names an auto-generated radio/mix (``RD*``).
    Without this, pasting a "watch" link that YouTube has decorated with a radio
    mix (``…&list=RDxxxx&start_radio=1&pp=…``) makes yt-dlp expand it into
    hundreds of tracks instead of the one video the user picked.

    Non-YouTube URLs and URLs without offending params are returned unchanged.
    Curated playlists (non-``RD`` ``list`` ids) are preserved so genuine
    playlist imports still work.

    Args:
        url: A URL, possibly a decorated YouTube link.

    Returns:
        The cleaned URL (unchanged if nothing needed stripping).
    """
    if not is_youtube_url(url):
        return url

    parsed = urlparse(url)
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in _YOUTUBE_STRIP_PARAMS:
            continue
        if key == "list" and _is_radio_list(value):
            continue
        kept.append((key, value))

    new_query = urlencode(kept)
    if new_query == parsed.query:
        return url

    cleaned = urlunparse(parsed._replace(query=new_query))
    logger.info("Normalized YouTube URL: %s -> %s", url, cleaned)
    return cleaned


class YtDlpResult:
    """Result of a yt-dlp extraction, including full command and output logs."""

    def __init__(
        self,
        audio_path: Path,
        thumbnail_path: Path | None,
        command: list[str],
        stdout: str,
        stderr: str,
        returncode: int,
    ) -> None:
        self.audio_path = audio_path
        self.thumbnail_path = thumbnail_path
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class SleepConfig:
    """Sleep/rate-limit parameters for yt-dlp."""

    def __init__(
        self,
        sleep_requests: int | None = None,
        min_sleep_interval: int | None = None,
        max_sleep_interval: int | None = None,
    ) -> None:
        self.sleep_requests = sleep_requests
        self.min_sleep_interval = min_sleep_interval
        self.max_sleep_interval = max_sleep_interval

    def as_args(self) -> list[str]:
        """Return yt-dlp CLI flags for these sleep settings."""
        args: list[str] = []
        if self.sleep_requests is not None:
            args += ["--sleep-requests", str(self.sleep_requests)]
        if self.min_sleep_interval is not None:
            args += ["--min-sleep-interval", str(self.min_sleep_interval)]
        if self.max_sleep_interval is not None:
            args += ["--max-sleep-interval", str(self.max_sleep_interval)]
        return args


# Default sleep config used by the non-queued path
_DEFAULT_SLEEP = SleepConfig(
    sleep_requests=5, min_sleep_interval=60, max_sleep_interval=90
)


def sleep_config_for_attempt(attempt: int) -> SleepConfig:
    """Return progressively more conservative sleep settings per attempt.

    Attempt 1: no sleep (fast first try).
    Attempt 2+: incrementally higher delays.

    Args:
        attempt: 1-based attempt number.

    Returns:
        SleepConfig for this attempt.
    """
    configs = [
        SleepConfig(),  # attempt 1: no sleep
        SleepConfig(sleep_requests=1, min_sleep_interval=5, max_sleep_interval=10),
        SleepConfig(sleep_requests=3, min_sleep_interval=15, max_sleep_interval=30),
        SleepConfig(sleep_requests=5, min_sleep_interval=30, max_sleep_interval=60),
        SleepConfig(sleep_requests=5, min_sleep_interval=60, max_sleep_interval=90),
        SleepConfig(sleep_requests=10, min_sleep_interval=90, max_sleep_interval=120),
    ]
    idx = min(attempt - 1, len(configs) - 1)
    return configs[max(0, idx)]


async def extract_from_youtube(
    url: str,
    dest_dir: Path,
    *,
    sleep: SleepConfig | None = None,
    verbose: bool = False,
    use_cookies: bool = False,
) -> tuple[Path, Path | None]:
    """Extract audio and thumbnail from a YouTube URL using yt-dlp.

    Args:
        url: YouTube video URL.
        dest_dir: Directory to write the MP3 and optional thumbnail into.
        sleep: Sleep/rate-limit config. Uses default conservative settings
            if not provided.
        verbose: If True, pass ``--verbose`` to yt-dlp for detailed logging.
        use_cookies: If True, pass the configured cookies file to yt-dlp.

    Returns:
        ``(audio_path, thumbnail_path)`` where ``thumbnail_path`` is ``None``
        if yt-dlp did not write a thumbnail file.

    Raises:
        RuntimeError: If yt-dlp exits with a non-zero return code or if no
            MP3 file is found after a successful run.
    """
    result = await extract_from_youtube_full(
        url, dest_dir, sleep=sleep, verbose=verbose, use_cookies=use_cookies
    )
    return result.audio_path, result.thumbnail_path


async def extract_from_youtube_full(
    url: str,
    dest_dir: Path,
    *,
    sleep: SleepConfig | None = None,
    verbose: bool = False,
    use_cookies: bool = False,
) -> YtDlpResult:
    """Extract audio and thumbnail, returning full command/output details.

    Like ``extract_from_youtube`` but returns a ``YtDlpResult`` with the
    command list, stdout, stderr, and return code for logging purposes.

    Args:
        url: YouTube video URL.
        dest_dir: Directory to write the MP3 and optional thumbnail into.
        sleep: Sleep/rate-limit config. Uses default conservative settings
            if not provided.
        verbose: If True, pass ``--verbose`` to yt-dlp for detailed logging.
        use_cookies: If True, pass the configured cookies file to yt-dlp.

    Returns:
        ``YtDlpResult`` with audio_path, thumbnail_path, and debug info.

    Raises:
        RuntimeError: If yt-dlp exits with a non-zero return code or if no
            MP3 file is found after a successful run.
    """
    if sleep is None:
        sleep = _DEFAULT_SLEEP

    logger.info(
        "yt-dlp extracting: %s%s", url, " (with cookies)" if use_cookies else ""
    )
    output_template = str(dest_dir / "%(title)s.%(ext)s")
    extra_args: list[str] = []
    if verbose:
        extra_args.append("--verbose")
    extra_args += sleep.as_args()

    cmd = yt_dlp_cmd(
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--js-runtimes",
        "deno",
        "--remote-components",
        "ejs:github",
        *extra_args,
        "--no-playlist",
        "-o",
        output_template,
        url,
        use_cookies=use_cookies,
    )
    # Through the server's limiter: one job slot for the whole run, ffmpeg
    # post-processing included, at the configured niceness.
    proc = await run_limited_async(cmd)
    stdout_text = proc.stdout.decode(errors="replace")
    stderr_text = proc.stderr.decode(errors="replace")

    if proc.returncode != 0:
        logger.warning(
            "yt-dlp failed (exit %d) for %s: %s",
            proc.returncode,
            url,
            stderr_text[:500],
        )
        raise RuntimeError(f"yt-dlp failed (exit {proc.returncode}): {stderr_text}")

    mp3_files = sorted(dest_dir.glob("*.mp3"))
    jpg_files = sorted(dest_dir.glob("*.jpg"))

    if not mp3_files:
        raise RuntimeError(
            f"yt-dlp completed but no .mp3 found in {dest_dir}. stderr: {stderr_text}"
        )

    audio_path = mp3_files[0]
    thumbnail_path = jpg_files[0] if jpg_files else None
    logger.info(
        "yt-dlp extracted: audio=%s thumbnail=%s",
        audio_path.name,
        thumbnail_path.name if thumbnail_path else None,
    )
    return YtDlpResult(
        audio_path=audio_path,
        thumbnail_path=thumbnail_path,
        command=cmd,
        stdout=stdout_text,
        stderr=stderr_text,
        returncode=proc.returncode or 0,
    )
