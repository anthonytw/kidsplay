"""Tests for writing images only when their pixels changed."""

import io
import os
from pathlib import Path

from PIL import Image

from demo import imagefiles


def _img(color: tuple[int, int, int], size: tuple[int, int] = (6, 4)) -> Image.Image:
    return Image.new("RGB", size, color)


def _age(path: Path) -> int:
    """Make the file old, so a later rewrite shows up in its mtime."""
    os.utime(path, (1_000_000, 1_000_000))
    return path.stat().st_mtime_ns


def test_png_written_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    assert imagefiles.save_png_if_changed(_img((1, 2, 3)), path) is True
    assert Image.open(path).getpixel((0, 0)) == (1, 2, 3)


def test_png_left_alone_when_pixels_match_even_if_bytes_differ(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.png"
    # Same pixels, different encoder settings: different bytes on disk.
    _img((9, 9, 9)).save(path, compress_level=0)
    before = path.read_bytes()
    stamp = _age(path)

    assert imagefiles.save_png_if_changed(_img((9, 9, 9)), path) is False
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == stamp


def test_png_rewritten_when_a_pixel_changes(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    _img((9, 9, 9)).save(path)
    changed = _img((9, 9, 9))
    changed.putpixel((5, 3), (0, 0, 0))

    assert imagefiles.save_png_if_changed(changed, path) is True
    assert Image.open(path).getpixel((5, 3)) == (0, 0, 0)


def test_png_rewritten_when_the_size_changes(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    _img((9, 9, 9), (6, 4)).save(path)
    assert imagefiles.save_png_if_changed(_img((9, 9, 9), (6, 5)), path) is True


def test_png_rewritten_over_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(b"not a png")
    assert imagefiles.save_png_if_changed(_img((1, 1, 1)), path) is True
    assert Image.open(path).size == (6, 4)


def test_png_bytes_skip_identical_pixels_and_keep_new_bytes_otherwise(
    tmp_path: Path,
) -> None:
    def encode(img: Image.Image, **kw: int) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG", **kw)
        return buf.getvalue()

    path = tmp_path / "shot.png"
    first = encode(_img((5, 6, 7)), compress_level=0)
    assert imagefiles.save_png_bytes_if_changed(first, path) is True
    stamp = _age(path)

    other_encoding = encode(_img((5, 6, 7)), compress_level=9)
    assert other_encoding != first
    assert imagefiles.save_png_bytes_if_changed(other_encoding, path) is False
    assert path.read_bytes() == first
    assert path.stat().st_mtime_ns == stamp

    new = encode(_img((7, 6, 5)))
    assert imagefiles.save_png_bytes_if_changed(new, path) is True
    assert path.read_bytes() == new


def test_gif_skips_the_same_frames_and_durations(tmp_path: Path) -> None:
    path = tmp_path / "w.gif"
    frames = [_img((255, 0, 0)), _img((0, 0, 255))]
    assert imagefiles.save_gif_if_changed(frames, [300, 500], path) is True
    stamp = _age(path)

    assert imagefiles.save_gif_if_changed(frames, [300, 500], path) is False
    assert path.stat().st_mtime_ns == stamp


def test_gif_rewritten_when_a_frame_or_a_duration_changes(tmp_path: Path) -> None:
    path = tmp_path / "w.gif"
    frames = [_img((255, 0, 0)), _img((0, 0, 255))]
    imagefiles.save_gif_if_changed(frames, [300, 500], path)

    assert imagefiles.save_gif_if_changed(frames, [300, 600], path) is True
    assert (
        imagefiles.save_gif_if_changed([frames[0], _img((0, 255, 0))], [300, 600], path)
        is True
    )
    assert imagefiles.save_gif_if_changed(frames[:1] * 3, [1, 1, 1], path) is True
