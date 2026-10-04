"""Tests for ImageCache: load, cache, LRU eviction, missing files.

Requires pygame.display.init() so that pygame.image.load() and
convert_alpha() work correctly.
"""

import io
from collections.abc import Iterator
from pathlib import Path

import pygame
import pytest
from PIL import Image

from kidsplay_device.image_cache import ImageCache

# ---------------------------------------------------------------------------
# Fixture: pygame display for the test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def pygame_display() -> Iterator[None]:
    """Initialise a headless pygame display once for the whole module."""
    pygame.display.init()
    pygame.display.set_mode((64, 64), flags=pygame.NOFRAME)
    yield
    pygame.display.quit()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def write_png(path: Path, width: int = 32, height: int = 32, seed: int = 0) -> Path:
    """Write a small RGB PNG to *path* and return *path*."""
    color = (100 + seed * 40 % 155, 80, 200 - seed * 30 % 180)
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


# ---------------------------------------------------------------------------
# Basic load and cache
# ---------------------------------------------------------------------------


class TestLoadAndCache:
    def test_load_existing_file_returns_surface(self, tmp_path: Path) -> None:
        p = write_png(tmp_path / "img.png")
        cache = ImageCache()
        result = cache.get(str(p))
        assert result is not None
        assert isinstance(result, pygame.Surface)

    def test_second_call_returns_same_object(self, tmp_path: Path) -> None:
        p = write_png(tmp_path / "img.png")
        cache = ImageCache()
        s1 = cache.get(str(p))
        s2 = cache.get(str(p))
        assert s1 is s2

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        cache = ImageCache()
        result = cache.get(str(tmp_path / "nonexistent.png"))
        assert result is None

    def test_missing_file_not_cached(self, tmp_path: Path) -> None:
        """After a miss, writing the file and retrying should succeed."""
        cache = ImageCache()
        p = tmp_path / "late.png"
        assert cache.get(str(p)) is None
        write_png(p)
        result = cache.get(str(p))
        assert result is not None

    def test_different_paths_are_cached_independently(self, tmp_path: Path) -> None:
        p1 = write_png(tmp_path / "a.png", seed=1)
        p2 = write_png(tmp_path / "b.png", seed=2)
        cache = ImageCache()
        s1 = cache.get(str(p1))
        s2 = cache.get(str(p2))
        assert s1 is not s2


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


class TestLRUEviction:
    def test_cache_size_stays_at_max(self, tmp_path: Path) -> None:
        max_size = 4
        cache = ImageCache(max_size=max_size)
        paths = [
            write_png(tmp_path / f"img{i}.png", seed=i) for i in range(max_size + 2)
        ]
        for p in paths:
            cache.get(str(p))
        assert len(cache._cache) == max_size

    def test_lru_entry_evicted_first(self, tmp_path: Path) -> None:
        """The least-recently-used entry should be evicted when cache is full."""
        cache = ImageCache(max_size=3)
        paths = [write_png(tmp_path / f"img{i}.png", seed=i) for i in range(3)]
        str_paths = [str(p) for p in paths]

        # Fill cache: paths[0], paths[1], paths[2]
        for sp in str_paths:
            cache.get(sp)

        # Access paths[0] to make it MRU, then paths[1]
        cache.get(str_paths[0])
        cache.get(str_paths[1])

        # Add a 4th entry — paths[2] is now LRU, should be evicted.
        new_path = write_png(tmp_path / "new.png", seed=9)
        cache.get(str(new_path))

        assert str_paths[2] not in cache._cache
        assert str_paths[0] in cache._cache
        assert str_paths[1] in cache._cache

    def test_eviction_does_not_happen_below_max(self, tmp_path: Path) -> None:
        cache = ImageCache(max_size=10)
        paths = [write_png(tmp_path / f"img{i}.png", seed=i) for i in range(5)]
        for p in paths:
            cache.get(str(p))
        assert len(cache._cache) == 5


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_empties_cache(self, tmp_path: Path) -> None:
        p = write_png(tmp_path / "img.png")
        cache = ImageCache()
        cache.get(str(p))
        assert len(cache._cache) == 1
        cache.clear()
        assert len(cache._cache) == 0

    def test_after_clear_file_loads_again(self, tmp_path: Path) -> None:
        p = write_png(tmp_path / "img.png")
        cache = ImageCache()
        s1 = cache.get(str(p))
        cache.clear()
        s2 = cache.get(str(p))
        # Both are valid surfaces (not the same Python object after eviction).
        assert s2 is not None
        assert isinstance(s2, pygame.Surface)
        assert s2 is not s1
