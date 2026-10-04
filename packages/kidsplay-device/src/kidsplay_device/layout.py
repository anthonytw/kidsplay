"""Screen geometry: one design at 640×480, scaled to any resolution.

Every size in the views was written for the reference 640×480 screen of the
GPi Case 2. ``Layout`` turns those numbers into the ones for the screen the
player actually opened: everything scales by ``min(width / 640, height / 480)``,
so the design keeps its proportions and, on a wider screen (800×480), the
extra width goes to the lists and grids rather than to bigger controls.

At 640×480 the scale is exactly 1, so every value below equals the constant it
replaced and the screens draw pixel for pixel as before.

Pure arithmetic, no pygame, so it is tested without a display.
"""

from dataclasses import dataclass

REFERENCE_WIDTH = 640
REFERENCE_HEIGHT = 480

MIN_WIDTH = 240
MIN_HEIGHT = 180
"""Below this the text is too small to read at all; smaller sizes are refused."""

#: Rows shown at once in the lists and the photo grid.
VISIBLE_ROWS = 3

#: Photo grid columns on the reference screen, and the width : height of a grid
#: cell there (213 × 124). A wider screen gets more columns, to keep its cells
#: about this shape instead of stretching them into wide, mostly empty boxes.
REFERENCE_GRID_COLS = 3
REFERENCE_CELL_ASPECT = (REFERENCE_WIDTH / REFERENCE_GRID_COLS) / 124


@dataclass(frozen=True)
class Layout:
    """Sizes of every part of the screen, in pixels.

    Attributes:
        width: Screen width.
        height: Screen height.
        scale: Factor from the 640×480 design to this screen.
        bar_h: Playback bar height, at the bottom of the screen.
        view_h: Height available to a view (above the bar).
        header_h: Title bar height.
        list_item_h: Height of a list row; exactly ``VISIBLE_ROWS`` fit.
        thumb_small: Thumbnail size in the playback bar.
        grid_cell_h: Height of a photo grid cell.
        grid_cols: Photo grid columns: 3 on 4:3 screens, more on wider ones.
    """

    width: int
    height: int
    scale: float
    bar_h: int
    view_h: int
    header_h: int
    list_item_h: int
    thumb_small: int
    grid_cell_h: int
    grid_cols: int

    @classmethod
    def for_size(cls, width: int, height: int) -> "Layout":
        """Work out the layout for a screen size.

        Args:
            width: Screen width in pixels.
            height: Screen height in pixels.

        Returns:
            The layout.

        Raises:
            ValueError: If the screen is smaller than ``MIN_WIDTH`` ×
                ``MIN_HEIGHT``.
        """
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            raise ValueError(
                f"screen size {width}x{height} is below the supported minimum "
                f"{MIN_WIDTH}x{MIN_HEIGHT}"
            )
        scale = min(width / REFERENCE_WIDTH, height / REFERENCE_HEIGHT)

        def px(value: int) -> int:
            return max(1, round(value * scale))

        bar_h = px(72)
        view_h = height - bar_h
        header_h = px(36)
        row_h = (view_h - header_h) // VISIBLE_ROWS
        return cls(
            width=width,
            height=height,
            scale=scale,
            bar_h=bar_h,
            view_h=view_h,
            header_h=header_h,
            list_item_h=row_h,
            thumb_small=px(56),
            grid_cell_h=row_h,
            grid_cols=max(
                REFERENCE_GRID_COLS, round(width / (row_h * REFERENCE_CELL_ASPECT))
            ),
        )

    def px(self, value: int | float) -> int:
        """Scale a length from the 640×480 design.

        Args:
            value: Length in reference pixels.

        Returns:
            The length on this screen, at least 1.
        """
        return max(1, round(value * self.scale))


DEFAULT_LAYOUT = Layout.for_size(REFERENCE_WIDTH, REFERENCE_HEIGHT)
