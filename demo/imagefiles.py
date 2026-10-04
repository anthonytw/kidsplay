"""Write images only when their pixels changed.

PNG and GIF bytes depend on the encoder and its settings, so regenerating a
screenshot on another machine rewrites every file even when nothing on screen
moved. These helpers compare *decoded pixels* with what is already on disk and
leave an identical file alone, so a rerun produces only real diffs.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from PIL import Image, ImageSequence

if TYPE_CHECKING:
    from pathlib import Path

Pixels = tuple[tuple[int, int], bytes]
GifFrames = list[tuple[Pixels, int]]


def _pixels(img: Image.Image) -> tuple[tuple[int, int], bytes]:
    rgb = img.convert("RGB")
    return rgb.size, rgb.tobytes()


def same_pixels(existing: Path, new: Image.Image) -> bool:
    """Whether a PNG on disk decodes to exactly the pixels of ``new``.

    Args:
        existing: Path of the file on disk (may not exist).
        new: The image about to be written.

    Returns:
        False if the file is missing or unreadable, or the pixels differ.
    """
    try:
        with Image.open(existing) as old:
            return _pixels(old) == _pixels(new)
    except (OSError, ValueError):
        return False


def save_png_if_changed(img: Image.Image, path: Path) -> bool:
    """Save ``img`` as an optimized PNG unless the file already shows it.

    Args:
        img: The image.
        path: Destination file.

    Returns:
        True if the file was written, False if it was left alone.
    """
    if same_pixels(path, img):
        return False
    img.save(path, optimize=True)
    return True


def save_png_bytes_if_changed(data: bytes, path: Path) -> bool:
    """Write already-encoded PNG bytes unless the file already shows them.

    Args:
        data: PNG file contents, e.g. from a browser screenshot.
        path: Destination file.

    Returns:
        True if the file was written, False if it was left alone.
    """
    with Image.open(io.BytesIO(data)) as new:
        if same_pixels(path, new):
            return False
    path.write_bytes(data)
    return True


def _gif_frames(source: Path | io.BytesIO) -> GifFrames:
    """Decode a GIF to (pixels, duration) per frame."""
    with Image.open(source) as gif:
        return [
            (_pixels(frame), int(frame.info.get("duration", 0)))
            for frame in ImageSequence.Iterator(gif)
        ]


def save_gif_if_changed(
    images: list[Image.Image], durations: list[int], path: Path
) -> bool:
    """Save an animated GIF unless the file already has the same frames.

    The new GIF is encoded in memory first and compared *after* GIF's palette
    quantization, so the comparison is between what would land on disk and
    what is there.

    Args:
        images: The frames.
        durations: Per-frame hold time in milliseconds.
        path: Destination file.

    Returns:
        True if the file was written, False if it was left alone.
    """
    buf = io.BytesIO()
    images[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    try:
        if _gif_frames(path) == _gif_frames(io.BytesIO(buf.getvalue())):
            return False
    except (OSError, ValueError):
        pass
    path.write_bytes(buf.getvalue())
    return True
