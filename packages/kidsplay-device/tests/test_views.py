"""Tests for view input-handling logic.

These tests verify that handle_input() correctly mutates selected_index,
scroll_offset, and internal mode flags.  No rendering is tested; the
screen mock is a MagicMock so draw() would no-op.

The app stub provides only the attributes views actually access:
  - app.switch_view(name)
  - app.play_track(track, playlist)
  - app.playback (PlaybackState)
  - app.db_path (Path to a real temp DB so on_enter() can query it)
  - app.cache (a MagicMock)
  - app.fonts (dict of MagicMock fonts)
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kidsplay_device.database import MediaRow, init_db, upsert_media_item
from kidsplay_device.player import PlaybackState
from kidsplay_device.views import (
    GRID_COLS,
    AudiobooksView,
    Button,
    HomeView,
    MusicView,
    PhotosView,
    PlayView,
    clamp_scroll,
)
from kidsplay_models.media import MediaType
from kidsplay_models.sync import SyncMediaEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(db_path: Path) -> MagicMock:
    """Build a minimal app stub."""
    app = MagicMock()
    app.db_path = db_path
    app.playback = PlaybackState()
    app.cache = MagicMock()
    app.cache.get.return_value = None
    font = MagicMock()
    font.render.return_value = MagicMock(get_width=lambda: 10, get_height=lambda: 10)
    font.size.return_value = (10, 10)
    app.fonts = {"small": font, "medium": font, "large": font}
    return app


def _make_media_row(
    media_id: str,
    media_type: str = "music",
    playlist_title: str = "Album A",
    title: str = "Track 1",
) -> MediaRow:
    return MediaRow(
        media_id=media_id,
        media_type=media_type,
        playlist_title=playlist_title,
        title=title,
        artist="Artist",
        duration_seconds=180,
        audio_path=f"/media/{media_id}.mp3",
        photo_path=None,
        thumbnail_small_path=None,
        thumbnail_medium_path=None,
        thumbnail_large_path=None,
    )


def _populate_db(db_path: Path, rows: list[MediaRow]) -> None:
    """Write rows directly into the device DB."""
    conn = init_db(db_path)
    for row in rows:
        # Build a SyncMediaEntry from the row so upsert_media_item works.
        import uuid

        mid = uuid.UUID(row.media_id) if _is_uuid(row.media_id) else uuid.uuid4()
        entry = SyncMediaEntry(
            media_id=mid,
            media_type=MediaType(row.media_type),
            playlist_title=row.playlist_title,
            title=row.title,
            artist=row.artist,
            duration_seconds=row.duration_seconds,
            audio_path=row.audio_path,
            photo_path=row.photo_path,
            thumbnail_paths={},
        )
        upsert_media_item(conn, entry)
    conn.commit()
    conn.close()


def _is_uuid(s: str) -> bool:
    try:
        import uuid

        uuid.UUID(s)
        return True
    except ValueError:
        return False


def _make_rows_for_db(
    n: int,
    media_type: str = "music",
    playlist_title: str = "Album A",
) -> list[MediaRow]:
    import uuid

    return [
        _make_media_row(
            str(uuid.uuid4()),
            media_type=media_type,
            playlist_title=playlist_title,
            title=f"Track {i}",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# clamp_scroll
# ---------------------------------------------------------------------------


class TestClampScroll:
    def test_no_scroll_when_all_fit(self) -> None:
        assert clamp_scroll(3, 10, 5) == 0

    def test_scroll_keeps_selected_visible(self) -> None:
        # 3 visible, 10 total, selected=7 → scroll=5
        assert clamp_scroll(7, 3, 10) == 5

    def test_scroll_does_not_exceed_max(self) -> None:
        # max offset = total - visible = 10 - 3 = 7
        assert clamp_scroll(9, 3, 10) == 7

    def test_zero_selection(self) -> None:
        assert clamp_scroll(0, 3, 10) == 0

    def test_single_item(self) -> None:
        assert clamp_scroll(0, 5, 1) == 0


# ---------------------------------------------------------------------------
# HomeView
# ---------------------------------------------------------------------------


class TestHomeView:
    def test_right_moves_selection(self, tmp_path: Path) -> None:
        view = HomeView(_make_app(tmp_path / "db.sqlite"))
        view.selected_index = 0
        view.handle_input(Button.RIGHT)
        assert view.selected_index == 1

    def test_right_does_not_go_past_end(self, tmp_path: Path) -> None:
        view = HomeView(_make_app(tmp_path / "db.sqlite"))
        view.selected_index = 3  # last item (2×2 grid, 4 items, index 3)
        view.handle_input(Button.RIGHT)
        assert view.selected_index == 3

    def test_left_moves_selection(self, tmp_path: Path) -> None:
        view = HomeView(_make_app(tmp_path / "db.sqlite"))
        view.selected_index = 1
        view.handle_input(Button.LEFT)
        assert view.selected_index == 0

    def test_left_does_not_go_negative(self, tmp_path: Path) -> None:
        view = HomeView(_make_app(tmp_path / "db.sqlite"))
        view.selected_index = 0
        view.handle_input(Button.LEFT)
        assert view.selected_index == 0

    def test_down_moves_to_second_row(self, tmp_path: Path) -> None:
        view = HomeView(_make_app(tmp_path / "db.sqlite"))
        view.selected_index = 0
        view.handle_input(Button.DOWN)
        assert view.selected_index == 2

    def test_up_moves_to_first_row(self, tmp_path: Path) -> None:
        view = HomeView(_make_app(tmp_path / "db.sqlite"))
        view.selected_index = 2
        view.handle_input(Button.UP)
        assert view.selected_index == 0

    def test_select_calls_switch_view(self, tmp_path: Path) -> None:
        app = _make_app(tmp_path / "db.sqlite")
        view = HomeView(app)
        view.selected_index = 0  # "Music"
        view.handle_input(Button.SELECT)
        app.switch_view.assert_called_once_with("music")


# ---------------------------------------------------------------------------
# MusicView — group list level
# ---------------------------------------------------------------------------


class TestMusicViewGroups:
    @pytest.fixture
    def view_with_groups(self, tmp_path: Path) -> tuple[MusicView, MagicMock]:
        db_path = tmp_path / "db.sqlite"
        rows = _make_rows_for_db(2, "music", "Album A") + _make_rows_for_db(
            2, "music", "Album B"
        )
        _populate_db(db_path, rows)
        app = _make_app(db_path)
        view = MusicView(app)
        view.on_enter()
        return view, app

    def test_on_enter_loads_groups(
        self, view_with_groups: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_with_groups
        assert len(view._groups) == 2

    def test_down_advances_selection(
        self, view_with_groups: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_with_groups
        view.selected_index = 0
        view.handle_input(Button.DOWN)
        assert view.selected_index == 1

    def test_down_does_not_exceed_last(
        self, view_with_groups: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_with_groups
        view.selected_index = 1
        view.handle_input(Button.DOWN)
        assert view.selected_index == 1

    def test_up_decrements_selection(
        self, view_with_groups: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_with_groups
        view.selected_index = 1
        view.handle_input(Button.UP)
        assert view.selected_index == 0

    def test_up_does_not_go_negative(
        self, view_with_groups: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_with_groups
        view.selected_index = 0
        view.handle_input(Button.UP)
        assert view.selected_index == 0

    def test_select_enters_group(
        self, view_with_groups: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_with_groups
        view.selected_index = 0
        view.handle_input(Button.SELECT)
        assert view._in_group is True
        assert len(view._tracks) == 2

    def test_cancel_calls_switch_view_home(
        self, view_with_groups: tuple[MusicView, MagicMock]
    ) -> None:
        view, app = view_with_groups
        view.handle_input(Button.CANCEL)
        app.switch_view.assert_called_once_with("home")


# ---------------------------------------------------------------------------
# MusicView — track list level
# ---------------------------------------------------------------------------


class TestMusicViewTracks:
    @pytest.fixture
    def view_in_tracks(self, tmp_path: Path) -> tuple[MusicView, MagicMock]:
        db_path = tmp_path / "db.sqlite"
        rows = _make_rows_for_db(4, "music", "Album A")
        _populate_db(db_path, rows)
        app = _make_app(db_path)
        view = MusicView(app)
        view.on_enter()
        view.selected_index = 0
        view.handle_input(Button.SELECT)  # enter tracks
        return view, app

    def test_down_advances_track_selection(
        self, view_in_tracks: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_in_tracks
        view._track_sel = 0
        view.handle_input(Button.DOWN)
        assert view._track_sel == 1

    def test_up_decrements_track_selection(
        self, view_in_tracks: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_in_tracks
        view._track_sel = 2
        view.handle_input(Button.UP)
        assert view._track_sel == 1

    def test_cancel_returns_to_group_list(
        self, view_in_tracks: tuple[MusicView, MagicMock]
    ) -> None:
        view, _ = view_in_tracks
        view.handle_input(Button.CANCEL)
        assert view._in_group is False

    def test_select_calls_play_track(
        self, view_in_tracks: tuple[MusicView, MagicMock]
    ) -> None:
        view, app = view_in_tracks
        view._track_sel = 0
        view.handle_input(Button.SELECT)
        assert app.play_track.called


# ---------------------------------------------------------------------------
# AudiobooksView
# ---------------------------------------------------------------------------


class TestAudiobooksView:
    @pytest.fixture
    def view_with_books(self, tmp_path: Path) -> tuple[AudiobooksView, MagicMock]:
        db_path = tmp_path / "db.sqlite"
        rows = _make_rows_for_db(3, "audiobook", "Gruffalo") + _make_rows_for_db(
            2, "audiobook", "Elmer"
        )
        _populate_db(db_path, rows)
        app = _make_app(db_path)
        view = AudiobooksView(app)
        view.on_enter()
        return view, app

    def test_on_enter_loads_books(
        self, view_with_books: tuple[AudiobooksView, MagicMock]
    ) -> None:
        view, _ = view_with_books
        # Two books, not five chapters -- the top level lists books.
        assert [g[0] for g in view._groups] == ["Elmer", "Gruffalo"]
        assert view._in_book is False

    def test_down_advances_selection(
        self, view_with_books: tuple[AudiobooksView, MagicMock]
    ) -> None:
        view, _ = view_with_books
        view.selected_index = 0
        view.handle_input(Button.DOWN)
        assert view.selected_index == 1

    def test_up_decrements_selection(
        self, view_with_books: tuple[AudiobooksView, MagicMock]
    ) -> None:
        view, _ = view_with_books
        view.selected_index = 1
        view.handle_input(Button.UP)
        assert view.selected_index == 0

    def test_cancel_goes_home(
        self, view_with_books: tuple[AudiobooksView, MagicMock]
    ) -> None:
        view, app = view_with_books
        view.handle_input(Button.CANCEL)
        app.switch_view.assert_called_once_with("home")

    def test_select_enters_book_and_loads_chapters(
        self, view_with_books: tuple[AudiobooksView, MagicMock]
    ) -> None:
        view, app = view_with_books
        view.selected_index = 1  # "Gruffalo", the 3-chapter book
        view.handle_input(Button.SELECT)
        assert view._in_book is True
        assert len(view._chapters) == 3
        assert not app.play_track.called, "entering a book must not start playback"

    def test_select_plays_chapter_with_same_book_as_playlist(
        self, view_with_books: tuple[AudiobooksView, MagicMock]
    ) -> None:
        view, app = view_with_books
        view.selected_index = 1  # "Gruffalo"
        view.handle_input(Button.SELECT)  # into the book
        view._chapter_sel = 0
        view.handle_input(Button.SELECT)  # play the chapter

        app.play_track.assert_called_once()
        track, playlist = app.play_track.call_args.args
        assert track is view._chapters[0]
        # The playlist is the book's own chapters, not the whole library.
        assert playlist == view._chapters
        assert {r.playlist_title for r in playlist} == {"Gruffalo"}

    def test_cancel_in_book_returns_to_book_list(
        self, view_with_books: tuple[AudiobooksView, MagicMock]
    ) -> None:
        view, app = view_with_books
        view.handle_input(Button.SELECT)  # into a book
        view.handle_input(Button.CANCEL)
        assert view._in_book is False
        app.switch_view.assert_not_called()

    def test_scroll_updates_on_navigation(
        self, view_with_books: tuple[AudiobooksView, MagicMock]
    ) -> None:
        view, _ = view_with_books
        # Both books fit on screen at this VIEW_H, so there is nothing to
        # scroll; just verify navigating does not crash or go negative.
        view.handle_input(Button.DOWN)
        assert view.scroll_offset >= 0


# ---------------------------------------------------------------------------
# PhotosView — grid mode
# ---------------------------------------------------------------------------


class TestPhotosViewAlbums:
    """Top level: the album list PhotosView opens on."""

    @pytest.fixture
    def view_at_albums(self, tmp_path: Path) -> tuple[PhotosView, MagicMock]:
        db_path = tmp_path / "db.sqlite"
        _populate_db(db_path, _make_rows_for_db(7, "photo", "Vacation"))
        app = _make_app(db_path)
        view = PhotosView(app)
        view.on_enter()
        return view, app

    def test_on_enter_loads_albums(
        self, view_at_albums: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_at_albums
        assert [g[0] for g in view._groups] == ["Vacation"]
        assert view._in_album is False

    def test_select_enters_album_and_loads_its_photos(
        self, view_at_albums: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_at_albums
        view.selected_index = 0
        view.handle_input(Button.SELECT)
        assert view._in_album is True
        assert len(view._photos) == 7
        assert view._photo_sel == 0

    def test_cancel_goes_home(
        self, view_at_albums: tuple[PhotosView, MagicMock]
    ) -> None:
        view, app = view_at_albums
        view.handle_input(Button.CANCEL)
        app.switch_view.assert_called_once_with("home")


# ---------------------------------------------------------------------------
# PhotosView — thumbnail grid (inside an album)
# ---------------------------------------------------------------------------


class TestPhotosViewGrid:
    """Grid level. Selection lives in ``_photo_sel``, not ``selected_index``
    (which stays on the album row), and movement is clamped per row."""

    @pytest.fixture
    def view_in_album(self, tmp_path: Path) -> tuple[PhotosView, MagicMock]:
        db_path = tmp_path / "db.sqlite"
        _populate_db(db_path, _make_rows_for_db(7, "photo", "Vacation"))
        app = _make_app(db_path)
        view = PhotosView(app)
        view.on_enter()
        view.handle_input(Button.SELECT)  # drill into the album
        return view, app

    def test_album_photos_loaded(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        assert len(view._photos) == 7

    def test_right_moves_within_row(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        view._photo_sel = 0
        view.handle_input(Button.RIGHT)
        assert view._photo_sel == 1

    def test_right_does_not_cross_row_boundary(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        view._photo_sel = GRID_COLS - 1  # last cell of the first row
        view.handle_input(Button.RIGHT)
        assert view._photo_sel == GRID_COLS - 1

    def test_left_moves_within_row(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        view._photo_sel = 1
        view.handle_input(Button.LEFT)
        assert view._photo_sel == 0

    def test_left_does_not_cross_row_boundary(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        view._photo_sel = 0
        view.handle_input(Button.LEFT)
        assert view._photo_sel == 0

    def test_down_moves_to_next_row(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        view._photo_sel = 0
        view.handle_input(Button.DOWN)
        assert view._photo_sel == GRID_COLS

    def test_down_clamped_when_no_next_row(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        view._photo_sel = 6  # 7 photos, so no full row below
        view.handle_input(Button.DOWN)
        assert view._photo_sel == 6

    def test_up_moves_to_previous_row(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        view._photo_sel = GRID_COLS
        view.handle_input(Button.UP)
        assert view._photo_sel == 0

    def test_select_enters_fullscreen(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, _ = view_in_album
        view.handle_input(Button.SELECT)
        assert view._fullscreen is True

    def test_cancel_returns_to_album_list(
        self, view_in_album: tuple[PhotosView, MagicMock]
    ) -> None:
        view, app = view_in_album
        view.handle_input(Button.CANCEL)
        assert view._in_album is False
        app.switch_view.assert_not_called()


# ---------------------------------------------------------------------------
# PhotosView — fullscreen mode
# ---------------------------------------------------------------------------


class TestPhotosViewFullscreen:
    """Fullscreen steps through the whole album, ignoring row boundaries."""

    @pytest.fixture
    def view_fullscreen(self, tmp_path: Path) -> PhotosView:
        db_path = tmp_path / "db.sqlite"
        _populate_db(db_path, _make_rows_for_db(5, "photo", "Vacation"))
        app = _make_app(db_path)
        view = PhotosView(app)
        view.on_enter()
        view.handle_input(Button.SELECT)  # into the album
        view._photo_sel = 2
        view.handle_input(Button.SELECT)  # into fullscreen
        return view

    def test_right_advances_in_fullscreen(self, view_fullscreen: PhotosView) -> None:
        view_fullscreen.handle_input(Button.RIGHT)
        assert view_fullscreen._photo_sel == 3

    def test_right_crosses_row_boundary(self, view_fullscreen: PhotosView) -> None:
        view_fullscreen._photo_sel = GRID_COLS - 1
        view_fullscreen.handle_input(Button.RIGHT)
        assert view_fullscreen._photo_sel == GRID_COLS

    def test_left_goes_back_in_fullscreen(self, view_fullscreen: PhotosView) -> None:
        view_fullscreen.handle_input(Button.LEFT)
        assert view_fullscreen._photo_sel == 1

    def test_right_clamped_at_last(self, view_fullscreen: PhotosView) -> None:
        view_fullscreen._photo_sel = 4
        view_fullscreen.handle_input(Button.RIGHT)
        assert view_fullscreen._photo_sel == 4

    def test_left_clamped_at_zero(self, view_fullscreen: PhotosView) -> None:
        view_fullscreen._photo_sel = 0
        view_fullscreen.handle_input(Button.LEFT)
        assert view_fullscreen._photo_sel == 0

    def test_cancel_exits_fullscreen_to_grid(self, view_fullscreen: PhotosView) -> None:
        view_fullscreen.handle_input(Button.CANCEL)
        assert view_fullscreen._fullscreen is False
        assert view_fullscreen._in_album is True


# ---------------------------------------------------------------------------
# PlayView
# ---------------------------------------------------------------------------


class TestPlayView:
    @pytest.fixture
    def view(self, tmp_path: Path) -> tuple[PlayView, MagicMock]:
        import uuid

        app = _make_app(tmp_path / "db.sqlite")
        v = PlayView(app)
        v.on_enter(return_view="music")
        tracks = [_make_media_row(str(uuid.uuid4()), title=f"T{i}") for i in range(3)]
        app.playback.load_track(tracks[1], tracks)
        app.playback.is_playing = True
        return v, app

    def test_left_calls_previous_track_and_play(
        self, view: tuple[PlayView, MagicMock]
    ) -> None:
        v, app = view
        v.handle_input(Button.LEFT)
        assert app.play_track.called

    def test_right_calls_next_track_and_play(
        self, view: tuple[PlayView, MagicMock]
    ) -> None:
        v, app = view
        v.handle_input(Button.RIGHT)
        assert app.play_track.called

    def test_cancel_calls_switch_view_to_return_view(
        self, view: tuple[PlayView, MagicMock]
    ) -> None:
        v, app = view
        v.handle_input(Button.CANCEL)
        app.switch_view.assert_called_once_with("music")

    def test_left_no_crash_when_playlist_empty(self, tmp_path: Path) -> None:
        app = _make_app(tmp_path / "db.sqlite")
        v = PlayView(app)
        v.on_enter()
        v.handle_input(Button.LEFT)  # should not raise

    def test_right_no_crash_when_playlist_empty(self, tmp_path: Path) -> None:
        app = _make_app(tmp_path / "db.sqlite")
        v = PlayView(app)
        v.on_enter()
        v.handle_input(Button.RIGHT)  # should not raise
