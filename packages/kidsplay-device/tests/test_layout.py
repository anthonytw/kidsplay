"""Tests for the screen layout arithmetic."""

import pytest

from kidsplay_device.layout import DEFAULT_LAYOUT, VISIBLE_ROWS, Layout

SIZES = [(240, 180), (320, 240), (640, 480), (800, 480), (1280, 720)]


def test_reference_layout_equals_the_original_constants() -> None:
    """At 640x480 every value is the constant views.py used to hard-code."""
    lay = DEFAULT_LAYOUT
    assert (lay.width, lay.height, lay.scale) == (640, 480, 1.0)
    assert lay.bar_h == 72
    assert lay.view_h == 408
    assert lay.header_h == 36
    assert lay.list_item_h == 124
    assert lay.grid_cell_h == 124
    assert lay.grid_cols == 3
    assert lay.thumb_small == 56
    assert [lay.px(n) for n in (1, 4, 8, 16, 110)] == [1, 4, 8, 16, 110]


@pytest.mark.parametrize(("width", "height"), SIZES)
def test_three_rows_fit_exactly_at_every_size(width: int, height: int) -> None:
    lay = Layout.for_size(width, height)
    assert lay.view_h + lay.bar_h == height
    rows = (lay.view_h - lay.header_h) // lay.list_item_h
    assert rows == VISIBLE_ROWS
    # The leftover is less than one pixel per row: nothing is wasted or cut.
    assert lay.view_h - lay.header_h - rows * lay.list_item_h < VISIBLE_ROWS


def test_scale_follows_the_tighter_axis() -> None:
    assert Layout.for_size(320, 240).scale == 0.5
    assert Layout.for_size(1280, 720).scale == 1.5
    # Wider than 4:3: the height decides, the extra width goes to the lists.
    assert Layout.for_size(800, 480).scale == 1.0
    # Taller than 4:3: the width decides.
    assert Layout.for_size(640, 960).scale == 1.0


def test_px_never_reaches_zero() -> None:
    assert Layout.for_size(240, 180).px(1) == 1
    assert Layout.for_size(240, 180).px(0.1) == 1


@pytest.mark.parametrize(("width", "height"), [(239, 480), (640, 179), (0, 0)])
def test_too_small_screens_are_refused(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="below the supported minimum"):
        Layout.for_size(width, height)


@pytest.mark.parametrize(("width", "height"), SIZES + [(1920, 480), (1024, 600)])
def test_grid_cells_are_never_stretched_into_mostly_empty_boxes(
    width: int, height: int
) -> None:
    """The photo grid's thumbnails are square, so a cell much wider than tall
    wastes most of its width (800×480 and 1280×720 used to: 2.2 : 1)."""
    lay = Layout.for_size(width, height)
    aspect = (width / lay.grid_cols) / lay.grid_cell_h
    assert 1.2 <= aspect <= 2.0, f"{lay.grid_cols} columns give a {aspect:.2f}:1 cell"


@pytest.mark.parametrize(
    ("width", "height", "cols"),
    [(240, 180, 3), (320, 240, 3), (640, 480, 3), (800, 480, 4), (1280, 720, 4)],
)
def test_wider_screens_get_more_grid_columns(
    width: int, height: int, cols: int
) -> None:
    assert Layout.for_size(width, height).grid_cols == cols
