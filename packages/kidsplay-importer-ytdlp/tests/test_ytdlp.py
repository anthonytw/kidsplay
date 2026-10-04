"""Tests for kidsplay_importer_ytdlp.ytdlp (moved from the server package).

Subprocess calls for yt-dlp are mocked with unittest.mock.patch so no real
network or process is invoked.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kidsplay_importer_ytdlp.preview import yt_dlp_json
from kidsplay_importer_ytdlp.ytdlp import (
    cookies_available,
    cookies_path,
    extract_from_youtube,
    is_auth_error,
    is_youtube_url,
    normalize_youtube_url,
    sleep_config_for_attempt,
    yt_dlp_base_cmd,
    yt_dlp_cmd,
)

# ---------------------------------------------------------------------------
# is_youtube_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=abc123", True),
        ("https://youtu.be/abc123", True),
        ("https://m.youtube.com/watch?v=abc123", True),
        ("https://youtube.com/watch?v=abc123", True),
        ("https://music.youtube.com/watch?v=abc123", True),
        ("https://www.youtube-nocookie.com/embed/abc123", True),
        ("https://notyoutube.com/watch?v=abc123", False),
        ("https://example.com/song.mp3", False),
        ("https://vimeo.com/123456", False),
        ("http://localhost:8000/file.mp3", False),
    ],
)
def test_is_youtube_url(url: str, expected: bool) -> None:
    assert is_youtube_url(url) == expected


def test_plugin_and_core_share_one_host_list() -> None:
    """The core refuses exactly the hosts the plugin claims."""
    from kidsplay_server.importers import KNOWN_SOURCES, YOUTUBE_HOSTS

    (youtube,) = KNOWN_SOURCES
    assert youtube.hosts is YOUTUBE_HOSTS
    assert all(is_youtube_url(f"https://{host}/x") for host in YOUTUBE_HOSTS)


# ---------------------------------------------------------------------------
# normalize_youtube_url
# ---------------------------------------------------------------------------


def test_normalize_strips_radio_and_tracking_params() -> None:
    """The reported bug: a radio-decorated watch link becomes a single video."""
    url = (
        "https://m.youtube.com/watch?v=Sd4SJVsTulc&list=RDSd4SJVsTulc"
        "&start_radio=1&pp=oAcB"
    )
    assert normalize_youtube_url(url) == "https://m.youtube.com/watch?v=Sd4SJVsTulc"


def test_normalize_preserves_curated_playlist() -> None:
    """A real (non-RD) playlist id is kept so playlist imports still work."""
    url = "https://www.youtube.com/watch?v=abc123&list=PL0123456789&index=4"
    # start_radio/pp/index dropped, but the PL list survives.
    assert normalize_youtube_url(url) == (
        "https://www.youtube.com/watch?v=abc123&list=PL0123456789"
    )


def test_normalize_playlist_only_url_is_untouched() -> None:
    url = "https://www.youtube.com/playlist?list=PLabc"
    assert normalize_youtube_url(url) == url


def test_normalize_plain_watch_url_unchanged() -> None:
    url = "https://www.youtube.com/watch?v=abc123"
    assert normalize_youtube_url(url) == url


def test_normalize_non_youtube_url_unchanged() -> None:
    url = "https://example.com/song.mp3?pp=x&start_radio=1"
    assert normalize_youtube_url(url) == url


# ---------------------------------------------------------------------------
# extract_from_youtube
# ---------------------------------------------------------------------------


def _make_proc(returncode: int, stderr: bytes = b"") -> MagicMock:
    """Return a mock asyncio.Process with the given returncode and stderr."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


@pytest.mark.asyncio
async def test_extract_from_youtube_success(tmp_path: Path) -> None:
    """Returns (audio_path, thumbnail_path) when yt-dlp exits 0."""
    mp3 = tmp_path / "My Song.mp3"
    jpg = tmp_path / "My Song.jpg"
    mp3.write_bytes(b"audio")
    jpg.write_bytes(b"thumb")

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_make_proc(0),
    ):
        audio, thumb = await extract_from_youtube("https://youtu.be/abc123", tmp_path)

    assert audio == mp3
    assert thumb == jpg


@pytest.mark.asyncio
async def test_extract_from_youtube_no_thumbnail(tmp_path: Path) -> None:
    """thumbnail_path is None when yt-dlp writes no .jpg file."""
    mp3 = tmp_path / "My Song.mp3"
    mp3.write_bytes(b"audio")

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_make_proc(0),
    ):
        audio, thumb = await extract_from_youtube("https://youtu.be/abc123", tmp_path)

    assert audio == mp3
    assert thumb is None


@pytest.mark.asyncio
async def test_extract_from_youtube_nonzero_exit(tmp_path: Path) -> None:
    """RuntimeError is raised when yt-dlp exits non-zero."""
    with (
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_proc(1, b"ERROR: Video unavailable"),
        ),
        pytest.raises(RuntimeError, match="yt-dlp failed"),
    ):
        await extract_from_youtube("https://youtu.be/bad", tmp_path)


@pytest.mark.asyncio
async def test_extract_from_youtube_no_mp3(tmp_path: Path) -> None:
    """RuntimeError is raised when yt-dlp exits 0 but writes no .mp3."""
    with (
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_proc(0),
        ),
        pytest.raises(RuntimeError, match="no .mp3 found"),
    ):
        await extract_from_youtube("https://youtu.be/abc123", tmp_path)


# ---------------------------------------------------------------------------
# Cookies and command building
# ---------------------------------------------------------------------------


def test_cookies_path_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIDSPLAY_YT_COOKIES", raising=False)
    assert cookies_path() is None
    assert not cookies_available()
    assert yt_dlp_cmd("-J", use_cookies=True) == [*yt_dlp_base_cmd(), "-J"]


def test_cookies_used_only_when_requested_and_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookies = tmp_path / "cookies.txt"
    monkeypatch.setenv("KIDSPLAY_YT_COOKIES", str(cookies))
    assert cookies_path() == cookies
    # Configured but missing: never passed.
    assert not cookies_available()
    assert "--cookies" not in yt_dlp_cmd(use_cookies=True)

    cookies.write_text("# Netscape HTTP Cookie File")
    assert cookies_available()
    assert yt_dlp_cmd("-J", use_cookies=True) == [
        *yt_dlp_base_cmd(),
        "--cookies",
        str(cookies),
        "-J",
    ]
    assert "--cookies" not in yt_dlp_cmd("-J")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ERROR: Private video. Sign in if you've been granted access", True),
        ("Use --cookies-from-browser or --cookies for the authentication", True),
        ("ERROR: Video unavailable", False),
    ],
)
def test_is_auth_error(text: str, expected: bool) -> None:
    assert is_auth_error(text) is expected


def test_sleep_config_grows_with_attempts() -> None:
    assert sleep_config_for_attempt(1).as_args() == []
    assert sleep_config_for_attempt(2).as_args() == [
        "--sleep-requests",
        "1",
        "--min-sleep-interval",
        "5",
        "--max-sleep-interval",
        "10",
    ]
    # Past the table, the last (most conservative) entry repeats.
    assert sleep_config_for_attempt(99).as_args() == (
        sleep_config_for_attempt(6).as_args()
    )


# ---------------------------------------------------------------------------
# The server's resource limiter applies to yt-dlp (and so to its ffmpeg)
# ---------------------------------------------------------------------------


@pytest.fixture
def limits() -> Iterator[None]:
    from kidsplay_server.processing.resources import ResourceLimits, configure_limits

    configure_limits(ResourceLimits(nice=10, max_jobs=1))
    yield
    configure_limits(ResourceLimits())


@pytest.mark.asyncio
async def test_ytdlp_runs_at_the_configured_niceness(
    tmp_path: Path, limits: None
) -> None:
    (tmp_path / "Song.mp3").write_bytes(b"audio")
    exec_mock = AsyncMock(return_value=_make_proc(0))
    with patch("asyncio.create_subprocess_exec", new=exec_mock):
        await extract_from_youtube("https://youtu.be/abc123", tmp_path)
    argv = exec_mock.call_args.args
    assert argv[0].endswith("nice")
    assert argv[1:3] == ("-n", "10")


@pytest.mark.asyncio
async def test_ytdlp_download_waits_for_a_free_job_slot(
    tmp_path: Path, limits: None
) -> None:
    """With one job allowed, yt-dlp does not start beside a running normalization."""
    from kidsplay_server.processing import resources

    (tmp_path / "Song.mp3").write_bytes(b"audio")
    slots = resources._slots
    assert slots is not None
    slots.acquire()  # a normalization encode is running
    exec_mock = AsyncMock(return_value=_make_proc(0))
    with patch("asyncio.create_subprocess_exec", new=exec_mock):
        task = asyncio.create_task(
            extract_from_youtube("https://youtu.be/abc123", tmp_path)
        )
        await asyncio.sleep(0.5)
        assert not exec_mock.called
        slots.release()
        await asyncio.wait_for(task, 5)
    assert exec_mock.called
    # The slot is free again for the next job.
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_preview_waits_for_a_free_job_slot(limits: None) -> None:
    from kidsplay_server.processing import resources

    slots = resources._slots
    assert slots is not None
    slots.acquire()
    proc = _make_proc(0)
    proc.communicate = AsyncMock(return_value=(b"{}", b""))
    exec_mock = AsyncMock(return_value=proc)
    with patch("asyncio.create_subprocess_exec", new=exec_mock):
        task = asyncio.create_task(yt_dlp_json("https://youtu.be/abc123"))
        await asyncio.sleep(0.5)
        assert not exec_mock.called
        slots.release()
        data, _debug = await asyncio.wait_for(task, 5)
    assert data == {}


def test_yt_dlp_runs_without_uv_on_path() -> None:
    """A systemd unit's PATH has no uv (all-in-one); yt-dlp must still start."""
    cmd = yt_dlp_cmd("--version")
    assert "uv" not in cmd
    bin_dir = Path(sys.executable).parent
    assert cmd[1].startswith(f"PATH={bin_dir}{os.pathsep}")
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
