"""All UI views for the KidsPlay device player.

Five views, each with three methods:
  handle_input(button) — mutate selection/scroll state, call app methods
  update()             — per-frame logic (currently a no-op for most views)
  draw(surface)        — render to the given pygame.Surface

Shared utilities
----------------
clamp_scroll(selected, visible_count, total_count) -> int
    Single function used by all list views to compute scroll offset.

truncate_text(font, text, max_width) -> str
    Adds "…" until the rendered text fits within max_width pixels.

Views never store data lists themselves across frames.  They re-query the
DB on ``on_enter()`` and keep that snapshot for the life of the view visit.

Layout
------
Sizes were designed for 640×480. ``apply_layout(Layout)`` sets the screen-size
globals below for the real screen and ``px(n)`` scales a design length, so the
views draw on any resolution (see ``layout.py``). At 640×480 ``px(n) == n``.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

import pygame

from .buttons import Button
from .database import (
    MediaRow,
    get_chapters_by_book,
    get_groups_with_thumbnails,
    get_photos_by_group,
    get_tracks_by_group,
)
from .i18n import N_, _, ngettext
from .layout import DEFAULT_LAYOUT, Layout
from .theme import THEMES

if TYPE_CHECKING:
    from datetime import datetime

    from .app import MusicPlayerApp
    from .image_cache import ImageCache
    from .theme import Theme

# ---------------------------------------------------------------------------
# Screen / layout constants
# ---------------------------------------------------------------------------

# Set by apply_layout(); these are the values for 640×480 until then.
SCREEN_W = DEFAULT_LAYOUT.width
SCREEN_H = DEFAULT_LAYOUT.height

# Playback bar at the bottom
BAR_H = DEFAULT_LAYOUT.bar_h
# Area available to views (above the playback bar)
VIEW_H = DEFAULT_LAYOUT.view_h

# Header bar
HEADER_H = DEFAULT_LAYOUT.header_h

# List rows: exactly three fit in (VIEW_H - HEADER_H)
LIST_ITEM_H = DEFAULT_LAYOUT.list_item_h
THUMB_SMALL = DEFAULT_LAYOUT.thumb_small  # fits in the playback bar

# Photo grid
GRID_COLS = DEFAULT_LAYOUT.grid_cols
GRID_CELL_H = DEFAULT_LAYOUT.grid_cell_h  # shows 3 rows

_layout: Layout = DEFAULT_LAYOUT


def apply_layout(layout: Layout) -> None:
    """Set every screen-size global to match *layout*.

    Call once the display size is known, before the first draw.

    Args:
        layout: The ``Layout`` for the screen the player opened.
    """
    global SCREEN_W, SCREEN_H, BAR_H, VIEW_H, HEADER_H, LIST_ITEM_H
    global THUMB_SMALL, GRID_CELL_H, GRID_COLS, _layout
    _layout = layout
    SCREEN_W, SCREEN_H = layout.width, layout.height
    BAR_H, VIEW_H, HEADER_H = layout.bar_h, layout.view_h, layout.header_h
    LIST_ITEM_H, THUMB_SMALL = layout.list_item_h, layout.thumb_small
    GRID_CELL_H = layout.grid_cell_h
    GRID_COLS = layout.grid_cols


def px(value: int | float) -> int:
    """Scale a length from the 640×480 design to the current screen.

    Args:
        value: Length in design pixels.

    Returns:
        The length in screen pixels (at least 1); *value* itself at 640×480.
    """
    return _layout.px(value)


# Colour palette
BG = (18, 18, 24)
SURFACE = (32, 34, 42)
SURFACE_SEL = (52, 58, 78)
PRIMARY = (106, 153, 229)
TEXT = (220, 222, 230)
TEXT_DIM = (120, 124, 140)
TEXT_BRIGHT = (255, 255, 255)
ACCENT = (255, 180, 60)
BAR_BG = (24, 26, 34)
PROGRESS_BG = (48, 52, 68)
PROGRESS_FG = PRIMARY


def apply_theme(theme: Theme) -> None:
    """Update all module-level color globals to match *theme*.

    Call this whenever the user switches themes; all subsequent draw calls
    in this module will use the new palette immediately.

    Args:
        theme: The ``Theme`` to apply.
    """
    global BG, SURFACE, SURFACE_SEL, PRIMARY, TEXT, TEXT_DIM
    global TEXT_BRIGHT, ACCENT, PROGRESS_BG, PROGRESS_FG
    BG = theme.BG
    SURFACE = theme.SURFACE
    SURFACE_SEL = theme.SURFACE_SEL
    PRIMARY = theme.PRIMARY
    TEXT = theme.TEXT
    TEXT_DIM = theme.TEXT_DIM
    TEXT_BRIGHT = theme.TEXT_BRIGHT
    ACCENT = theme.ACCENT
    PROGRESS_BG = theme.PROGRESS_BG
    PROGRESS_FG = theme.PROGRESS_FG


_BACKGROUND: pygame.Surface | None = None
_HOME_BACKGROUND: pygame.Surface | None = None


def set_backgrounds(
    background: pygame.Surface | None, home_background: pygame.Surface | None
) -> None:
    """Set the theme's background images (already scaled to the screen).

    Args:
        background: Drawn behind every screen except the home screen, the
            photo viewer and the sleep screen; None for the plain color.
        home_background: Drawn behind the home screen; None uses *background*.
    """
    global _BACKGROUND, _HOME_BACKGROUND
    _BACKGROUND, _HOME_BACKGROUND = background, home_background


def _fill_background(
    surface: pygame.Surface, *, home: bool = False, plain: bool = False
) -> None:
    """Clear *surface* with the theme's background color and image."""
    surface.fill(BG)
    if plain:
        return
    image = (_HOME_BACKGROUND or _BACKGROUND) if home else _BACKGROUND
    if image is not None:
        surface.blit(image, (0, 0))


# ---------------------------------------------------------------------------
# Button enum
# ---------------------------------------------------------------------------


class UiSound(Enum):
    """UI sound effect to play after a button action.

    Returned by ``handle_input`` so the app can play the right clip without
    views needing direct access to the mixer.
    """

    MOVE = auto()  # Cursor moved in a list / grid
    SELECT = auto()  # Entered a sub-view or toggled a setting
    BACK = auto()  # Returned to a previous screen
    OPEN = auto()  # Opened a media item (track, photo, audiobook chapter)


class View(Protocol):
    """Interface every top-level view implements; ``app.py`` dispatches to it."""

    def on_enter(self) -> None: ...

    def handle_input(self, button: Button) -> UiSound | None: ...

    def update(self) -> None: ...

    def draw(self, surface: pygame.Surface) -> None: ...


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def clamp_scroll(selected: int, visible_count: int, total_count: int) -> int:
    """Compute a scroll offset so *selected* is always visible.

    Args:
        selected: Currently selected index (0-based).
        visible_count: Number of items that fit on screen at once.
        total_count: Total number of items in the list.

    Returns:
        Scroll offset (first visible item index).
    """
    if total_count <= visible_count:
        return 0
    max_offset = total_count - visible_count
    # Keep selected item at most at position (visible_count - 1) from top.
    offset = max(0, selected - visible_count + 1)
    return min(offset, max_offset)


def truncate_text(font: pygame.font.Font, text: str, max_width: int) -> str:
    """Shorten *text* with a trailing ellipsis until it fits in *max_width*.

    Args:
        font: Font used to measure rendered width.
        text: Original text string.
        max_width: Maximum pixel width.

    Returns:
        The original string if it fits, otherwise a truncated version ending
        with ``"…"``.
    """
    if font.size(text)[0] <= max_width:
        return text
    while len(text) > 1 and font.size(text + "…")[0] > max_width:
        text = text[:-1]
    return text + "…"


def scale_fit(img: pygame.Surface, max_w: int, max_h: int) -> pygame.Surface:
    """Scale *img* to fit within *max_w* × *max_h*, preserving aspect ratio.

    Args:
        img: Source surface.
        max_w: Maximum output width in pixels.
        max_h: Maximum output height in pixels.

    Returns:
        A new surface scaled down (or up) to fit, never exceeding the given
        bounds on either axis.
    """
    iw, ih = img.get_size()
    scale = min(max_w / iw, max_h / ih)
    new_w = max(1, int(iw * scale))
    new_h = max(1, int(ih * scale))
    return pygame.transform.smoothscale(img, (new_w, new_h))


def scale_cover(img: pygame.Surface, width: int, height: int) -> pygame.Surface:
    """Scale *img* to exactly *width* × *height*, cropping what overflows.

    Args:
        img: Source surface.
        width: Output width in pixels.
        height: Output height in pixels.

    Returns:
        A new surface that covers the target with no bars, centred.
    """
    iw, ih = img.get_size()
    scale = max(width / iw, height / ih)
    new_w = max(width, round(iw * scale))
    new_h = max(height, round(ih * scale))
    scaled = pygame.transform.smoothscale(img, (new_w, new_h))
    crop = pygame.Rect((new_w - width) // 2, (new_h - height) // 2, width, height)
    return scaled.subsurface(crop).copy()


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def _draw_header(surface: pygame.Surface, font: pygame.font.Font, title: str) -> None:
    """Draw a title header bar at the top of the surface."""
    pygame.draw.rect(surface, SURFACE, (0, 0, SCREEN_W, HEADER_H))
    label = truncate_text(font, title, SCREEN_W - px(16))
    surf = font.render(label, True, TEXT_BRIGHT)
    surface.blit(surf, (px(8), (HEADER_H - surf.get_height()) // 2))


def _draw_list_item(
    surface: pygame.Surface,
    x: int,
    y: int,
    title: str,
    subtitle: str | None,
    is_selected: bool,
    font_title: pygame.font.Font,
    font_sub: pygame.font.Font,
    cache: ImageCache,
    thumb_path: str | None,
) -> None:
    """Draw a single list row with optional thumbnail, title, and subtitle.

    Args:
        surface: Target surface.
        x: Left edge of the row.
        y: Top edge of the row.
        title: Primary label text.
        subtitle: Secondary label text, or ``None`` to vertically center title.
        is_selected: Whether to highlight this row.
        font_title: Font for the title.
        font_sub: Font for the subtitle.
        cache: ``ImageCache`` instance (``app.cache``).
        thumb_path: Relative path to the thumbnail, or ``None``.
    """
    row_rect = pygame.Rect(x, y, SCREEN_W - x * 2, LIST_ITEM_H)
    pygame.draw.rect(surface, SURFACE_SEL if is_selected else SURFACE, row_rect)

    cx = x + px(4)
    # Thumbnail — constrained by item height, natural aspect ratio
    thumb_h = LIST_ITEM_H - px(8)
    if thumb_path:
        img = cache.get(thumb_path)
        if img:
            scaled = scale_fit(img, SCREEN_W // 2, thumb_h)
            sw, sh = scaled.get_size()
            by = y + (LIST_ITEM_H - sh) // 2
            surface.blit(scaled, (cx, by))
            cx += sw + px(8)
    else:
        cx += thumb_h + px(8)

    max_title_w = SCREEN_W - cx - x - px(4)
    trunc = truncate_text(font_title, title, max_title_w)
    title_surf = font_title.render(trunc, True, TEXT_BRIGHT if is_selected else TEXT)
    if subtitle:
        title_y = y + (LIST_ITEM_H // 2) - title_surf.get_height() - 1
        surface.blit(title_surf, (cx, title_y))
        sub_text = truncate_text(font_sub, subtitle, max_title_w)
        sub_surf = font_sub.render(sub_text, True, TEXT_DIM)
        surface.blit(sub_surf, (cx, y + LIST_ITEM_H // 2 + 1))
    else:
        title_y = y + (LIST_ITEM_H - title_surf.get_height()) // 2
        surface.blit(title_surf, (cx, title_y))


#: Display names of the built-in themes. ``Theme.name`` (Spanish for the six
#: original colors) is the persisted key in ``settings.json``, so it stays; only
#: what the screen shows is translated. A custom theme shows its own name.
THEME_LABELS: dict[str, str] = {
    "Azul": N_("Blue"),
    "Morado": N_("Purple"),
    "Verde": N_("Green"),
    "Rojo": N_("Red"),
    "Naranja": N_("Orange"),
    "Cian": N_("Cyan"),
    "High contrast": N_("Contrast"),
    "Night": N_("Night"),
}

# ---------------------------------------------------------------------------
# HomeView
# ---------------------------------------------------------------------------


class HomeView:
    """2×2 grid home screen: Music, Audiobooks, Photos, Settings."""

    # (label, view_name, fa_solid_codepoint)
    _ITEMS = [
        (N_("Music"), "music", "\uf001"),  # fa-music
        (N_("Audiobooks"), "audiobooks", "\uf518"),  # fa-book-open
        (N_("Photos"), "photos", "\uf03e"),  # fa-image
        (N_("Settings"), "settings", "\uf013"),  # fa-gear
    ]

    def __init__(self, app: MusicPlayerApp) -> None:
        self._app = app
        self.selected_index: int = 0

    def on_enter(self) -> None:
        pass

    def handle_input(self, button: Button) -> UiSound | None:
        """Navigate the 2×2 grid; A selects."""
        count = len(self._ITEMS)
        cols = 2
        row = self.selected_index // cols
        col = self.selected_index % cols
        max_row = (count - 1) // cols

        if button == Button.RIGHT:
            if col < cols - 1 and self.selected_index + 1 < count:
                self.selected_index += 1
            return UiSound.MOVE
        elif button == Button.LEFT:
            if col > 0:
                self.selected_index -= 1
            return UiSound.MOVE
        elif button == Button.DOWN:
            if row < max_row and self.selected_index + cols < count:
                self.selected_index += cols
            return UiSound.MOVE
        elif button == Button.UP:
            if row > 0:
                self.selected_index -= cols
            return UiSound.MOVE
        elif button == Button.SELECT:
            view_name = self._ITEMS[self.selected_index][1]
            self._app.switch_view(view_name)
            return UiSound.SELECT
        return None

    def update(self) -> None:
        pass

    def draw(self, surface: pygame.Surface) -> None:
        """Render the 2×2 grid of home menu tiles."""
        _fill_background(surface, home=True)
        font = self._app.fonts["medium"]
        cols, rows = 2, 2
        pad = px(16)
        warn_h = (
            self._draw_identity_warning(surface) if self._app.identity_warning else 0
        )
        cell_w = (SCREEN_W - pad * (cols + 1)) // cols
        cell_h = (VIEW_H - warn_h - pad * (rows + 1)) // rows

        icon_font = self._app.fonts.get("icon")
        for i, (label, view_name, icon) in enumerate(self._ITEMS):
            row, col = divmod(i, cols)
            x = pad + col * (cell_w + pad)
            y = pad + row * (cell_h + pad)
            is_sel = i == self.selected_index
            color = TEXT_BRIGHT if is_sel else PRIMARY
            bg = SURFACE_SEL if is_sel else SURFACE
            locked = not self._app.view_allowed(view_name)
            if locked:
                # Locked during bedtime: a dim tile with a moon.
                color = TEXT_DIM
                bg = BG
                icon = "\uf186"  # fa-moon
            pygame.draw.rect(surface, bg, (x, y, cell_w, cell_h), border_radius=px(10))
            if is_sel:
                pygame.draw.rect(
                    surface,
                    PRIMARY,
                    (x, y, cell_w, cell_h),
                    width=px(2),
                    border_radius=px(10),
                )
            lbl_color = TEXT_BRIGHT if is_sel and not locked else TEXT_DIM
            text = truncate_text(font, _(label), cell_w - px(8))
            lbl = font.render(text, True, lbl_color)
            if icon_font:
                icon_surf = icon_font.render(icon, True, color)
                total_h = icon_surf.get_height() + px(8) + lbl.get_height()
                iy = y + (cell_h - total_h) // 2
                cx = x + (cell_w - icon_surf.get_width()) // 2
                surface.blit(icon_surf, (cx, iy))
                lx = x + (cell_w - lbl.get_width()) // 2
                surface.blit(lbl, (lx, iy + icon_surf.get_height() + px(8)))
            else:
                lx = x + (cell_w - lbl.get_width()) // 2
                ly = y + (cell_h - lbl.get_height()) // 2
                surface.blit(lbl, (lx, ly))

    def _draw_identity_warning(self, surface: pygame.Surface) -> int:
        """Draw the "unknown server" strip at the bottom of the view area.

        Returns:
            Its height, which the tiles leave free.
        """
        font = self._app.fonts["small"]
        text = truncate_text(
            font, _("Unknown server. Ask a grown-up."), SCREEN_W - px(24)
        )
        image = font.render(text, True, BG)
        strip_h = image.get_height() + px(12)
        rect = pygame.Rect(px(8), VIEW_H - strip_h - px(4), SCREEN_W - px(16), strip_h)
        pygame.draw.rect(surface, ACCENT, rect, border_radius=px(6))
        surface.blit(
            image, (rect.x + (rect.width - image.get_width()) // 2, rect.y + px(6))
        )
        return strip_h + px(4)


# ---------------------------------------------------------------------------
# MusicView
# ---------------------------------------------------------------------------


class MusicView:
    """Two-level music browser: playlist groups → tracks within a group."""

    def __init__(self, app: MusicPlayerApp) -> None:
        self._app = app
        # Level 0: (playlist_title, representative_thumbnail) pairs
        self._groups: list[tuple[str, str | None]] = []
        self.selected_index: int = 0
        self.scroll_offset: int = 0
        # Level 1: tracks within the selected group
        self._tracks: list[MediaRow] = []
        self._track_sel: int = 0
        self._track_scroll: int = 0
        self._in_group: bool = False  # False = group list, True = track list

    def on_enter(self) -> None:
        """Reload group list; return to group level."""
        import sqlite3

        conn = sqlite3.connect(self._app.db_path)
        try:
            self._groups = get_groups_with_thumbnails(conn, "music")
        finally:
            conn.close()
        self._in_group = False
        visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
        self.scroll_offset = clamp_scroll(
            self.selected_index, visible, len(self._groups)
        )

    def handle_input(self, button: Button) -> UiSound | None:
        if not self._in_group:
            return self._handle_groups(button)
        else:
            return self._handle_tracks(button)

    def _handle_groups(self, button: Button) -> UiSound | None:
        count = len(self._groups)
        visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
        if button == Button.UP:
            if self.selected_index > 0:
                self.selected_index -= 1
                self.scroll_offset = clamp_scroll(self.selected_index, visible, count)
            return UiSound.MOVE
        elif button == Button.DOWN:
            if self.selected_index < count - 1:
                self.selected_index += 1
                self.scroll_offset = clamp_scroll(self.selected_index, visible, count)
            return UiSound.MOVE
        elif button == Button.SELECT and count > 0:
            import sqlite3

            group = self._groups[self.selected_index][0]
            conn = sqlite3.connect(self._app.db_path)
            try:
                self._tracks = get_tracks_by_group(conn, group)
            finally:
                conn.close()
            self._track_sel = 0
            self._track_scroll = 0
            self._in_group = True
            return UiSound.SELECT
        elif button == Button.CANCEL:
            self._app.switch_view("home")
            return UiSound.BACK
        return None

    def _handle_tracks(self, button: Button) -> UiSound | None:
        count = len(self._tracks)
        visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
        if button == Button.UP:
            if self._track_sel > 0:
                self._track_sel -= 1
                self._track_scroll = clamp_scroll(self._track_sel, visible, count)
            return UiSound.MOVE
        elif button == Button.DOWN:
            if self._track_sel < count - 1:
                self._track_sel += 1
                self._track_scroll = clamp_scroll(self._track_sel, visible, count)
            return UiSound.MOVE
        elif button == Button.SELECT and count > 0:
            track = self._tracks[self._track_sel]
            self._app.play_track(track, self._tracks)
            return UiSound.OPEN
        elif button == Button.CANCEL:
            self._in_group = False
            return UiSound.BACK
        return None

    def update(self) -> None:
        pass

    def draw(self, surface: pygame.Surface) -> None:
        _fill_background(surface)
        if not self._in_group:
            _draw_header(surface, self._app.fonts["medium"], _("Music"))
            _draw_group_list(
                surface,
                self._groups,
                self.selected_index,
                self.scroll_offset,
                self._app.fonts["folder"],
                self._app.cache,
            )
        else:
            group = self._groups[self.selected_index][0] if self._groups else _("Music")
            _draw_header(surface, self._app.fonts["medium"], group)
            _draw_track_list(
                surface,
                self._tracks,
                self._track_sel,
                self._track_scroll,
                self._app.fonts["medium"],
                self._app.fonts["small"],
                self._app.cache,
            )


# ---------------------------------------------------------------------------
# AudiobooksView
# ---------------------------------------------------------------------------


class AudiobooksView:
    """Two-level audiobook browser: books → chapters."""

    def __init__(self, app: MusicPlayerApp) -> None:
        self._app = app
        self._groups: list[tuple[str, str | None]] = []
        self.selected_index: int = 0
        self.scroll_offset: int = 0
        self._chapters: list[MediaRow] = []
        self._chapter_sel: int = 0
        self._chapter_scroll: int = 0
        self._in_book: bool = False

    def on_enter(self) -> None:
        """Reload book list from DB; return to book level."""
        import sqlite3

        conn = sqlite3.connect(self._app.db_path)
        try:
            self._groups = get_groups_with_thumbnails(conn, "audiobook")
        finally:
            conn.close()
        self._in_book = False
        visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
        self.scroll_offset = clamp_scroll(
            self.selected_index, visible, len(self._groups)
        )

    def handle_input(self, button: Button) -> UiSound | None:
        if not self._in_book:
            return self._handle_groups(button)
        else:
            return self._handle_chapters(button)

    def _handle_groups(self, button: Button) -> UiSound | None:
        count = len(self._groups)
        visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
        if button == Button.UP:
            if self.selected_index > 0:
                self.selected_index -= 1
                self.scroll_offset = clamp_scroll(self.selected_index, visible, count)
            return UiSound.MOVE
        elif button == Button.DOWN:
            if self.selected_index < count - 1:
                self.selected_index += 1
                self.scroll_offset = clamp_scroll(self.selected_index, visible, count)
            return UiSound.MOVE
        elif button == Button.SELECT and count > 0:
            import sqlite3

            book = self._groups[self.selected_index][0]
            conn = sqlite3.connect(self._app.db_path)
            try:
                self._chapters = get_chapters_by_book(conn, book)
            finally:
                conn.close()
            self._chapter_sel = 0
            self._chapter_scroll = 0
            self._in_book = True
            return UiSound.SELECT
        elif button == Button.CANCEL:
            self._app.switch_view("home")
            return UiSound.BACK
        return None

    def _handle_chapters(self, button: Button) -> UiSound | None:
        count = len(self._chapters)
        visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
        if button == Button.UP:
            if self._chapter_sel > 0:
                self._chapter_sel -= 1
                self._chapter_scroll = clamp_scroll(self._chapter_sel, visible, count)
            return UiSound.MOVE
        elif button == Button.DOWN:
            if self._chapter_sel < count - 1:
                self._chapter_sel += 1
                self._chapter_scroll = clamp_scroll(self._chapter_sel, visible, count)
            return UiSound.MOVE
        elif button == Button.SELECT and count > 0:
            chapter = self._chapters[self._chapter_sel]
            self._app.play_track(chapter, self._chapters)
            return UiSound.OPEN
        elif button == Button.CANCEL:
            self._in_book = False
            return UiSound.BACK
        return None

    def update(self) -> None:
        pass

    def draw(self, surface: pygame.Surface) -> None:
        _fill_background(surface)
        if not self._in_book:
            _draw_header(surface, self._app.fonts["medium"], _("Audiobooks"))
            _draw_group_list(
                surface,
                self._groups,
                self.selected_index,
                self.scroll_offset,
                self._app.fonts["folder"],
                self._app.cache,
            )
        else:
            book = (
                self._groups[self.selected_index][0]
                if self._groups
                else _("Audiobooks")
            )
            _draw_header(surface, self._app.fonts["medium"], book)
            _draw_track_list(
                surface,
                self._chapters,
                self._chapter_sel,
                self._chapter_scroll,
                self._app.fonts["medium"],
                self._app.fonts["small"],
                self._app.cache,
            )


# ---------------------------------------------------------------------------
# PhotosView
# ---------------------------------------------------------------------------


class PhotosView:
    """Two-level photo browser: albums → thumbnail grid → fullscreen."""

    def __init__(self, app: MusicPlayerApp) -> None:
        self._app = app
        self._groups: list[tuple[str, str | None]] = []
        self.selected_index: int = 0
        self.scroll_offset: int = 0
        self._photos: list[MediaRow] = []
        self._photo_sel: int = 0
        self._photo_scroll: int = 0  # in rows, not items
        self._in_album: bool = False
        self._fullscreen: bool = False

    def on_enter(self) -> None:
        """Reload album list from DB; return to album level."""
        import sqlite3

        conn = sqlite3.connect(self._app.db_path)
        try:
            self._groups = get_groups_with_thumbnails(conn, "photo")
        finally:
            conn.close()
        self._in_album = False
        self._fullscreen = False
        visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
        self.scroll_offset = clamp_scroll(
            self.selected_index, visible, len(self._groups)
        )

    def handle_input(self, button: Button) -> UiSound | None:
        if self._fullscreen:
            return self._handle_fullscreen(button)
        elif self._in_album:
            return self._handle_grid(button)
        else:
            return self._handle_groups(button)

    def _handle_groups(self, button: Button) -> UiSound | None:
        count = len(self._groups)
        visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
        if button == Button.UP:
            if self.selected_index > 0:
                self.selected_index -= 1
                self.scroll_offset = clamp_scroll(self.selected_index, visible, count)
            return UiSound.MOVE
        elif button == Button.DOWN:
            if self.selected_index < count - 1:
                self.selected_index += 1
                self.scroll_offset = clamp_scroll(self.selected_index, visible, count)
            return UiSound.MOVE
        elif button == Button.SELECT and count > 0:
            import sqlite3

            album = self._groups[self.selected_index][0]
            conn = sqlite3.connect(self._app.db_path)
            try:
                self._photos = get_photos_by_group(conn, album)
            finally:
                conn.close()
            self._photo_sel = 0
            self._photo_scroll = 0
            self._in_album = True
            return UiSound.SELECT
        elif button == Button.CANCEL:
            self._app.switch_view("home")
            return UiSound.BACK
        return None

    def _handle_grid(self, button: Button) -> UiSound | None:
        count = len(self._photos)
        if count == 0:
            if button == Button.CANCEL:
                self._in_album = False
                return UiSound.BACK
            return None

        col = self._photo_sel % GRID_COLS
        sound: UiSound | None = None
        if button == Button.RIGHT:
            if col < GRID_COLS - 1 and self._photo_sel + 1 < count:
                self._photo_sel += 1
            sound = UiSound.MOVE
        elif button == Button.LEFT:
            if col > 0:
                self._photo_sel -= 1
            sound = UiSound.MOVE
        elif button == Button.DOWN:
            if self._photo_sel + GRID_COLS < count:
                self._photo_sel += GRID_COLS
            sound = UiSound.MOVE
        elif button == Button.UP:
            if self._photo_sel >= GRID_COLS:
                self._photo_sel -= GRID_COLS
            sound = UiSound.MOVE
        elif button == Button.SELECT:
            self._fullscreen = True
            return UiSound.OPEN
        elif button == Button.CANCEL:
            self._in_album = False
            return UiSound.BACK

        # Update row-based scroll offset after any movement.
        visible_rows = _visible_items(VIEW_H - HEADER_H, GRID_CELL_H)
        total_rows = (count + GRID_COLS - 1) // GRID_COLS
        sel_row = self._photo_sel // GRID_COLS
        self._photo_scroll = clamp_scroll(sel_row, visible_rows, total_rows)
        return sound

    def _handle_fullscreen(self, button: Button) -> UiSound | None:
        count = len(self._photos)
        if button == Button.RIGHT:
            if self._photo_sel < count - 1:
                self._photo_sel += 1
            return UiSound.MOVE
        elif button == Button.LEFT:
            if self._photo_sel > 0:
                self._photo_sel -= 1
            return UiSound.MOVE
        elif button == Button.CANCEL:
            self._fullscreen = False
            return UiSound.BACK
        return None

    def update(self) -> None:
        pass

    def draw(self, surface: pygame.Surface) -> None:
        _fill_background(surface, plain=self._fullscreen)
        if self._fullscreen:
            self._draw_fullscreen(surface)
        elif self._in_album:
            album = (
                self._groups[self.selected_index][0] if self._groups else _("Photos")
            )
            _draw_header(surface, self._app.fonts["medium"], album)
            self._draw_grid(surface)
        else:
            _draw_header(surface, self._app.fonts["medium"], _("Photos"))
            _draw_group_list(
                surface,
                self._groups,
                self.selected_index,
                self.scroll_offset,
                self._app.fonts["folder"],
                self._app.cache,
            )

    def _draw_grid(self, surface: pygame.Surface) -> None:
        y_start = HEADER_H
        cell_w = SCREEN_W // GRID_COLS
        cache = self._app.cache

        for i, photo in enumerate(self._photos):
            row, col = divmod(i, GRID_COLS)
            visible_row = row - self._photo_scroll
            if visible_row < 0 or visible_row * GRID_CELL_H + HEADER_H >= VIEW_H:
                continue
            x = col * cell_w
            y = y_start + visible_row * GRID_CELL_H
            is_sel = i == self._photo_sel
            pygame.draw.rect(
                surface, SURFACE_SEL if is_sel else SURFACE, (x, y, cell_w, GRID_CELL_H)
            )
            if is_sel:
                pygame.draw.rect(
                    surface, PRIMARY, (x, y, cell_w, GRID_CELL_H), width=px(2)
                )
            thumb = (
                photo.thumbnail_medium_path
                or photo.thumbnail_large_path
                or photo.thumbnail_small_path
            )
            if thumb:
                img = cache.get(thumb)
                if img:
                    thumb_size = min(cell_w - px(4), GRID_CELL_H - px(4))
                    scaled = scale_fit(img, thumb_size, thumb_size)
                    sw, sh = scaled.get_size()
                    ix = x + (cell_w - sw) // 2
                    iy = y + (GRID_CELL_H - sh) // 2
                    surface.blit(scaled, (ix, iy))

    def _draw_fullscreen(self, surface: pygame.Surface) -> None:
        if not self._photos:
            return
        photo = self._photos[self._photo_sel]
        path = photo.photo_path or photo.thumbnail_large_path
        if path:
            img = self._app.cache.get(path)
            if img:
                scaled = scale_fit(img, SCREEN_W, VIEW_H)
                sw, sh = scaled.get_size()
                surface.blit(scaled, ((SCREEN_W - sw) // 2, (VIEW_H - sh) // 2))
        font = self._app.fonts["small"]
        caption = truncate_text(font, photo.title, SCREEN_W - px(16))
        cap_surf = font.render(caption, True, TEXT_BRIGHT)
        surface.blit(cap_surf, (px(8), VIEW_H - cap_surf.get_height() - px(4)))


# ---------------------------------------------------------------------------
# PlayView
# ---------------------------------------------------------------------------


class PlayView:
    """Full-screen playback view: large art, title, controls."""

    def __init__(self, app: MusicPlayerApp) -> None:
        self._app = app
        self._return_view: str = "home"

    def on_enter(self, return_view: str = "home") -> None:
        """Record which view to return to when B is pressed.

        Args:
            return_view: Name of the view to return to on B press.
        """
        self._return_view = return_view

    def handle_input(self, button: Button) -> UiSound | None:
        if button == Button.LEFT:
            track = self._app.playback.previous_track()
            if track:
                self._app.play_track(
                    track, self._app.playback.playlist, switch_to_play=False
                )
                return UiSound.OPEN
            return UiSound.MOVE
        elif button == Button.RIGHT:
            track = self._app.playback.next_track()
            if track:
                self._app.play_track(
                    track, self._app.playback.playlist, switch_to_play=False
                )
                return UiSound.OPEN
            return UiSound.MOVE
        elif button == Button.CANCEL:
            self._app.switch_view(self._return_view)
            return UiSound.BACK
        return None

    def update(self) -> None:
        pass

    def draw(self, surface: pygame.Surface) -> None:
        """Render the full-screen play view."""
        _fill_background(surface)
        pb = self._app.playback
        if not pb.current_track:
            _draw_no_track(surface, self._app.fonts["medium"])
            return

        track = pb.current_track
        # Art fills the width; reserve ~110px at bottom for title/artist/progress.
        art_w = SCREEN_W - px(32)
        art_h = VIEW_H - px(110)
        art_x = px(16)
        art_y = px(8)

        # Album art
        thumb_path = (
            track.thumbnail_large_path
            or track.thumbnail_medium_path
            or track.thumbnail_small_path
        )
        _draw_art(surface, self._app.cache, thumb_path, art_x, art_y, art_w, art_h)

        # Title
        title_y = art_y + art_h + px(8)
        font_title = self._app.fonts["medium"]
        font_small = self._app.fonts["small"]
        title = truncate_text(font_title, track.title, SCREEN_W - px(32))
        ts = font_title.render(title, True, TEXT_BRIGHT)
        surface.blit(ts, ((SCREEN_W - ts.get_width()) // 2, title_y))

        # Artist
        artist_y = title_y + ts.get_height() + px(4)
        if track.artist:
            artist = truncate_text(font_small, track.artist, SCREEN_W - px(32))
            as_ = font_small.render(artist, True, TEXT_DIM)
            surface.blit(as_, ((SCREEN_W - as_.get_width()) // 2, artist_y))
            artist_y += as_.get_height() + px(4)

        # Progress bar — pinned to bottom of view area
        progress_y = VIEW_H - px(36)
        _draw_progress(surface, pb, progress_y, font_small)


# ---------------------------------------------------------------------------
# SettingsView
# ---------------------------------------------------------------------------


class SettingsView:
    """Settings screen — theme picker and other device preferences.

    LEFT / RIGHT cycles through color themes, unless the child's profile
    chose a theme (then it is fixed and the screen says so).
    B returns to the home screen.
    """

    #: Swatches per row of the picker.
    _COLUMNS = 4

    def __init__(self, app: MusicPlayerApp) -> None:
        self._app = app

    def on_enter(self) -> None:
        pass

    def handle_input(self, button: Button) -> UiSound | None:
        if button in (Button.LEFT, Button.RIGHT):
            if self._app.theme_locked:
                return None
            self._cycle_theme(forward=button == Button.RIGHT)
            return UiSound.SELECT
        elif button == Button.CANCEL:
            self._app.switch_view("home")
            return UiSound.BACK
        return None

    def _cycle_theme(self, forward: bool) -> None:
        current_name = self._app._theme.name
        idx = next((i for i, t in enumerate(THEMES) if t.name == current_name), 0)
        idx = (idx + (1 if forward else -1)) % len(THEMES)
        self._app.set_theme(THEMES[idx])

    def update(self) -> None:
        pass

    def draw(self, surface: pygame.Surface) -> None:
        """Render the settings screen with color-theme swatches."""
        _fill_background(surface)
        font = self._app.fonts["medium"]
        font_sm = self._app.fonts["small"]
        _draw_header(surface, font, _("Settings"))
        locked = self._app.theme_locked

        # Section label
        pad = px(16)
        section_y = HEADER_H + px(12)
        title = _("Theme chosen by your parents") if locked else _("Color Theme")
        lbl = font.render(truncate_text(font, title, SCREEN_W - 2 * pad), True, TEXT)
        surface.blit(lbl, (pad, section_y))

        # Color swatches — a grid, as many rows as the themes need
        cols = self._COLUMNS
        gap = px(8)
        # A card is 110 design pixels wide, but never narrower than its longest
        # label: on a small screen the cards are only ~55 px, and a translated
        # name ("Contraste") would otherwise be cut short.
        label_w = px(4) + max(
            font_sm.size(_(THEME_LABELS.get(t.name, t.name)))[0] for t in THEMES
        )
        swatch_w = min(
            (SCREEN_W - 2 * pad - (cols - 1) * gap) // cols, max(px(110), label_w)
        )
        swatch_h = px(100)
        swatch_y = section_y + lbl.get_height() + px(14)

        current_name = self._app._theme.name
        for ti, t in enumerate(THEMES):
            row, col = divmod(ti, cols)
            in_row = min(cols, len(THEMES) - row * cols)
            row_w = in_row * swatch_w + (in_row - 1) * gap
            sx = (SCREEN_W - row_w) // 2 + col * (swatch_w + gap)
            sy = swatch_y + row * (swatch_h + gap)
            is_active = t.name == current_name

            # Swatch card background
            pygame.draw.rect(
                surface, t.SURFACE, (sx, sy, swatch_w, swatch_h), border_radius=px(8)
            )
            # Primary color block — fills most of the card
            inset = px(6)
            block_h = swatch_h - px(32)
            pygame.draw.rect(
                surface,
                t.PRIMARY,
                (sx + inset, sy + inset, swatch_w - 2 * inset, block_h),
                border_radius=px(4),
            )
            # Accent dot — bottom-right corner of primary block
            dot_r = px(7)
            dot_x = sx + swatch_w - px(14)
            dot_y = sy + inset + block_h - px(10)
            pygame.draw.circle(surface, t.ACCENT, (dot_x, dot_y), dot_r)

            # Active selection border
            if is_active:
                out = px(3)
                pygame.draw.rect(
                    surface,
                    TEXT_BRIGHT,
                    (sx - out, sy - out, swatch_w + 2 * out, swatch_h + 2 * out),
                    width=out,
                    border_radius=px(11),
                )

            # Theme name inside the card
            name_color = TEXT_BRIGHT if is_active else TEXT_DIM
            # The card is sized to its longest label (see above), so the label
            # gets all of it but a small margin.
            label = truncate_text(
                font_sm, _(THEME_LABELS.get(t.name, t.name)), swatch_w - px(4)
            )
            name_surf = font_sm.render(label, True, name_color)
            name_x = sx + (swatch_w - name_surf.get_width()) // 2
            name_y = sy + swatch_h - name_surf.get_height() - px(4)
            surface.blit(name_surf, (name_x, name_y))

        # Button legend at the bottom
        icon_font = self._app.fonts.get("icon_sm")
        if icon_font:
            # FA chevron-left / chevron-right + action text, B to go back
            lft = icon_font.render("\uf053", True, PRIMARY)  # fa-chevron-left
            rgt = icon_font.render("\uf054", True, PRIMARY)  # fa-chevron-right
            theme_lbl = font_sm.render(f" {_('Change theme')}", True, TEXT_DIM)
            back_lbl = font_sm.render(f" {_('Back')}", True, TEXT_DIM)
            row_h = lft.get_height()
            legend_y = VIEW_H - row_h - px(10)
            # "B" badge: filled circle with letter centred inside
            badge_r = row_h // 2
            badge_surf = pygame.Surface((badge_r * 2, badge_r * 2), pygame.SRCALPHA)
            pygame.draw.circle(badge_surf, PRIMARY, (badge_r, badge_r), badge_r)
            b_letter = font_sm.render("B", True, BG)
            bx = badge_r - b_letter.get_width() // 2
            by = badge_r - b_letter.get_height() // 2
            badge_surf.blit(b_letter, (bx, by))
            spacer = font_sm.render("    ", True, TEXT_DIM)
            pieces: list[tuple[pygame.Surface, bool]] = []
            if not locked:
                pieces += [
                    (lft, True),
                    (rgt, True),
                    (theme_lbl, False),
                    (spacer, False),
                ]
            pieces += [(badge_surf, True), (back_lbl, False)]
            total_w = sum(s.get_width() for s, _icon in pieces)
            lx = (SCREEN_W - total_w) // 2
            for surf, is_icon in pieces:
                dy = 0 if is_icon else (row_h - surf.get_height()) // 2
                surface.blit(surf, (lx, legend_y + dy))
                lx += surf.get_width()
        else:
            legend_y = VIEW_H - font_sm.get_height() - px(10)
            entries = [("B", _("Back"))]
            if not locked:
                entries.insert(0, ("< >", _("Change theme")))
            _draw_button_legend(surface, font_sm, legend_y, entries)


# ---------------------------------------------------------------------------
# Private drawing helpers
# ---------------------------------------------------------------------------


def _draw_button_legend(
    surface: pygame.Surface,
    font: pygame.font.Font,
    y: int,
    entries: list[tuple[str, str]],
) -> None:
    """Draw a row of ``[btn] label`` hints centred at the bottom of the view.

    Args:
        surface: Target surface.
        font: Font for the legend text.
        y: Top edge of the legend row.
        entries: List of ``(button_label, action_label)`` pairs.
    """
    gap = px(24)
    parts: list[pygame.Surface] = []
    for btn, action in entries:
        btn_surf = font.render(f"[{btn}]", True, PRIMARY)
        act_surf = font.render(f" {action}", True, TEXT_DIM)
        parts.extend([btn_surf, act_surf])

    total_w = sum(s.get_width() for s in parts) + gap * (len(entries) - 1)
    x = (SCREEN_W - total_w) // 2
    for i, (btn_surf, act_surf) in enumerate(zip(parts[::2], parts[1::2], strict=True)):
        if i > 0:
            x += gap
        surface.blit(btn_surf, (x, y))
        x += btn_surf.get_width()
        surface.blit(act_surf, (x, y))
        x += act_surf.get_width()


def _draw_no_track(surface: pygame.Surface, font: pygame.font.Font) -> None:
    surf = font.render(_("Nothing playing"), True, TEXT_DIM)
    surface.blit(
        surf, ((SCREEN_W - surf.get_width()) // 2, (VIEW_H - surf.get_height()) // 2)
    )


def _draw_art(
    surface: pygame.Surface,
    cache: ImageCache,
    path: str | None,
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    """Draw art at (x, y) scaled to fit w×h preserving aspect ratio."""
    if path:
        img = cache.get(path)
        if img:
            scaled = scale_fit(img, w, h)
            sw, sh = scaled.get_size()
            surface.blit(scaled, (x + (w - sw) // 2, y + (h - sh) // 2))
            return
    pygame.draw.rect(surface, SURFACE, pygame.Rect(x, y, w, h), border_radius=px(6))


def _draw_progress(
    surface: pygame.Surface,
    pb: object,  # PlaybackState
    y: int,
    font: pygame.font.Font,
) -> None:
    """Draw a simple progress bar with elapsed / total text."""
    from .player import PlaybackState

    assert isinstance(pb, PlaybackState)
    track = pb.current_track
    duration = (track.duration_seconds or 0) if track else 0
    pos = int(pb.current_position)
    progress = pos / duration if duration > 0 else 0.0

    bar_x, bar_w, bar_h = px(32), SCREEN_W - px(64), px(6)
    radius = px(3)
    pygame.draw.rect(
        surface, PROGRESS_BG, (bar_x, y, bar_w, bar_h), border_radius=radius
    )
    if progress > 0:
        pygame.draw.rect(
            surface,
            PROGRESS_FG,
            (bar_x, y, int(bar_w * progress), bar_h),
            border_radius=radius,
        )

    def _fmt(secs: int) -> str:
        m, s = divmod(secs, 60)
        return f"{m}:{s:02d}"

    elapsed = font.render(_fmt(pos), True, TEXT_DIM)
    total = font.render(_fmt(duration), True, TEXT_DIM)
    surface.blit(elapsed, (bar_x, y + bar_h + px(2)))
    surface.blit(total, (bar_x + bar_w - total.get_width(), y + bar_h + px(2)))


def _draw_repeat(
    surface: pygame.Surface,
    mode: object,
    font: pygame.font.Font,
    y: int,
) -> None:
    from .player import RepeatMode

    assert isinstance(mode, RepeatMode)
    labels = {
        RepeatMode.ALL: (_("Repeat All"), ACCENT),
        RepeatMode.ONE: (_("Repeat One"), PRIMARY),
    }
    if mode in labels:
        text, color = labels[mode]
        surf = font.render(text, True, color)
        surface.blit(surf, ((SCREEN_W - surf.get_width()) // 2, y))


def _draw_group_list(
    surface: pygame.Surface,
    groups: list[tuple[str, str | None]],
    selected: int,
    scroll: int,
    font: pygame.font.Font,
    cache: ImageCache,
) -> None:
    """Draw a scrolled list of group names with representative thumbnails."""
    visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
    for vi in range(visible):
        idx = scroll + vi
        if idx >= len(groups):
            break
        name, thumb = groups[idx]
        y = HEADER_H + vi * LIST_ITEM_H
        is_sel = idx == selected
        _draw_list_item(surface, 0, y, name, None, is_sel, font, font, cache, thumb)


def _draw_track_list(
    surface: pygame.Surface,
    items: list[MediaRow],
    selected: int,
    scroll: int,
    font_title: pygame.font.Font,
    font_sub: pygame.font.Font,
    cache: ImageCache,
) -> None:
    """Draw a scrolled list of MediaRow items with thumbnails."""
    visible = _visible_items(VIEW_H - HEADER_H, LIST_ITEM_H)
    for vi in range(visible):
        idx = scroll + vi
        if idx >= len(items):
            break
        item = items[idx]
        y = HEADER_H + vi * LIST_ITEM_H
        is_sel = idx == selected
        thumb = item.thumbnail_medium_path or item.thumbnail_small_path
        sub = item.artist or ""
        if item.duration_seconds:
            mins, secs = divmod(item.duration_seconds, 60)
            dur = f"{mins}:{secs:02d}"
            sub = f"{sub}  {dur}".strip() if sub else dur
        _draw_list_item(
            surface,
            0,
            y,
            item.title,
            sub or None,
            is_sel,
            font_title,
            font_sub,
            cache,
            thumb,
        )


def _visible_items(available_height: int, item_height: int) -> int:
    return max(1, available_height // item_height)


# ---------------------------------------------------------------------------
# SleepView
# ---------------------------------------------------------------------------

SLEEP_BG = (4, 4, 8)
SLEEP_FG = (54, 58, 80)


def until_label(wake_at: datetime) -> str:
    """Say when bedtime ends, e.g. "Until 07:30" / "Hasta las 07:30".

    The clock stays 24-hour in every language, as in the parents' bedtime
    settings, so "07:30" and "19:30" cannot be confused on a dim screen. The
    hour picks the form because Spanish says "la 1:00" but "las 2:00": a
    one-o'clock wake time is the singular of the message, and each language's
    message decides how its hour is written.

    Args:
        wake_at: Local wall-clock time bedtime ends.

    Returns:
        The translated line.
    """
    # The singular is the one-o'clock message: it spells the hour out so that
    # Spanish can write "la 1:00" while English keeps "01:00".
    return ngettext(
        "Until 01:{minute}",
        "Until {hour}:{minute}",
        wake_at.hour,
    ).format(hour=f"{wake_at.hour:02d}", minute=f"{wake_at.minute:02d}")


class SleepView:
    """Bedtime sleep screen: dim, no playback, ignores the buttons.

    Drawn over the whole screen (the app hides the playback bar for it).
    """

    def __init__(self, app: MusicPlayerApp) -> None:
        self._app = app

    def on_enter(self) -> None:
        pass

    def handle_input(self, button: Button) -> UiSound | None:
        """Ignore every button: nothing to do until wake time."""
        del button
        return None

    def update(self) -> None:
        pass

    def draw(self, surface: pygame.Surface) -> None:
        """Render a moon, "Bedtime" and the wake time, all dim."""
        surface.fill(SLEEP_BG)
        w, h = surface.get_size()
        icon_font = self._app.fonts.get("icon")
        font = self._app.fonts["large"]
        font_sm = self._app.fonts["small"]
        parts: list[pygame.Surface] = []
        if icon_font:
            parts.append(icon_font.render("\uf186", True, SLEEP_FG))  # fa-moon
        parts.append(font.render(_("Bedtime"), True, SLEEP_FG))
        wake_at = self._app.bedtime_wake_at
        if wake_at is not None:
            parts.append(font_sm.render(until_label(wake_at), True, SLEEP_FG))
        gap = px(16)
        total = sum(p.get_height() for p in parts) + gap * (len(parts) - 1)
        y = (h - total) // 2
        for part in parts:
            surface.blit(part, ((w - part.get_width()) // 2, y))
            y += part.get_height() + gap
