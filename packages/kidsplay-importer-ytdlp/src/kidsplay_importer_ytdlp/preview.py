"""Playlist/video preview via ``yt-dlp -J`` (metadata only, no download)."""

import json
from collections.abc import Mapping

from kidsplay_server.importers import (
    ImportPreview,
    PreviewDebug,
    PreviewError,
    TrackPreview,
)
from kidsplay_server.processing.resources import run_limited_async

from .ytdlp import normalize_youtube_url, yt_dlp_cmd


def parse_yt_title(title: str, uploader: str) -> tuple[str, str]:
    """Split a YouTube title into (artist, title).

    If the title contains ``-``, the first occurrence splits artist (left)
    from title (right).  Otherwise ``uploader`` is used as the artist.

    Args:
        title: Raw video title from yt-dlp metadata.
        uploader: Channel/uploader name from yt-dlp metadata.

    Returns:
        ``(artist, title)`` tuple.
    """
    if "-" in title:
        idx = title.index("-")
        return title[:idx].strip(), title[idx + 1 :].strip()
    return uploader, title


def _json_str(data: Mapping[str, object], key: str) -> str | None:
    """Return ``data[key]`` if it is a string, else ``None``.

    yt-dlp JSON is untyped; this narrows a value before it reaches a typed field.

    Args:
        data: A decoded yt-dlp JSON object.
        key: Key to look up.

    Returns:
        The string value, or ``None`` if absent or not a string.
    """
    value = data.get(key)
    return value if isinstance(value, str) else None


def _json_float(data: Mapping[str, object], key: str) -> float | None:
    """Return ``data[key]`` as a float if it is a real number, else ``None``.

    Args:
        data: A decoded yt-dlp JSON object.
        key: Key to look up.

    Returns:
        The numeric value, or ``None`` if absent or not a number.
    """
    value = data.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


async def yt_dlp_json(*args: str) -> tuple[dict[str, object], PreviewDebug]:
    """Run yt-dlp with ``-J`` and return parsed JSON + debug info.

    Args:
        *args: Extra yt-dlp flags inserted before the URL (last positional).

    Returns:
        ``(data, debug)`` tuple.

    Raises:
        PreviewError: On subprocess or parse failure.
    """
    cmd = yt_dlp_cmd(
        "--js-runtimes",
        "deno",
        "--remote-components",
        "ejs:github",
        "-J",
        "--no-warnings",
        *args,
    )

    try:
        # Through the server's limiter, like every other yt-dlp run.
        proc = await run_limited_async(cmd)
        stdout, stderr = proc.stdout, proc.stderr
    except Exception as exc:
        raise PreviewError(
            f"yt-dlp unavailable: {exc}",
            PreviewDebug(command=cmd, returncode=-1, stderr=str(exc)),
        ) from exc

    debug = PreviewDebug(
        command=cmd,
        returncode=proc.returncode or 0,
        stderr=stderr.decode(errors="replace"),
    )

    if proc.returncode != 0:
        raise PreviewError(stderr.decode(errors="replace"), debug)

    try:
        data: dict[str, object] = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise PreviewError(f"yt-dlp returned invalid JSON: {exc}", debug) from exc

    return data, debug


async def preview_youtube(url: str, max_items: int) -> ImportPreview:
    """Fetch metadata for a YouTube URL without downloading.

    For playlists, uses ``--flat-playlist`` to grab just the list of
    video URLs and titles without resolving each video's full metadata.
    This is much faster and more reliable than ``-J`` alone, which
    must fetch metadata for every entry and fails if any single video
    is unavailable.

    For single videos, ``-J`` provides title, duration, and thumbnail.

    At most *max_items* entries are fetched via ``--playlist-end`` so a large
    list does not dump hundreds of rows; the result reports ``total_available``
    and ``truncated`` so the caller can offer to fetch more or import a subset.
    Radio/mix links are normalized to a single video first (see
    ``normalize_youtube_url``).

    Args:
        url: YouTube video or playlist URL.
        max_items: Cap on the number of playlist entries returned.

    Returns:
        ``ImportPreview`` with is_playlist flag and track list.

    Raises:
        PreviewError: If yt-dlp fails or returns unexpected output.
    """
    url = normalize_youtube_url(url)

    # First try with --flat-playlist.  For a single video this still
    # returns a normal video object (not a playlist), so it works for
    # both cases.  For playlists it returns only lightweight entries
    # (title, url, id) without resolving each video — fast and robust.
    # --playlist-end caps how many entries we pull for a large list.
    data, debug = await yt_dlp_json(
        "--flat-playlist", "--playlist-end", str(max_items), url
    )

    if data.get("_type") == "playlist":
        playlist_title: str | None = (
            _json_str(data, "title") or _json_str(data, "uploader") or "Playlist"
        )
        tracks: list[TrackPreview] = []
        entries = data.get("entries")
        for entry in entries if isinstance(entries, list) else []:
            if not entry or not isinstance(entry, dict):
                continue
            raw_title = _json_str(entry, "title") or ""
            uploader = _json_str(entry, "uploader") or _json_str(entry, "channel") or ""
            artist, track_title = parse_yt_title(raw_title, uploader)
            # With --flat-playlist, webpage_url may be absent; build from id.
            entry_url = (
                _json_str(entry, "webpage_url")
                or _json_str(entry, "url")
                or f"https://www.youtube.com/watch?v={entry.get('id', '')}"
            )
            tracks.append(
                TrackPreview(
                    url=entry_url,
                    title=track_title,
                    artist=artist,
                    duration_seconds=_json_float(entry, "duration"),
                    thumbnail_url=_json_str(entry, "thumbnail"),
                )
            )
        # yt-dlp reports the full list length in ``playlist_count`` when known
        # (None for unbounded/unknown lists). If we filled the cap, assume more
        # may exist even when the count is unknown.
        raw_count = data.get("playlist_count")
        total_available = raw_count if isinstance(raw_count, int) else None
        if total_available is not None:
            truncated = total_available > len(tracks)
        else:
            truncated = len(tracks) >= max_items
        return ImportPreview(
            is_playlist=True,
            playlist_title=playlist_title,
            tracks=tracks,
            total_available=total_available,
            truncated=truncated,
            debug=debug,
        )

    raw_title = _json_str(data, "title") or ""
    uploader = _json_str(data, "uploader") or _json_str(data, "channel") or ""
    artist, track_title = parse_yt_title(raw_title, uploader)
    return ImportPreview(
        is_playlist=False,
        tracks=[
            TrackPreview(
                url=_json_str(data, "webpage_url") or url,
                title=track_title,
                artist=artist,
                duration_seconds=_json_float(data, "duration"),
                thumbnail_url=_json_str(data, "thumbnail"),
            )
        ],
        debug=debug,
    )
