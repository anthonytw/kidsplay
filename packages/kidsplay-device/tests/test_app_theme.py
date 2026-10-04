"""Themes, input profiles and hardware config in the running player.

The real ``MusicPlayerApp`` runs headless. Sync is replaced by direct calls to
the callbacks the sync thread would make, so no network is involved.
"""

import logging
from collections.abc import Callable
from pathlib import Path

import pygame
import pytest
from PIL import Image

from kidsplay_device import app as app_module
from kidsplay_device import views
from kidsplay_device.app import MusicPlayerApp
from kidsplay_device.config import DeviceConfig
from kidsplay_device.database import init_db, set_profile_settings, set_theme_definition
from kidsplay_device.theme import DEFAULT_THEME
from kidsplay_device.views import Button, UiSound
from kidsplay_models import (
    BUILTIN_THEMES_BY_ID,
    ProfileSettings,
    ThemeAsset,
    ThemeAssetRole,
    ThemeDefinition,
)

from . import scenes
from .scenes import press

FONT = Path(__file__).parents[1] / "src/kidsplay_device/assets/fa-solid-900.ttf"
RED = (200, 30, 30)

pytestmark = pytest.mark.usefixtures("headless")


def asset(media: Path, role: ThemeAssetRole, data: bytes, name: str) -> ThemeAsset:
    rel = f"themes/aa/{name}"
    (media / rel).parent.mkdir(parents=True, exist_ok=True)
    (media / rel).write_bytes(data)
    return ThemeAsset(
        role=role, content_hash="a" * 64, relative_path=rel, size_bytes=len(data)
    )


def image_bytes(tmp_path: Path, colour: tuple[int, int, int]) -> bytes:
    path = tmp_path / "img.webp"
    Image.new("RGB", (100, 50), colour).save(path, "WEBP", lossless=True)
    return path.read_bytes()


def wav_bytes(tmp_path: Path) -> bytes:
    scenes._silence(tmp_path / "s.wav")
    return (tmp_path / "s.wav").read_bytes()


def custom(assets: list[ThemeAsset]) -> ThemeDefinition:
    return BUILTIN_THEMES_BY_ID["night"].model_copy(
        update={"id": "mine", "name": "Mine", "builtin": False, "assets": assets}
    )


def deliver(
    app: MusicPlayerApp, settings: ProfileSettings, theme: ThemeDefinition | None
) -> None:
    """What the sync thread does, then what the UI thread does next frame."""
    app._on_synced_settings(settings)
    app._on_synced_theme(theme)
    app._tick_controls()


def new_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    size: tuple[int, int] | None = None,
    input_profile: str = "gpi2",
    input_overrides: dict[str, object] | None = None,
    before_init: Callable[[DeviceConfig], None] | None = None,
) -> MusicPlayerApp:
    """An empty-library player whose stored settings are left as stored."""
    return scenes.make_app(
        tmp_path / "player",
        monkeypatch,
        size=size,
        library=False,
        volume_buttons=None,
        input_profile=input_profile,
        input_overrides=input_overrides,
        before_init=before_init,
    )


def screen() -> pygame.Surface:
    surface = pygame.display.get_surface()
    assert surface is not None
    return surface


class TestProfileTheme:
    def test_no_choice_is_the_original_look_and_the_picker_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        assert app._theme is DEFAULT_THEME
        assert not app.theme_locked
        app.switch_view("settings")
        press(app, pygame.K_RIGHT)
        assert app._theme.name == "Morado"
        # ...and the child's pick is remembered, as before.
        assert (
            '"theme_name": "Morado"' in (tmp_path / "player/settings.json").read_text()
        )

    def test_a_stored_profile_theme_applies_at_startup_and_locks_the_picker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def store(cfg: DeviceConfig) -> None:
            conn = init_db(cfg.db_path)
            set_profile_settings(conn, ProfileSettings(theme="high-contrast"))
            conn.commit()
            conn.close()

        app = new_app(tmp_path, monkeypatch, before_init=store)
        assert app._theme.theme_id == "high-contrast"
        assert app.theme_locked
        app.switch_view("settings")
        sound = app._get_view("settings").handle_input(Button.RIGHT)
        assert sound is None  # nothing changed, so no click
        assert app._theme.theme_id == "high-contrast"
        assert app._get_view("settings").handle_input(Button.CANCEL) is UiSound.BACK

    def test_the_theme_changes_after_a_sync_and_reverts_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        app.switch_view("settings")
        press(app, pygame.K_RIGHT)  # the child picked Morado
        deliver(app, ProfileSettings(theme="night"), None)
        assert app._theme.theme_id == "night"
        deliver(app, ProfileSettings(theme="green"), None)
        assert app._theme.theme_id == "green"
        deliver(app, ProfileSettings(), None)
        assert app._theme.name == "Morado"  # their own pick is still there

    def test_the_profile_theme_is_not_saved_as_the_childs_pick(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        deliver(app, ProfileSettings(theme="night"), None)
        app._save_settings()
        assert "Azul" in (tmp_path / "player/settings.json").read_text()

    def test_an_unknown_theme_shows_the_default_and_stays_locked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        app.switch_view("settings")
        press(app, pygame.K_RIGHT)
        deliver(app, ProfileSettings(theme="from-the-future"), None)
        assert app._theme is DEFAULT_THEME
        assert app.theme_locked

    def test_the_settings_screen_says_who_chose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        deliver(app, ProfileSettings(theme="night"), None)
        app.switch_view("settings")
        app._render()
        locked = pygame.image.tobytes(screen(), "RGB")
        deliver(app, ProfileSettings(), None)
        app._render()
        unlocked = pygame.image.tobytes(screen(), "RGB")
        assert locked != unlocked


class TestCustomTheme:
    @pytest.fixture
    def app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MusicPlayerApp:
        return new_app(tmp_path, monkeypatch, size=(800, 480))

    def test_assets_are_loaded_and_used(
        self, app: MusicPlayerApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        media = app._config.media_root
        theme = custom(
            [
                asset(
                    media,
                    ThemeAssetRole.BACKGROUND,
                    image_bytes(tmp_path, RED),
                    "bg.webp",
                ),
                asset(
                    media,
                    ThemeAssetRole.HOME_BACKGROUND,
                    image_bytes(tmp_path, (30, 30, 200)),
                    "home.webp",
                ),
                asset(media, ThemeAssetRole.FONT, FONT.read_bytes(), "f.ttf"),
                asset(
                    media, ThemeAssetRole.SOUND_MOVE, wav_bytes(tmp_path), "move.wav"
                ),
            ]
        )
        loaded: list[str] = []
        real_sound = pygame.mixer.Sound
        monkeypatch.setattr(
            pygame.mixer,
            "Sound",
            lambda path: (loaded.append(str(path)), real_sound(path))[1],
        )
        deliver(app, ProfileSettings(theme="mine"), theme)

        # Backgrounds are scaled to the screen; colors are the theme's.
        assert views._BACKGROUND is not None
        assert views._BACKGROUND.get_size() == (800, 480)
        assert views._HOME_BACKGROUND is not None
        assert views._HOME_BACKGROUND.get_size() == (800, 480)
        assert app._theme.BG == (11, 10, 18)
        # The font and the sound come from the theme; the others stay bundled.
        assert app._theme_font_path == media / "themes/aa/f.ttf"
        assert str(media / "themes/aa/move.wav") in loaded
        assert any(p.endswith("select.ogg") for p in loaded)

        # Home uses its own background, other screens the shared one; both are
        # veiled with the theme's background color, not shown raw.
        app.switch_view("home")
        app._render()
        home = screen().get_at((5, app.layout.view_h - 5))[:3]
        app.switch_view("music")
        app._render()
        music = screen().get_at((5, app.layout.view_h - 5))[:3]
        assert home != music
        assert music != RED
        assert music[0] > music[2]  # still reddish: the picture shows through

    def test_the_photo_viewer_and_sleep_screen_stay_plain(
        self, app: MusicPlayerApp, tmp_path: Path
    ) -> None:
        media = app._config.media_root
        theme = custom(
            [
                asset(
                    media,
                    ThemeAssetRole.BACKGROUND,
                    image_bytes(tmp_path, RED),
                    "bg.webp",
                )
            ]
        )
        deliver(app, ProfileSettings(theme="mine"), theme)
        assert views._BACKGROUND is not None
        photos = app._get_view("photos")
        photos._fullscreen = True  # ty: ignore[unresolved-attribute] # the view's own state
        surface = pygame.Surface((10, 10))
        photos.draw(surface)
        assert surface.get_at((5, 5))[:3] == app._theme.BG
        app.show_sleep_screen()
        app._render()
        assert screen().get_at((3, 3))[:3] == views.SLEEP_BG

    def test_broken_assets_leave_the_defaults_and_do_not_crash(
        self,
        app: MusicPlayerApp,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        media = app._config.media_root
        theme = custom(
            [
                asset(media, ThemeAssetRole.BACKGROUND, b"junk", "bg.webp"),
                asset(media, ThemeAssetRole.HOME_BACKGROUND, b"", "home.webp"),
                asset(media, ThemeAssetRole.FONT, b"\0" * 4096, "f.ttf"),
                asset(media, ThemeAssetRole.SOUND_MOVE, b"junk", "move.ogg"),
                asset(media, ThemeAssetRole.SOUND_OPEN, b"", "open.ogg"),
            ]
        )
        with caplog.at_level(logging.WARNING):
            deliver(app, ProfileSettings(theme="mine"), theme)
        assert app._theme.theme_id == "mine"  # colors still apply
        assert views._BACKGROUND is None
        assert views._HOME_BACKGROUND is None
        assert set(app._sounds) == set(UiSound)  # every sound still has a clip
        for name in ("home", "music", "settings", "play"):
            app.switch_view(name)
            app._render()  # no crash drawing with the fallback font
        assert "font" in caplog.text.lower()

    def test_missing_files_leave_the_defaults(
        self, app: MusicPlayerApp, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The manifest can arrive before a failed download is retried."""
        media = app._config.media_root
        theme = custom([asset(media, ThemeAssetRole.BACKGROUND, b"x", "bg.webp")])
        (media / theme.assets[0].relative_path).unlink()
        with caplog.at_level(logging.WARNING):
            deliver(app, ProfileSettings(theme="mine"), theme)
        assert views._BACKGROUND is None
        assert "not on this device yet" in caplog.text

    def test_switching_back_drops_the_theme_assets(
        self, app: MusicPlayerApp, tmp_path: Path
    ) -> None:
        media = app._config.media_root
        theme = custom(
            [
                asset(
                    media,
                    ThemeAssetRole.BACKGROUND,
                    image_bytes(tmp_path, RED),
                    "bg.webp",
                ),
                asset(media, ThemeAssetRole.FONT, FONT.read_bytes(), "f.ttf"),
            ]
        )
        deliver(app, ProfileSettings(theme="mine"), theme)
        assert views._BACKGROUND is not None and app._theme_font_path is not None
        deliver(app, ProfileSettings(theme="night"), None)
        assert views._BACKGROUND is None and app._theme_font_path is None

    def test_a_stored_custom_theme_applies_offline_at_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        theme_holder: list[ThemeDefinition] = []

        def store(cfg: DeviceConfig) -> None:
            theme = custom(
                [
                    asset(
                        cfg.media_root,
                        ThemeAssetRole.BACKGROUND,
                        image_bytes(tmp_path, RED),
                        "bg.webp",
                    )
                ]
            )
            theme_holder.append(theme)
            conn = init_db(cfg.db_path)
            set_profile_settings(conn, ProfileSettings(theme="mine"))
            set_theme_definition(conn, theme)
            conn.commit()
            conn.close()

        app = new_app(tmp_path, monkeypatch, before_init=store)
        assert app._theme.theme_id == "mine"
        assert views._BACKGROUND is not None


class TestInputProfile:
    def test_volume_keys_are_off_on_the_reference_hardware(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        assert not app.volume.settings.volume_buttons
        assert not app.change_volume(-1)

    def test_the_keyboard_profile_turns_them_on_under_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch, input_profile="keyboard")
        assert app.volume.settings.volume_buttons
        deliver(app, ProfileSettings(max_volume=60), None)  # a sync keeps it on
        assert app.volume.settings.volume_buttons
        assert app.volume.settings.max_volume == 60
        press(app, pygame.K_MINUS)  # a step starts from the effective 60
        assert app.volume.level == 50
        for _ in range(20):
            press(app, pygame.K_EQUALS)
        assert app.volume.effective_percent() == 60  # never above the cap

    def test_a_parents_explicit_off_beats_the_keyboard_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: the keyboard profile used to force the buttons on."""
        app = new_app(tmp_path, monkeypatch, input_profile="keyboard")
        assert app.volume.settings.volume_buttons  # undecided: profile default
        deliver(app, ProfileSettings(max_volume=60, volume_buttons=False), None)
        assert app.volume.settings.volume_buttons is False
        assert not app.change_volume(-1)
        press(app, pygame.K_EQUALS)
        assert app.volume.effective_percent() == 60  # untouched, and capped

    def test_a_parents_explicit_on_still_works_on_the_reference_hardware(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        deliver(app, ProfileSettings(max_volume=60, volume_buttons=True), None)
        assert app.volume.settings.volume_buttons is True

    def test_undecided_follows_the_profile_when_settings_arrive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        deliver(app, ProfileSettings(volume_buttons=True), None)
        deliver(app, ProfileSettings(), None)  # the parent went back to "default"
        assert app.volume.settings.volume_buttons is False

    def test_an_override_can_turn_them_on_for_any_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch, input_overrides={"volume_buttons": True})
        assert app.volume.settings.volume_buttons

    def test_switching_the_profile_needs_no_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config key remaps the buttons: F5 toggles repeat, joystick 7 plays."""
        app = new_app(
            tmp_path,
            monkeypatch,
            input_profile="generic-gamepad",
            input_overrides={
                "keys": {"K_F5": "repeat"},
                "joy_buttons": {"7": "playpause"},
            },
        )
        before = app.playback.repeat_mode
        press(app, pygame.K_F5)
        assert app.playback.repeat_mode != before
        assert app._map_event(pygame.event.Event(pygame.JOYBUTTONDOWN, button=7)) is (
            Button.PLAYPAUSE
        )
        assert app._map_event(
            pygame.event.Event(pygame.JOYAXISMOTION, axis=0, value=1.0)
        ) is (Button.RIGHT)

    def test_the_keyboard_profile_has_no_joystick(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch, input_profile="keyboard")
        assert (
            app._map_event(pygame.event.Event(pygame.JOYBUTTONDOWN, button=0)) is None
        )

    def test_escape_still_quits_and_b_still_goes_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch)
        app.switch_view("music")
        press(app, pygame.K_b)
        assert app._current_view == "home"
        app._running = True
        press(app, pygame.K_ESCAPE)
        assert app._running is False


class TestHardwareConfig:
    def test_window_opens_at_the_configured_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = new_app(tmp_path, monkeypatch, size=(800, 480))
        assert screen().get_size() == (800, 480)
        assert (views.SCREEN_W, views.SCREEN_H) == (800, 480)
        assert app.layout.scale == 1.0
        assert app.fonts["small"].get_height() > 0

    def test_fonts_and_icons_scale_with_the_screen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        small = new_app(tmp_path / "a", monkeypatch, size=(320, 240))
        small_h = small.fonts["medium"].get_height()
        pygame.quit()
        large = new_app(tmp_path / "b", monkeypatch, size=(1280, 720))
        assert large.fonts["medium"].get_height() > 2 * small_h

    def test_command_line_overrides_the_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        args = app_module.parse_args(
            ["--width", "800", "--height", "480", "--input-profile", "keyboard"]
        )
        assert (args.width, args.height, args.input_profile) == (800, 480, "keyboard")
        none = app_module.parse_args([])
        assert (none.width, none.height, none.input_profile) == (None, None, None)

    def test_main_applies_the_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[DeviceConfig] = []

        class Stub:
            def __init__(self, config: DeviceConfig) -> None:
                seen.append(config)

            def initialize(self) -> None: ...

            def run(self) -> None: ...

        cfg = DeviceConfig("http://s", "d", "k", tmp_path / "m", tmp_path / "db")
        cfg.save(tmp_path / "config.json")
        monkeypatch.setattr(
            DeviceConfig, "load", classmethod(lambda cls, path=None: cfg)
        )
        monkeypatch.setattr(app_module, "MusicPlayerApp", Stub)
        app_module.main(["--width", "1280", "--height", "720"])
        app_module.main([])
        assert (seen[0].width, seen[0].height) == (1280, 720)
        assert (seen[1].width, seen[1].height) == (640, 480)
