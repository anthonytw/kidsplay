"""Unit tests for PlaybackState.

No pygame dependency.  All tests use MediaRow objects constructed directly.
"""

from kidsplay_device.database import MediaRow
from kidsplay_device.player import RESTART_THRESHOLD, PlaybackState, RepeatMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_track(media_id: str, title: str = "") -> MediaRow:
    return MediaRow(
        media_id=media_id,
        media_type="music",
        playlist_title="Test Playlist",
        title=title or media_id,
        artist=None,
        duration_seconds=180,
        audio_path=f"/media/{media_id}.mp3",
        photo_path=None,
        thumbnail_small_path=None,
        thumbnail_medium_path=None,
        thumbnail_large_path=None,
    )


def make_playlist(n: int) -> list[MediaRow]:
    return [make_track(f"t{i}", f"Track {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# load_track
# ---------------------------------------------------------------------------


class TestLoadTrack:
    def test_sets_current_track(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[1], playlist)
        assert ps.current_track is playlist[1]

    def test_sets_playlist(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[0], playlist)
        assert ps.playlist is playlist

    def test_finds_index_in_playlist(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(5)
        ps.load_track(playlist[3], playlist)
        assert ps.playlist_index == 3

    def test_resets_position(self) -> None:
        ps = PlaybackState()
        ps.current_position = 99.0
        playlist = make_playlist(2)
        ps.load_track(playlist[0], playlist)
        assert ps.current_position == 0.0

    def test_index_defaults_to_zero_if_track_not_found(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        orphan = make_track("orphan")
        ps.load_track(orphan, playlist)
        assert ps.playlist_index == 0


# ---------------------------------------------------------------------------
# next_track
# ---------------------------------------------------------------------------


class TestNextTrack:
    def test_advances_to_next(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[0], playlist)
        nxt = ps.next_track()
        assert nxt is playlist[1]
        assert ps.playlist_index == 1

    def test_returns_none_at_end_no_repeat(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[2], playlist)
        assert ps.next_track() is None

    def test_wraps_with_repeat_all(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[2], playlist)
        ps.repeat_mode = RepeatMode.ALL
        nxt = ps.next_track()
        assert nxt is playlist[0]
        assert ps.playlist_index == 0

    def test_repeat_one_returns_same_track(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[1], playlist)
        ps.repeat_mode = RepeatMode.ONE
        nxt = ps.next_track()
        assert nxt is playlist[1]
        assert ps.playlist_index == 1  # index unchanged

    def test_returns_none_on_empty_playlist(self) -> None:
        ps = PlaybackState()
        assert ps.next_track() is None

    def test_resets_position_on_advance(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(2)
        ps.load_track(playlist[0], playlist)
        ps.current_position = 60.0
        ps.next_track()
        assert ps.current_position == 0.0


# ---------------------------------------------------------------------------
# previous_track
# ---------------------------------------------------------------------------


class TestPreviousTrack:
    def test_goes_to_previous(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[2], playlist)
        prev = ps.previous_track()
        assert prev is playlist[1]
        assert ps.playlist_index == 1

    def test_restarts_current_when_past_threshold(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[2], playlist)
        ps.current_position = RESTART_THRESHOLD + 1.0
        prev = ps.previous_track()
        assert prev is playlist[2]
        assert ps.current_position == 0.0

    def test_restarts_current_at_start_of_list_no_repeat(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[0], playlist)
        ps.current_position = 0.0
        prev = ps.previous_track()
        assert prev is playlist[0]
        assert ps.current_position == 0.0

    def test_wraps_with_repeat_all(self) -> None:
        ps = PlaybackState()
        playlist = make_playlist(3)
        ps.load_track(playlist[0], playlist)
        ps.current_position = 0.0
        ps.repeat_mode = RepeatMode.ALL
        prev = ps.previous_track()
        assert prev is playlist[2]
        assert ps.playlist_index == 2

    def test_returns_none_on_empty_playlist(self) -> None:
        ps = PlaybackState()
        assert ps.previous_track() is None

    def test_exactly_at_threshold_restarts(self) -> None:
        """Position == threshold should NOT trigger restart (> not >=)."""
        ps = PlaybackState()
        playlist = make_playlist(2)
        ps.load_track(playlist[1], playlist)
        ps.current_position = RESTART_THRESHOLD  # exactly at boundary
        prev = ps.previous_track()
        # Not > threshold, so should go to previous track
        assert prev is playlist[0]


# ---------------------------------------------------------------------------
# toggle_repeat
# ---------------------------------------------------------------------------


class TestToggleRepeat:
    def test_none_to_all(self) -> None:
        ps = PlaybackState()
        mode = ps.toggle_repeat()
        assert mode == RepeatMode.ALL
        assert ps.repeat_mode == RepeatMode.ALL

    def test_all_to_one(self) -> None:
        ps = PlaybackState()
        ps.repeat_mode = RepeatMode.ALL
        mode = ps.toggle_repeat()
        assert mode == RepeatMode.ONE

    def test_one_to_none(self) -> None:
        ps = PlaybackState()
        ps.repeat_mode = RepeatMode.ONE
        mode = ps.toggle_repeat()
        assert mode == RepeatMode.NONE

    def test_full_cycle(self) -> None:
        ps = PlaybackState()
        assert ps.repeat_mode == RepeatMode.NONE
        ps.toggle_repeat()
        assert ps.repeat_mode == RepeatMode.ALL
        ps.toggle_repeat()
        assert ps.repeat_mode == RepeatMode.ONE
        ps.toggle_repeat()
        assert ps.repeat_mode == RepeatMode.NONE


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_track_next_no_repeat_returns_none(self) -> None:
        ps = PlaybackState()
        t = make_track("solo")
        ps.load_track(t, [t])
        assert ps.next_track() is None

    def test_single_track_next_repeat_all_returns_same(self) -> None:
        ps = PlaybackState()
        t = make_track("solo")
        ps.load_track(t, [t])
        ps.repeat_mode = RepeatMode.ALL
        result = ps.next_track()
        assert result is t

    def test_single_track_previous_restarts(self) -> None:
        ps = PlaybackState()
        t = make_track("solo")
        ps.load_track(t, [t])
        ps.current_position = 0.0
        result = ps.previous_track()
        assert result is t
