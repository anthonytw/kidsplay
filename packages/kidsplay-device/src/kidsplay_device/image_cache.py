"""Bounded LRU image cache for pygame Surfaces.

This is the *only* module that calls ``pygame.image.load()``.  All views
request images by path through ``ImageCache.get()``; they never load files
themselves.

Cache policy:
  - On first access, load the file from disk and cache the Surface.
  - On subsequent accesses, return the cached Surface and promote it to
    most-recently-used.
  - When the cache exceeds ``max_size`` entries, evict the
    least-recently-used entry.
  - ``clear()`` discards all cached surfaces (used on view transitions
    that completely replace the visible content).

Thread safety: not thread-safe.  This cache is used exclusively on the
pygame main thread.
"""

import logging
from collections import OrderedDict
from pathlib import Path

import pygame

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SIZE = 64


class ImageCache:
    """LRU cache that loads ``pygame.Surface`` objects from disk on demand.

    Args:
        max_size: Maximum number of surfaces to keep in memory.  When
            exceeded the least-recently-used entry is evicted.
        media_root: Optional base directory.  Paths that are not absolute
            are resolved relative to this directory before loading.  This
            lets views pass the relative paths stored in the device DB
            (e.g. ``"thumbnails/ab/abc…webp"``) without knowing the root.
    """

    def __init__(
        self,
        max_size: int = _DEFAULT_MAX_SIZE,
        media_root: Path | None = None,
    ) -> None:
        self._max_size = max_size
        self._media_root = media_root
        # OrderedDict used as an ordered map: key=path, value=Surface.
        # Most-recently-used is at the *end*.
        self._cache: OrderedDict[str, pygame.Surface] = OrderedDict()

    def get(self, path: str) -> pygame.Surface | None:
        """Return the ``pygame.Surface`` for *path*, loading it if needed.

        On cache hit: promotes the entry to most-recently-used.
        On cache miss: loads from disk.  Relative paths are resolved
        against ``media_root`` if one was supplied.  Returns ``None`` if
        the file is missing or unreadable (failure is not cached).

        Args:
            path: Relative or absolute filesystem path to an image file.

        Returns:
            A ``pygame.Surface``, or ``None`` if the file cannot be loaded.
        """
        if path in self._cache:
            # Promote to MRU.
            self._cache.move_to_end(path)
            return self._cache[path]

        surface = self._load(path)
        if surface is None:
            return None

        self._cache[path] = surface
        self._cache.move_to_end(path)
        self._evict_if_needed()
        return surface

    def clear(self) -> None:
        """Discard all cached surfaces.

        Call this on view transitions where the new view will show
        completely different images, to keep memory usage low.
        """
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self, path: str) -> pygame.Surface | None:
        """Load a single image file and return a Surface, or None on error.

        Resolves relative paths against ``media_root`` when set.

        Args:
            path: Filesystem path to the image.

        Returns:
            Loaded ``pygame.Surface`` (converted for fast blitting), or
            ``None`` if the file is missing or ``pygame.image.load`` fails.
        """
        p = Path(path)
        if not p.is_absolute() and self._media_root is not None:
            p = self._media_root / p
        if not p.exists():
            logger.debug("ImageCache: file not found: %s", p)
            return None
        try:
            surface = pygame.image.load(str(p))
            return surface.convert_alpha()
        except Exception:
            logger.debug("ImageCache: failed to load %s", path, exc_info=True)
            return None

    def _evict_if_needed(self) -> None:
        """Remove LRU entries until the cache is within ``max_size``."""
        while len(self._cache) > self._max_size:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.debug("ImageCache: evicted %s", evicted_key)
