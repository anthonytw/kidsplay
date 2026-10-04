"""Playback state machine.

Pure logic — no pygame dependency.  The app layer owns the mixer calls;
this module only tracks *what* should be playing and *where* in the
playlist we are.

Repeat cycle: NONE → ALL → ONE → NONE.

Previous-track semantics:
  - If position > RESTART_THRESHOLD seconds, restart the current track.
  - Otherwise, go to the previous track (or restart at start if at index 0
    and repeat is not ALL).
"""

from enum import Enum, auto

from .database import MediaRow

RESTART_THRESHOLD: float = 3.0


class RepeatMode(Enum):
    """Repeat mode for playlist playback."""

    NONE = auto()
    ALL = auto()
    ONE = auto()


class PlaybackState:
    """Tracks the current track, playlist, position, and repeat mode.

    This class has no side effects — callers are responsible for starting /
    stopping the actual audio mixer in response to the tracks returned by
    ``load_track``, ``next_track``, and ``previous_track``.

    Attributes:
        current_track: The currently loaded track, or ``None``.
        playlist: The full ordered list of tracks in the current context.
        playlist_index: Index of ``current_track`` within ``playlist``.
        is_playing: True if audio is actively playing (not paused).
        is_paused: True if playback is paused (implies ``is_playing``).
        repeat_mode: Current repeat mode.
        current_position: Playback position in seconds (updated by app).
    """

    def __init__(self) -> None:
        self.current_track: MediaRow | None = None
        self.playlist: list[MediaRow] = []
        self.playlist_index: int = 0
        self.is_playing: bool = False
        self.is_paused: bool = False
        self.repeat_mode: RepeatMode = RepeatMode.NONE
        self.current_position: float = 0.0

    def load_track(self, track: MediaRow, playlist: list[MediaRow]) -> None:
        """Load a track and its playlist context.

        Finds the track's index in the playlist by ``media_id`` comparison.
        Resets ``current_position`` to 0.

        Args:
            track: The track to load.
            playlist: The ordered playlist containing ``track``.
        """
        self.current_track = track
        self.playlist = playlist
        self.current_position = 0.0
        for i, t in enumerate(playlist):
            if t.media_id == track.media_id:
                self.playlist_index = i
                return
        self.playlist_index = 0

    def next_track(self) -> MediaRow | None:
        """Advance to the next track according to repeat mode.

        Returns:
            The next ``MediaRow`` to play, or ``None`` if the playlist is
            exhausted and repeat is off.
        """
        if not self.playlist:
            return None

        if self.repeat_mode == RepeatMode.ONE:
            return self.current_track

        next_index = self.playlist_index + 1
        if next_index >= len(self.playlist):
            if self.repeat_mode == RepeatMode.ALL:
                next_index = 0
            else:
                return None

        self.playlist_index = next_index
        self.current_track = self.playlist[next_index]
        self.current_position = 0.0
        return self.current_track

    def previous_track(self) -> MediaRow | None:
        """Go back to the previous track, or restart current if past threshold.

        If ``current_position`` exceeds ``RESTART_THRESHOLD`` seconds, the
        current track is returned unchanged (restart semantics).  Otherwise
        the previous track in the playlist is returned, wrapping if repeat
        is ALL.

        Returns:
            The ``MediaRow`` to (re)play, or ``None`` if there is nothing
            to play.
        """
        if not self.playlist:
            return None

        if self.current_position > RESTART_THRESHOLD:
            self.current_position = 0.0
            return self.current_track

        prev_index = self.playlist_index - 1
        if prev_index < 0:
            if self.repeat_mode == RepeatMode.ALL:
                prev_index = len(self.playlist) - 1
            else:
                # Restart the first track.
                self.current_position = 0.0
                return self.current_track

        self.playlist_index = prev_index
        self.current_track = self.playlist[prev_index]
        self.current_position = 0.0
        return self.current_track

    def toggle_repeat(self) -> RepeatMode:
        """Cycle repeat mode: NONE → ALL → ONE → NONE.

        Returns:
            The new ``RepeatMode`` after toggling.
        """
        if self.repeat_mode == RepeatMode.NONE:
            self.repeat_mode = RepeatMode.ALL
        elif self.repeat_mode == RepeatMode.ALL:
            self.repeat_mode = RepeatMode.ONE
        else:
            self.repeat_mode = RepeatMode.NONE
        return self.repeat_mode
