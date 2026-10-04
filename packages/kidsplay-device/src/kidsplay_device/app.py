"""Main application loop and glue for the KidsPlay device player.

Startup sequence
----------------
1. Load ``DeviceConfig`` from disk.
2. ``pygame.init()`` — display, mixer, joystick.
3. Open local SQLite DB.
4. Start background sync thread (``SyncClient.run_sync_loop``).
5. Create views, enter HomeView, start 30-FPS main loop.

Button routing
--------------
An input profile (``input_profiles.py``, chosen in ``config.json``) turns keys,
joystick buttons and sticks into logical buttons. X is global: toggles
play/pause from any view. All other buttons are forwarded to the current view.

Screen size and themes
----------------------
The window is ``config.width`` × ``config.height`` and every size scales from
the 640×480 design (``layout.py``). The theme is the profile's, when it chose
one (fixed, delivered by sync), otherwise the child's own color pick; a theme
may bring a font, UI sounds and background images (``theme.py``).

Playback bar
------------
Drawn on every frame at the bottom of the screen (below ``Layout.view_h``).
Shows: small thumbnail • play/pause icon • track title • progress bar.

Parental controls
-----------------
The profile settings (``controls.py``) come from the last sync, persisted in
the local DB, so they apply offline and from the first frame after boot.

- Volume cap: every track load, settings change and volume button press
  sets ``pygame.mixer.music`` to the capped volume, and the UI sounds to
  0.50 of it. In-app volume buttons do nothing unless ``volume_buttons`` is
  on (in the profile, or by default in the input profile: hardware with no
  volume dial); the kid's level is remembered in ``settings.json``.
- Bedtime: checked once a second against ``TimeSource``. On entering it
  during playback the music fades out over ``FADE_SECONDS`` (the screen
  dims with it), then stops. ``sleep_screen`` shows ``SleepView`` and
  ignores every button; ``audiobooks_only`` allows only the home screen,
  audiobooks and audiobook playback. Leaving bedtime returns to home.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pygame

from kidsplay_models import BedtimeMode, ProfileSettings, ThemeDefinition

from .config import DEFAULT_CONFIG_PATH, DeviceConfig
from .controls import (
    DUCK_TAIL_SECONDS,
    HEARTBEAT_INTERVAL,
    NOT_BEDTIME,
    NTP_POLL_INTERVAL,
    BedtimeStatus,
    Fader,
    TimeSource,
    VolumeControl,
    bedtime_status,
    music_output_volume,
    read_boot_id,
    read_ntp_synchronized,
    run_ntp_watch,
)
from .database import (
    get_clock_heartbeat,
    get_last_server_time,
    get_profile_settings,
    get_theme_definition,
    init_db,
    set_clock_heartbeat,
)
from .fonts import load_text_fonts
from .i18n import _, activate, resolve_language
from .image_cache import ImageCache
from .input_profiles import PROFILES, InputMapper, resolve_profile
from .layout import Layout
from .player import PlaybackState
from .sync import SyncClient
from .theme import DEFAULT_THEME, THEMES_BY_NAME, Theme, resolve_profile_theme
from .views import (
    AudiobooksView,
    Button,
    HomeView,
    MusicView,
    PhotosView,
    PlayView,
    SettingsView,
    SleepView,
    UiSound,
    View,
    apply_layout,
    apply_theme,
    px,
    scale_cover,
    scale_fit,
    set_backgrounds,
    truncate_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

logger = logging.getLogger(__name__)

# Custom pygame event fired when mixer finishes a track.
_MUSIC_ENDED: int = 0  # assigned after pygame.init()

# Views reachable during an ``audiobooks_only`` bedtime.
_AUDIOBOOK_VIEWS: frozenset[str] = frozenset({"home", "audiobooks", "play"})

# How often bedtime is re-evaluated, in seconds.
_BEDTIME_CHECK_INTERVAL: float = 1.0

# How long the volume overlay stays up after a volume button press.
_VOLUME_OVERLAY_SECONDS: float = 1.5


#: How much of the theme's background color veils a background image, 0-255,
#: so text stays readable whatever picture a family chooses.
_BACKGROUND_VEIL: int = 140


class MusicPlayerApp:
    """Top-level application controller.

    Args:
        config: Device configuration loaded from disk.
        time_source: Clock for bedtime; built from the local DB by
            :meth:`initialize` if None. Tests pass a fake clock.
        mono: Monotonic clock driving the fade and the bedtime check
            interval.
    """

    #: Path to the user-facing settings file (persists theme choice etc.)
    _SETTINGS_PATH: Path = Path.home() / ".kidsplay" / "settings.json"

    def __init__(
        self,
        config: DeviceConfig,
        *,
        time_source: TimeSource | None = None,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self.db_path = config.db_path
        self.playback = PlaybackState()
        self._cache: ImageCache | None = None
        self.fonts: dict[str, pygame.font.Font] = {}
        self.layout = Layout.for_size(config.width, config.height)
        self._input = InputMapper(
            resolve_profile(config.input_profile, config.input_overrides)
        )
        # The theme on screen; it is the profile's when that chose one, else
        # the child's own pick (``_local_theme``, persisted in settings.json).
        self._theme: Theme = DEFAULT_THEME
        self._local_theme: Theme = DEFAULT_THEME
        self._synced_theme: ThemeDefinition | None = None
        self._theme_font_path: Path | None = None
        self._sounds: dict[UiSound, pygame.mixer.Sound] = {}

        # Created by initialize(); None until then.
        self._screen: pygame.Surface | None = None
        self._clock: pygame.time.Clock | None = None
        self._views: dict[str, View] = {}
        self._current_view: str = "home"
        self._running: bool = False
        self._ntp_stop = threading.Event()
        self._joysticks: list[pygame.joystick.JoystickType] = []

        # Parental controls.
        self._mono = mono
        self._time_source = time_source
        self.volume = VolumeControl(ProfileSettings())
        self._pending_settings: ProfileSettings | None = None
        # A one-tuple, so "no theme" (None) is distinguishable from "nothing
        # pending".
        self._pending_theme: tuple[ThemeDefinition | None] | None = None
        # True while the server answering is not the one this player paired
        # with (set by the sync thread; a plain bool, read by the home screen).
        self.identity_warning: bool = False
        self._bedtime: BedtimeStatus = NOT_BEDTIME
        self._next_bedtime_check: float = float("-inf")
        self._bedtime_error_logged = False
        self._next_heartbeat: float = float("-inf")
        self._fader: Fader | None = None
        self._after_fade: str | None = None
        # Monotonic time until which the music is ducked for a UI sound.
        self._duck_until: float = float("-inf")
        self._ducked: bool = False
        self._volume_overlay_until: float = float("-inf")

    @property
    def cache(self) -> ImageCache:
        """The shared image cache.

        Raises:
            RuntimeError: If :meth:`initialize` has not been called yet.
        """
        if self._cache is None:
            raise RuntimeError("MusicPlayerApp.initialize() has not been called")
        return self._cache

    def _require_screen(self) -> pygame.Surface:
        if self._screen is None:
            raise RuntimeError("MusicPlayerApp.initialize() has not been called")
        return self._screen

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Initialise pygame and all subsystems."""
        global _MUSIC_ENDED

        # Database
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = init_db(self.db_path)
        try:
            # Settings and clock floor from the last sync: in force before
            # (and without) any network.
            synced_settings = get_profile_settings(conn)
            self._synced_theme = get_theme_definition(conn)
            last_server_time = get_last_server_time(conn)
            heartbeat = get_clock_heartbeat(conn)
        finally:
            conn.close()
        self.volume.settings = self._effective_settings(synced_settings)
        activate(resolve_language(self._config.language, synced_settings.language))
        if self._time_source is None:
            self._time_source = TimeSource(
                last_server_time, heartbeat=heartbeat, boot_id=read_boot_id()
            )

        # Pygame
        pygame.init()
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

        _MUSIC_ENDED = pygame.USEREVENT + 1
        pygame.mixer.music.set_endevent(_MUSIC_ENDED)

        apply_layout(self.layout)
        flags = pygame.FULLSCREEN if self._config.fullscreen else 0
        self._screen = pygame.display.set_mode(
            (self.layout.width, self.layout.height), flags
        )
        pygame.display.set_caption("KidsPlay")
        pygame.mouse.set_visible(False)
        self._clock = pygame.time.Clock()

        # Joystick — keep references alive so SDL doesn't close the devices
        self._joysticks = [
            pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())
        ]

        # Fonts (pygame's default font -- covers the Spanish accents; see
        # fonts.py). A theme may replace the text font, in _show_theme().
        self.fonts.update(load_text_fonts(self.layout.scale))

        # Font Awesome Free Solid — used for home screen icons.
        # See assets/README.txt for how to obtain the TTF/OTF file.
        _fa_candidates = [
            Path(__file__).parent / "assets" / "fa-solid-900.ttf",
            self._config.db_path.parent / "fa-solid-900.ttf",
            # Raspberry Pi OS: apt-get install fonts-font-awesome
            Path("/usr/share/fonts/truetype/font-awesome/fontawesome-webfont.ttf"),
        ]
        for _fa_path in _fa_candidates:
            if _fa_path.exists():
                self.fonts["icon"] = pygame.font.Font(str(_fa_path), px(72))
                self.fonts["icon_sm"] = pygame.font.Font(str(_fa_path), px(36))
                break

        # Image cache — pass media_root so relative paths from the DB resolve
        self._cache = ImageCache(max_size=64, media_root=self._config.media_root)

        # Load persisted settings, then show the theme (which also loads the
        # UI sounds and backgrounds) before views are first drawn.
        self._load_settings()
        self._refresh_theme()
        self._apply_volume()

        # Views
        self._views = {
            "home": HomeView(self),
            "music": MusicView(self),
            "audiobooks": AudiobooksView(self),
            "photos": PhotosView(self),
            "play": PlayView(self),
            "settings": SettingsView(self),
            "sleep": SleepView(self),
        }

        # Start sync background thread
        sync_client = SyncClient(
            self._config,
            on_settings=self._on_synced_settings,
            on_server_time=self._require_time_source().note_server_time,
            on_theme=self._on_synced_theme,
            on_identity=self._on_identity_problem,
        )
        t = threading.Thread(target=sync_client.run_sync_loop, daemon=True)
        t.start()
        # NTP also confirms the clock, which is what does it in all-in-one
        # mode (a sync with the local server cannot).
        threading.Thread(
            target=run_ntp_watch,
            args=(self._require_time_source(),),
            kwargs={
                "probe": read_ntp_synchronized,
                "interval": NTP_POLL_INTERVAL,
                "stop": self._ntp_stop,
            },
            daemon=True,
        ).start()

        # Enter home, then straight into bedtime if it already is.
        self._get_view("home").on_enter()
        self._tick_controls()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the 30-FPS main loop.  Returns when the user quits."""
        clock = self._clock
        if clock is None:
            raise RuntimeError("MusicPlayerApp.initialize() has not been called")
        self._running = True
        while self._running:
            clock.tick(30)
            self._process_events()
            self._tick_controls()
            self._update_position()
            self._get_view(self._current_view).update()
            self._render()
        self._shutdown()

    def run_frame(self, position: float | None = None) -> None:
        """Handle pending input, then draw one frame, with no clock.

        A hook for drivers that step the player themselves instead of calling
        :meth:`run`: the screenshot tool posts key presses and captures the
        screen after each. It does exactly what :meth:`run` does for input and
        drawing, and none of the timing: no frame limiter, no parental-controls
        tick, no polling of the mixer's position.

        Args:
            position: If given, playback position in seconds to show in this
                frame (the mixer's real position is not polled).

        Raises:
            RuntimeError: If :meth:`initialize` has not been called.
        """
        self._require_screen()
        self._process_events()
        if position is not None:
            self.playback.current_position = position
        self._render()

    # ------------------------------------------------------------------
    # Public API for views
    # ------------------------------------------------------------------

    def switch_view(self, view_name: str) -> None:
        """Switch to a named view, calling on_enter() on the destination.

        Args:
            view_name: One of ``'home'``, ``'music'``, ``'audiobooks'``,
                ``'photos'``, ``'play'``, ``'settings'``.
        """
        if view_name not in self._views:
            logger.warning("Unknown view: %s", view_name)
            return
        if not self.view_allowed(view_name):
            logger.info("View %s is locked during bedtime", view_name)
            return
        self._current_view = view_name
        self._get_view(view_name).on_enter()

    def play_track(
        self,
        track: object,
        playlist: list,
        switch_to_play: bool = True,
        return_view: str | None = None,
    ) -> None:
        """Load and start playing a track.

        Args:
            track: A ``MediaRow`` to play.
            playlist: The full playlist list (list of ``MediaRow``).
            switch_to_play: If True, switch to PlayView after loading.
            return_view: View name that PlayView's B button returns to.
                Defaults to the current view.
        """
        from .database import MediaRow as MR

        assert isinstance(track, MR)
        if not self._track_allowed(track):
            logger.info("Not playing %s during bedtime", track.media_id)
            return
        audio_path = track.audio_path
        if not audio_path:
            logger.warning("Track %s has no audio_path", track.media_id)
            return

        full = Path(audio_path)
        if not full.is_absolute():
            full = self._config.media_root / audio_path
        if not full.exists():
            logger.warning("Audio file missing: %s", full)
            return

        try:
            pygame.mixer.music.load(str(full))
            pygame.mixer.music.play()
            # Every track load: the cap applies to each new stream.
            self._update_music_volume()
        except pygame.error as exc:
            logger.error("Mixer error: %s", exc)
            return

        self.playback.load_track(track, list(playlist))
        self.playback.is_playing = True
        self.playback.is_paused = False

        if switch_to_play:
            rv = return_view or self._current_view
            play_view = self._get_view("play")
            assert isinstance(play_view, PlayView)
            play_view.on_enter(return_view=rv)
            self._current_view = "play"

    def toggle_playback(self) -> None:
        """Toggle play / pause for the current track."""
        track = self.playback.current_track
        if track is not None and not self._track_allowed(track):
            return
        if self.playback.is_playing:
            if self.playback.is_paused:
                pygame.mixer.music.unpause()
                self.playback.is_paused = False
            else:
                pygame.mixer.music.pause()
                self.playback.is_paused = True

    def view_allowed(self, view_name: str) -> bool:
        """Whether a view may be entered under the current bedtime.

        Args:
            view_name: A view name as passed to :meth:`switch_view`.

        Returns:
            False for views locked by bedtime.
        """
        mode = self._bedtime.mode
        if mode is BedtimeMode.SLEEP_SCREEN:
            return view_name == "sleep"
        if mode is BedtimeMode.AUDIOBOOKS_ONLY:
            return view_name in _AUDIOBOOK_VIEWS
        return view_name != "sleep"

    @property
    def bedtime_wake_at(self) -> datetime | None:
        """Local time the current bedtime ends; None outside bedtime."""
        return self._bedtime.wake_at if self._bedtime.active else None

    def change_volume(self, direction: int) -> bool:
        """Apply an in-app volume button press.

        Does nothing unless the profile enables ``volume_buttons``; never
        exceeds the cap. A press that cannot change the volume (at the cap, or
        at zero) still shows the volume overlay.

        Args:
            direction: ``+1`` for up, ``-1`` for down.

        Returns:
            True if the volume changed.
        """
        if not self.volume.settings.volume_buttons:
            return False
        changed = self.volume.step(direction)
        # Shown for every press, also at the cap (or at zero): the bar then
        # stays full at the cap mark, so the child sees it is the limit.
        self._volume_overlay_until = self._mono() + _VOLUME_OVERLAY_SECONDS
        if changed:
            self._apply_volume()
            self._save_settings()
        return changed

    @property
    def theme_locked(self) -> bool:
        """Whether the child's profile chose the theme (the picker is off)."""
        return self.volume.settings.theme is not None

    def set_theme(self, theme: Theme) -> None:
        """Switch to a new color theme and persist the choice.

        With a theme chosen by the profile the choice is remembered but the
        screen keeps the profile's theme.

        Args:
            theme: The ``Theme`` to activate.
        """
        self._local_theme = theme
        self._refresh_theme()
        self._save_settings()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        """Load persisted settings (e.g. theme) from disk if the file exists."""
        try:
            data: dict = json.loads(self._SETTINGS_PATH.read_text())
            theme_name: str = data.get("theme_name", DEFAULT_THEME.name)
            self._local_theme = THEMES_BY_NAME.get(theme_name, DEFAULT_THEME)
            level = data.get("volume_level")
            if isinstance(level, int):
                self.volume = VolumeControl(self.volume.settings, level)
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load settings: %s", exc)

    def _save_settings(self) -> None:
        """Persist current settings to disk."""
        try:
            self._SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "theme_name": self._local_theme.name,
                "volume_level": self.volume.level,
            }
            self._SETTINGS_PATH.write_text(json.dumps(payload, indent=2))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save settings: %s", exc)

    def _refresh_theme(self) -> None:
        """Show the profile's theme if it chose one, else the child's own."""
        profile_theme = resolve_profile_theme(
            self.volume.settings.theme,
            self._synced_theme,
            self._config.media_root,
        )
        self._show_theme(profile_theme or self._local_theme)

    def _show_theme(self, theme: Theme) -> None:
        """Make *theme* the one on screen: colors, font, sounds, backgrounds.

        Assets are best effort: any that will not load leave the built-in
        font, sound or plain background in place, so a broken theme never
        stops the player.

        Args:
            theme: The theme to show.
        """
        self._theme = theme
        apply_theme(theme)
        font_path = theme.assets.font
        if font_path != self._theme_font_path:
            self.fonts.update(load_text_fonts(self.layout.scale, font_path))
            self._theme_font_path = font_path
        self._load_sounds(theme)
        self._load_backgrounds(theme)

    def _load_sounds(self, theme: Theme) -> None:
        """Load the UI sounds: the theme's, else the bundled ones."""
        sounds_dir = Path(__file__).parent / "assets" / "ui-sounds"
        names = {
            UiSound.SELECT: "select",
            UiSound.BACK: "back",
            UiSound.MOVE: "move",
            UiSound.OPEN: "open",
        }
        for ui_sound, name in names.items():
            bundled = sounds_dir / f"{name}.ogg"
            themed = theme.assets.sounds.get(name)
            for path in (themed, bundled) if themed else (bundled,):
                try:
                    self._sounds[ui_sound] = pygame.mixer.Sound(str(path))
                    break
                except (pygame.error, FileNotFoundError) as exc:
                    logger.warning("Could not load sound %s: %s", path, exc)
        self._apply_volume()

    def _load_backgrounds(self, theme: Theme) -> None:
        """Scale the theme's background images to the screen, veiled."""
        set_backgrounds(
            self._prepare_background(theme, theme.assets.background),
            self._prepare_background(theme, theme.assets.home_background),
        )

    def _prepare_background(
        self, theme: Theme, path: Path | None
    ) -> pygame.Surface | None:
        if path is None:
            return None
        img = self.cache.get(str(path))
        if img is None:
            logger.warning("Could not load theme background %s", path)
            return None
        try:
            size = (self.layout.width, self.layout.height)
            covered = scale_cover(img, *size)
            veil = pygame.Surface(size)
            veil.fill(theme.BG)
            veil.set_alpha(_BACKGROUND_VEIL)
            covered.blit(veil, (0, 0))
            return covered
        except (pygame.error, ValueError) as exc:
            logger.warning("Could not prepare theme background %s: %s", path, exc)
            return None

    def _play_ui_sound(self, sound: UiSound) -> None:
        """Play a UI sound effect without interrupting music playback.

        Args:
            sound: The ``UiSound`` variant to play.
        """
        if not self.volume.settings.ui_sounds:
            # Off: no clip, and so nothing to duck the music under.
            return
        clip = self._sounds.get(sound)
        if clip:
            clip.play()
            # Duck the music for the clip's length so music + beep stay within
            # the cap; an overlapping beep only extends the duck.
            end = self._mono() + clip.get_length() + DUCK_TAIL_SECONDS
            self._duck_until = max(self._duck_until, end)
            self._ducked = True
            self._update_music_volume()

    def _get_view(self, name: str) -> View:
        return self._views[name]

    def _require_time_source(self) -> TimeSource:
        if self._time_source is None:
            raise RuntimeError("MusicPlayerApp.initialize() has not been called")
        return self._time_source

    # ------------------------------------------------------------------
    # Parental controls
    # ------------------------------------------------------------------

    def _on_synced_settings(self, settings: ProfileSettings) -> None:
        """Receive settings from the sync thread; applied on the UI thread."""
        self._pending_settings = settings

    def _on_identity_problem(self, bad: bool) -> None:
        """Receive the sync thread's verdict on the server's identity."""
        self.identity_warning = bad

    def _on_synced_theme(self, theme: ThemeDefinition | None) -> None:
        """Receive the profile's theme from the sync thread (UI thread applies)."""
        self._pending_theme = (theme,)

    def _effective_settings(self, settings: ProfileSettings) -> ProfileSettings:
        """The settings in force: the input profile is the volume-key default.

        A parent's explicit choice (on or off) wins. Where they have not made
        one, hardware with no volume dial (the ``keyboard`` profile) has the
        in-app volume buttons on and the rest have them off. The cap bounds
        them either way.
        """
        if settings.volume_buttons is None:
            return settings.model_copy(
                update={"volume_buttons": self._input.profile.volume_buttons}
            )
        return settings

    def _apply_volume(self) -> None:
        """Set the capped volume on the music stream and the UI sounds."""
        self._update_music_volume()
        ui = self.volume.ui_volume()
        for clip in self._sounds.values():
            clip.set_volume(ui)

    def _ui_sound_playing(self) -> bool:
        """Whether the mixer is still playing any UI sound.

        The duck's end time is only an estimate made when the sound started: if
        the audio thread falls behind (a busy CPU), the clip is still being
        mixed after it. Until the mixer has finished it, the music stays ducked.
        """
        return any(clip.get_num_channels() > 0 for clip in self._sounds.values())

    def _duck_over(self, now: float) -> bool:
        """Whether a duck in progress can end: its time is up and no UI sound
        is still being mixed."""
        return now >= self._duck_until and not self._ui_sound_playing()

    def _update_music_volume(self) -> None:
        """Set the music stream's volume from the cap, fade and UI-sound duck."""
        now = self._mono()
        if self._ducked and self._duck_over(now):
            self._ducked = False
        fade = None if self._fader is None else self._fader.volume_at(now)
        pygame.mixer.music.set_volume(
            music_output_volume(self.volume.music_volume(), fade, self._ducked)
        )

    def _track_allowed(self, track: object) -> bool:
        from .database import MediaRow as MR

        mode = self._bedtime.mode
        if mode is BedtimeMode.SLEEP_SCREEN:
            return False
        if mode is BedtimeMode.AUDIOBOOKS_ONLY:
            return isinstance(track, MR) and track.media_type == "audiobook"
        return True

    def _tick_controls(self) -> None:
        """Per-frame parental controls: new settings, bedtime, fade."""
        now = self._mono()
        pending = self._pending_settings
        pending_theme = self._pending_theme
        if pending is not None:
            self._pending_settings = None
            self.volume.settings = self._effective_settings(pending)
            activate(resolve_language(self._config.language, pending.language))
            self._apply_volume()
            logger.info("Applied synced profile settings: %s", pending)
            self._next_bedtime_check = float("-inf")
            self._bedtime_error_logged = False
        if pending_theme is not None:
            self._pending_theme = None
            self._synced_theme = pending_theme[0]
        if pending is not None or pending_theme is not None:
            self._refresh_theme()

        if now >= self._next_heartbeat:
            self._next_heartbeat = now + HEARTBEAT_INTERVAL
            self._write_heartbeat()

        if now >= self._next_bedtime_check:
            self._next_bedtime_check = now + _BEDTIME_CHECK_INTERVAL
            try:
                status = bedtime_status(
                    self.volume.settings, self._require_time_source().now()
                )
            except Exception:
                # A bad persisted setting must never stop the player: log it
                # once (not every check) and fail open.
                if not self._bedtime_error_logged:
                    logger.exception(
                        "Bedtime evaluation failed; bedtime is not enforced"
                    )
                    self._bedtime_error_logged = True
                status = NOT_BEDTIME
            if status.mode is not self._bedtime.mode:
                self._change_bedtime(status, now)
            else:
                self._bedtime = status

        fader = self._fader
        if self._ducked and self._duck_over(now):
            # The UI sound is over: restore the music.
            self._update_music_volume()
        if fader is not None:
            # Clamped to the cap: a sync may lower it mid-fade.
            self._update_music_volume()
            if fader.done(now):
                self._finish_fade()

    def _write_heartbeat(self) -> None:
        """Record the wall clock so a restored clock can be spotted at boot."""
        try:
            heartbeat = self._require_time_source().heartbeat()
            conn = init_db(self.db_path)
            try:
                set_clock_heartbeat(conn, heartbeat)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("Could not write the clock heartbeat", exc_info=True)

    def _change_bedtime(self, status: BedtimeStatus, now: float) -> None:
        """Enter, leave or switch bedtime mode."""
        previous = self._bedtime
        self._bedtime = status
        logger.info(
            "Bedtime mode %s -> %s (wake at %s)",
            previous.mode.value,
            status.mode.value,
            status.wake_at,
        )
        if not status.active:
            # Leaving bedtime: undo any fade and go home.
            self._fader = None
            self._after_fade = None
            self._apply_volume()
            if self._current_view == "sleep" or not self.view_allowed(
                self._current_view
            ):
                self._current_view = "home"
                self._get_view("home").on_enter()
            return

        target = "sleep" if status.mode is BedtimeMode.SLEEP_SCREEN else "audiobooks"
        pb = self.playback
        track = pb.current_track
        audible = pb.is_playing and not pb.is_paused
        if track is not None and self._track_allowed(track):
            # An audiobook may keep playing in audiobooks_only mode.
            if not self.view_allowed(self._current_view):
                self._enter_view(target)
            return
        if audible:
            if self._fader is None:
                self._fader = Fader(self.volume.music_volume(), now)
            self._after_fade = target
            return
        self._stop_playback()
        self._enter_view(target)

    def _finish_fade(self) -> None:
        """End of the bedtime fade: stop playback and show the target view."""
        self._fader = None
        target = self._after_fade
        self._after_fade = None
        self._stop_playback()
        self._update_music_volume()
        if target is not None:
            self._enter_view(target)

    def _stop_playback(self) -> None:
        """Stop the track bedtime does not allow, and drop it from the bar."""
        if self.playback.is_playing:
            pygame.mixer.music.stop()
        self.playback.is_playing = False
        self.playback.is_paused = False
        # Showing it would suggest it can be played.
        self.playback.current_track = None
        self.playback.playlist = []
        self.playback.current_position = 0.0

    def show_sleep_screen(self, wake_at: datetime | None = None) -> None:
        """Show the bedtime sleep screen, as the bedtime logic would.

        The way in for tests and screenshot tooling: ``switch_view("sleep")``
        is refused outside bedtime (only the bedtime check may enter it), and
        setting ``_current_view`` by hand skips the bedtime state the view
        reads. This records a sleep-screen bedtime ending at ``wake_at`` and
        enters the view. It does not touch playback, and the next periodic
        bedtime check in :meth:`update` ends it again unless the clock really
        says it is bedtime, so it is not for production code.

        Args:
            wake_at: Local wall-clock wake time to display, or None for none.
        """
        self._bedtime = BedtimeStatus(BedtimeMode.SLEEP_SCREEN, wake_at)
        self._enter_view("sleep")

    def _enter_view(self, view_name: str) -> None:
        self._current_view = view_name
        self._get_view(view_name).on_enter()

    def _process_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._running = False
                return

            if event.type == _MUSIC_ENDED:
                self._handle_track_ended()
                continue

            button = self._map_event(event)
            if button is not None and (
                self._fader is not None or self._current_view == "sleep"
            ):
                # Bedtime is starting or in force: buttons do nothing, not even
                # the keyboard's Escape (closing the window still quits).
                continue
            if button is Button.TERMINATE:
                self._running = False
                return
            if button in (Button.VOLUME_UP, Button.VOLUME_DOWN):
                if self.change_volume(1 if button is Button.VOLUME_UP else -1):
                    self._play_ui_sound(UiSound.MOVE)
            elif button is Button.PLAYPAUSE:
                self.toggle_playback()
                self._play_ui_sound(UiSound.SELECT)
            elif button is Button.REPEAT:
                self.playback.toggle_repeat()
                self._play_ui_sound(UiSound.SELECT)
            elif button is not None:
                sound = self._get_view(self._current_view).handle_input(button)
                if isinstance(sound, UiSound):
                    self._play_ui_sound(sound)

    def _map_event(self, event: pygame.event.Event) -> Button | None:
        return self._input.map_event(event)

    def _handle_track_ended(self) -> None:
        track = self.playback.next_track()
        if track:
            self.play_track(track, self.playback.playlist, switch_to_play=False)
        else:
            self.playback.is_playing = False
            self.playback.is_paused = False

    def _render(self) -> None:
        bg = self._theme.BG
        screen = self._require_screen()
        if self._current_view == "sleep":
            # Full-screen and dim: no playback bar at bedtime.
            self._get_view("sleep").draw(screen)
            pygame.display.flip()
            return
        screen.fill(bg)

        # Current view draws into the upper area.
        lay = self.layout
        view_surf = pygame.Surface((lay.width, lay.view_h))
        view_surf.fill(bg)
        self._get_view(self._current_view).draw(view_surf)
        screen.blit(view_surf, (0, 0))

        now = self._mono()
        volume_up = self._fader is None and now < self._volume_overlay_until

        # Playback bar at the bottom (the volume overlay takes its place).
        if volume_up:
            self._draw_volume_overlay()
        else:
            self._draw_playback_bar()

        if self._fader is not None:
            # The screen dims as the sound fades out.
            shade = pygame.Surface((lay.width, lay.height))
            shade.set_alpha(int(220 * self._fader.progress(now)))
            screen.blit(shade, (0, 0))

        pygame.display.flip()

    def _volume_overlay_rect(self) -> pygame.Rect:
        """Where the volume overlay is drawn: over the playback bar.

        Any box floating over the view would cover something the child is
        reading (the artist line and seek bar on the play screen, list rows
        elsewhere), and at 240×180 there is no room to dodge. The playback bar
        is the one strip that only repeats what the view shows, so the overlay
        takes its place for the moment it is up.
        """
        lay = self.layout
        return pygame.Rect(0, lay.view_h, lay.width, lay.bar_h)

    def _draw_volume_overlay(self) -> None:
        """Show the in-app volume level (and the cap) after a button press."""
        screen = self._require_screen()
        lay = self.layout
        t = self._theme
        box = self._volume_overlay_rect()
        pygame.draw.rect(screen, t.SURFACE, box)
        pygame.draw.line(screen, t.PRIMARY, box.topleft, box.topright, width=lay.px(2))
        label = self.fonts["small"].render(
            _("Volume {percent}%").format(percent=self.volume.effective_percent()),
            True,
            t.TEXT_BRIGHT,
        )
        pad = lay.px(16)
        screen.blit(label, (pad, box.centery - label.get_height() // 2))
        inner_x = pad + label.get_width() + pad
        inner_w = lay.width - pad - inner_x
        bar_h = max(lay.px(8), 4)
        bar_y = box.centery - bar_h // 2
        pygame.draw.rect(screen, t.PROGRESS_BG, (inner_x, bar_y, inner_w, bar_h))
        cap_w = inner_w * self.volume.cap // 100
        level_w = inner_w * self.volume.effective_percent() // 100
        pygame.draw.rect(screen, t.PROGRESS_FG, (inner_x, bar_y, level_w, bar_h))
        tick = max(lay.px(4), 2)
        pygame.draw.line(
            screen,
            t.ACCENT,
            (inner_x + cap_w, bar_y - tick),
            (inner_x + cap_w, bar_y + bar_h + tick),
        )

    def _draw_playback_bar(self) -> None:
        """Draw the always-visible playback bar at the bottom of the screen."""
        screen = self._require_screen()
        lay = self.layout
        t = self._theme
        bar_rect = pygame.Rect(0, lay.view_h, lay.width, lay.bar_h)
        pygame.draw.rect(screen, t.SURFACE, bar_rect)

        pb = self.playback
        x = lay.px(4)

        # Thumbnail — height fills bar, width is natural (16:9 friendly).
        thumb_h = lay.bar_h - lay.px(8)
        thumb_slot_w = thumb_h * 16 // 9  # reserve slot sized for 16:9
        if pb.current_track:
            track = pb.current_track
            thumb = track.thumbnail_small_path or track.thumbnail_medium_path
            if thumb:
                img = self.cache.get(thumb)
                if img:
                    scaled = scale_fit(img, thumb_slot_w, thumb_h)
                    sw, sh = scaled.get_size()
                    tx = x + (thumb_slot_w - sw) // 2
                    ty = lay.view_h + (lay.bar_h - sh) // 2
                    screen.blit(scaled, (tx, ty))
        x += thumb_slot_w + lay.px(6)

        # Play/pause icon — use Font Awesome if loaded, else ASCII fallback
        playing = pb.is_playing and not pb.is_paused
        if "icon_sm" in self.fonts:
            icon_font = self.fonts["icon_sm"]
            icon = "\uf04c" if playing else "\uf04b"  # fa-pause / fa-play
        else:
            icon_font = self.fonts["medium"]
            icon = "II" if playing else ">"
        icon_surf = icon_font.render(icon, True, t.PRIMARY)
        iy = lay.view_h + (lay.bar_h - icon_surf.get_height()) // 2
        screen.blit(icon_surf, (x, iy))
        x += icon_surf.get_width() + lay.px(6)

        # Repeat icon — fixed to the right edge, drawn before title so title
        # can reserve space for it.
        from .player import RepeatMode

        repeat_w = 0
        if pb.repeat_mode != RepeatMode.NONE and "icon_sm" in self.fonts:
            icon_font_r = self.fonts["icon_sm"]
            # arrow-rotate-right (ONE) / arrows-spin (ALL)
            r_glyph = "\uf01e" if pb.repeat_mode == RepeatMode.ONE else "\ue4bb"
            r_surf = icon_font_r.render(r_glyph, True, t.PRIMARY)
            repeat_w = r_surf.get_width() + lay.px(8)
            rx = lay.width - repeat_w + lay.px(4)
            ry = lay.view_h + (lay.bar_h - r_surf.get_height()) // 2
            screen.blit(r_surf, (rx, ry))

        # Track title
        if pb.current_track:
            title_font = self.fonts["small"]
            max_title_w = lay.width - x - repeat_w - lay.px(4)
            title = truncate_text(title_font, pb.current_track.title, max_title_w)
            ts = title_font.render(title, True, t.TEXT_BRIGHT)
            ty = lay.view_h + (lay.bar_h - ts.get_height()) // 2
            screen.blit(ts, (x, ty))

        # Thin progress bar at very bottom of bar
        if pb.current_track and pb.current_track.duration_seconds:
            duration = pb.current_track.duration_seconds
            progress = min(1.0, pb.current_position / duration)
            prog_h = lay.px(4)
            prog_y = lay.view_h + lay.bar_h - prog_h
            pygame.draw.rect(screen, t.PROGRESS_BG, (0, prog_y, lay.width, prog_h))
            if progress > 0:
                pygame.draw.rect(
                    screen,
                    t.PROGRESS_FG,
                    (0, prog_y, int(lay.width * progress), prog_h),
                )

    def _update_position(self) -> None:
        """Sync playback position from pygame mixer."""
        if self.playback.is_playing and not self.playback.is_paused:
            try:
                pos_ms = pygame.mixer.music.get_pos()
                if pos_ms >= 0:
                    self.playback.current_position = pos_ms / 1000.0
            except pygame.error:
                pass

    def _shutdown(self) -> None:
        self._ntp_stop.set()
        self._write_heartbeat()
        pygame.mixer.music.stop()
        pygame.quit()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the player's command line.

    Args:
        argv: Arguments without the program name; ``sys.argv`` when None.

    Returns:
        The parsed options; each is None when not given.
    """
    parser = argparse.ArgumentParser(
        prog="kidsplay-player",
        description="KidsPlay device player. Options override config.json "
        "for this run.",
    )
    parser.add_argument("--width", type=int, help="window width in pixels")
    parser.add_argument("--height", type=int, help="window height in pixels")
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        default=None,
        help="fill the screen, with no title bar (the kiosk always passes this), "
        "also on the pairing screen",
    )
    parser.add_argument(
        "--input-profile",
        help="input profile: " + ", ".join(sorted(PROFILES)),
    )
    parser.add_argument(
        "--pair-server",
        metavar="URL",
        help="server to pair with when there is no config.json, instead of "
        "choosing or typing one on the device",
    )
    args = parser.parse_args(argv)
    if args.pair_server is not None:
        from .pairing import normalize_server_url

        url = normalize_server_url(args.pair_server)
        if url is None:
            parser.error(f"--pair-server: not a server address: {args.pair_server!r}")
        args.pair_server = url
    return args


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``kidsplay-player`` CLI script.

    Args:
        argv: Command-line arguments; ``sys.argv`` when None.
    """
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        config = DeviceConfig.load(DEFAULT_CONFIG_PATH)
    except FileNotFoundError:
        # A device with no config pairs itself with a server: it shows a code
        # (and QR code) for a parent to approve, then writes config.json. A
        # config that exists but is unreadable is NOT replaced; that fails
        # loudly, below.
        from .pairing import (
            BOOT_PAIR_SERVER_FILE,
            PAIR_SERVER_FILENAME,
            preset_pair_server,
        )
        from .pairing_app import run_pairing

        logger.info("No config.json: starting pairing")
        # A preset server skips the picker and the on-screen keyboard: the
        # device goes straight to showing its code (B still goes back).
        start_url = args.pair_server or preset_pair_server(
            (DEFAULT_CONFIG_PATH.parent / PAIR_SERVER_FILENAME, BOOT_PAIR_SERVER_FILE)
        )
        # No config yet, so the command-line size and input profile are all
        # there is to go on; the overrides below apply to the paired config too.
        paired = run_pairing(
            DEFAULT_CONFIG_PATH,
            width=args.width,
            height=args.height,
            input_profile=args.input_profile,
            start_url=start_url,
            fullscreen=args.fullscreen,
        )
        if paired is None:
            return
        config = paired
    overrides = {
        "width": args.width,
        "height": args.height,
        "input_profile": args.input_profile,
        "fullscreen": args.fullscreen,
    }
    config = replace(config, **{k: v for k, v in overrides.items() if v is not None})
    app = MusicPlayerApp(config)
    app.initialize()
    app.run()
