"""A populated headless player and a scripted walk through every screen.

Shared by the screenshot tests. The library is generated (Pillow drawings and a
silent WAV), so no media has to be committed. The player is the real
``MusicPlayerApp`` with SDL's dummy video and audio drivers; the walk posts key
presses and renders one frame per step, so a capture never depends on timing.

The titles are deliberately long and accented ("¿Dónde está?"), because the
layout has to survive the longest realistic text in both languages.
"""

import uuid
import wave
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pygame
import pytest
from PIL import Image, ImageDraw

from kidsplay_device import app as app_module
from kidsplay_device.app import MusicPlayerApp
from kidsplay_device.config import DeviceConfig
from kidsplay_device.controls import VolumeControl
from kidsplay_device.database import init_db, upsert_media_item
from kidsplay_models import ProfileSettings
from kidsplay_models.media import MediaType
from kidsplay_models.sync import SyncMediaEntry

ALBUM = "Canciones de cuna: ¿Dónde está el osito de peluche?"
LONG_TITLE = "La nana más larga del mundo para dormir a los pequeños ¿verdad?"

# (playlist_title, [track titles], artist)
MUSIC = [
    (ALBUM, [LONG_TITLE, "Arroz con leche", "Cielito lindo", "El patio de mi casa"]),
    ("Road trip favourites", ["Sunshine", "Another song", "Short", "Yet another one"]),
    ("Dinosaur songs", ["Stomp stomp", "The big roar", "Tiny raptor"]),
]
BOOKS = [
    ("Las aventuras de Pinocho, el muñeco de madera", ["Capítulo 1", "Capítulo 2"]),
    ("The Little Prince", ["Chapter 1", "Chapter 2", "Chapter 3"]),
]
PHOTOS = ("Vacaciones en la playa: verano 2025", 7)
ARTIST = "Los Cantores del Sol y la Luna"


def media_texts() -> set[str]:
    """Every string in the generated library: the child's own text.

    These may be longer than the screen and are shortened with an ellipsis on
    purpose. Every other string the player draws is its own, and must fit.
    """
    texts = {PHOTOS[0], ARTIST}
    for playlist, titles in [*MUSIC, *BOOKS]:
        texts.add(playlist)
        texts.update(titles)
    texts.update(photo_title(i) for i in range(PHOTOS[1]))
    return texts


def photo_title(index: int) -> str:
    """Title of the ``index``-th (0-based) generated photo."""
    return f"Foto {index + 1}: una tarde soleada en la playa con toda la familia"


def _thumb(path: Path, size: int, hue: int, label: str) -> None:
    """Draw a coloured square with a letter as a WebP thumbnail."""
    colour = ((hue * 70) % 200 + 40, (hue * 110) % 200 + 40, (hue * 30) % 200 + 40)
    img = Image.new("RGB", (size, size), colour)
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        (size // 4, size // 4, size * 3 // 4, size * 3 // 4), fill=(250, 250, 250)
    )
    draw.text((size // 2 - 4, size // 2 - 6), label[:1], fill=(20, 20, 20))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "WEBP")


def _photo(path: Path, hue: int) -> None:
    """A 4:3 gradient 'photo'."""
    img = Image.new("RGB", (640, 480))
    px = img.load()
    assert px is not None
    for x in range(640):
        for y in range(0, 480, 4):
            for dy in range(4):
                px[x, y + dy] = (
                    (x * 255 // 640 + hue * 30) % 256,
                    (y * 255 // 480) % 256,
                    (hue * 60) % 256,
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "WEBP")


def _silence(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\x00\x00" * 2 * 44100)


def build_library(media_root: Path, db_path: Path) -> None:
    """Write a media library into a device database and media directory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    hue = 0

    def add(
        media_type: MediaType,
        playlist: str,
        title: str,
        *,
        audio: bool = True,
        photo: bool = False,
    ) -> None:
        nonlocal hue
        hue += 1
        key = uuid.uuid4().hex
        thumbs: dict[str, str] = {}
        for size in (60, 200, 480):
            rel = f"thumbnails/{key[:2]}/{key}_{size}x{size}.webp"
            _thumb(media_root / rel, size, hue, title)
            thumbs[f"{size}x{size}"] = rel
        audio_path = None
        if audio:
            audio_path = f"audio/{key[:2]}/{key}.wav"
            _silence(media_root / audio_path)
        photo_path = None
        if photo:
            photo_path = f"photos/{key[:2]}/{key}_640x480.webp"
            _photo(media_root / photo_path, hue)
        upsert_media_item(
            conn,
            SyncMediaEntry(
                media_id=uuid.uuid4(),
                media_type=media_type,
                playlist_title=playlist,
                title=title,
                artist=ARTIST,
                duration_seconds=187,
                audio_path=audio_path,
                photo_path=photo_path,
                thumbnail_paths=thumbs,
            ),
        )

    for playlist, titles in MUSIC:
        for title in titles:
            add(MediaType.MUSIC, playlist, title)
    for playlist, titles in BOOKS:
        for title in titles:
            add(MediaType.AUDIOBOOK, playlist, title)
    for i in range(PHOTOS[1]):
        add(
            MediaType.PHOTO,
            PHOTOS[0],
            photo_title(i),
            audio=False,
            photo=True,
        )
    conn.commit()
    conn.close()


class NoSync:
    """Stands in for ``SyncClient``: keeps its callbacks, never touches a network."""

    def __init__(self, config: DeviceConfig, **callbacks: Callable[..., None]) -> None:
        del config
        self.callbacks = callbacks

    def run_sync_loop(self) -> None:
        """No network in tests."""


def make_app(
    tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    size: tuple[int, int] | None = None,
    library: bool = True,
    volume_buttons: bool | None = True,
    input_profile: str = "gpi2",
    input_overrides: dict[str, object] | None = None,
    before_init: Callable[[DeviceConfig], None] | None = None,
    credentials: tuple[str, str] | None = None,
) -> MusicPlayerApp:
    """Build and initialise a headless player over a generated library.

    Args:
        tmp: Scratch directory (a pytest ``tmp_path``).
        monkeypatch: Used to replace the sync thread with a stub.
        size: ``(width, height)``, or None for the player's default.
        library: Fill the library; False leaves it empty.
        volume_buttons: Enable the in-app volume buttons (for the overlay);
            None leaves the settings as they were stored.
        input_profile: ``input_profile`` for the config.
        input_overrides: ``input_overrides`` for the config.
        before_init: Called with the config before the app is initialised, to
            store settings or files the way an earlier sync would have.
        credentials: ``(device_id, api_key)`` of a registered device, for tests
            that sync from a real server; random ones otherwise.

    Returns:
        An app on which ``initialize()`` has run. The sync thread is pointed
        at a closed port and never reaches a server.
    """
    width, height = size or (640, 480)
    config = DeviceConfig(
        server_url="http://127.0.0.1:9",
        device_id=credentials[0] if credentials else str(uuid.uuid4()),
        api_key=credentials[1] if credentials else "k",
        media_root=tmp / "media",
        db_path=tmp / "db.sqlite",
        sync_interval_seconds=3600,
        width=width,
        height=height,
        input_profile=input_profile,
        input_overrides=input_overrides or {},
    )
    if library:
        build_library(config.media_root, config.db_path)
    monkeypatch.setattr(app_module, "SyncClient", NoSync)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.media_root.mkdir(parents=True, exist_ok=True)
    if before_init is not None:
        before_init(config)
    app = MusicPlayerApp(config)
    # Never touch the real ~/.kidsplay/settings.json.
    app._SETTINGS_PATH = tmp / "settings.json"
    app.initialize()
    if volume_buttons is not None:
        settings = ProfileSettings(volume_buttons=volume_buttons, max_volume=80)
        app.volume = VolumeControl(settings, 50)
    return app


def press(app: MusicPlayerApp, *keys: int) -> None:
    """Post key presses and let the player handle them."""
    for key in keys:
        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=key))
    app._process_events()


def walk(app: MusicPlayerApp, capture: Callable[[str], None]) -> None:
    """Visit every screen of a populated player, capturing each.

    Args:
        app: A player over the library from :func:`build_library`.
        capture: Called with a scene name after that scene has been rendered
            (``app._render()``), so it can save or inspect the screen.
    """
    k = pygame

    def shot(name: str) -> None:
        app._render()
        capture(name)

    shot("home")
    press(app, k.K_RETURN)
    shot("music-albums")
    press(app, k.K_RETURN)
    shot("music-tracks")
    press(app, k.K_DOWN, k.K_DOWN, k.K_DOWN)
    shot("music-tracks-scrolled")
    press(app, k.K_RETURN)
    app.playback.current_position = 37.0
    shot("play")
    press(app, k.K_EQUALS)
    shot("play-volume")
    app._volume_overlay_until = float("-inf")
    press(app, k.K_y)
    shot("play-repeat")
    press(app, k.K_BACKSPACE, k.K_BACKSPACE, k.K_BACKSPACE)
    shot("home-after-music")
    press(app, k.K_RIGHT, k.K_RETURN)
    shot("audiobooks-books")
    press(app, k.K_RETURN)
    shot("audiobooks-chapters")
    press(app, k.K_BACKSPACE, k.K_BACKSPACE)
    press(app, k.K_LEFT, k.K_DOWN, k.K_RETURN)
    shot("photos-albums")
    press(app, k.K_RETURN)
    shot("photos-grid")
    press(app, k.K_DOWN, k.K_DOWN, k.K_RIGHT)
    shot("photos-grid-scrolled")
    press(app, k.K_RETURN)
    shot("photo")
    press(app, k.K_BACKSPACE, k.K_BACKSPACE, k.K_BACKSPACE)
    press(app, k.K_RIGHT, k.K_RETURN)
    shot("settings")
    press(app, k.K_RIGHT)
    shot("settings-next-theme")
    press(app, k.K_BACKSPACE)
    app.show_sleep_screen(datetime(2026, 9, 29, 7, 30))
    shot("sleep")


#: The view each scene of :func:`walk` shows, for tests to assert on.
WALK_VIEWS: dict[str, str] = {
    "home": "home",
    "music-albums": "music",
    "music-tracks": "music",
    "music-tracks-scrolled": "music",
    "play": "play",
    "play-volume": "play",
    "play-repeat": "play",
    "home-after-music": "home",
    "audiobooks-books": "audiobooks",
    "audiobooks-chapters": "audiobooks",
    "photos-albums": "photos",
    "photos-grid": "photos",
    "photos-grid-scrolled": "photos",
    "photo": "photos",
    "settings": "settings",
    "settings-next-theme": "settings",
    "sleep": "sleep",
}


def walk_empty(app: MusicPlayerApp, capture: Callable[[str], None]) -> None:
    """Visit the list screens and the play view of a player with no media.

    Args:
        app: A player over an empty library.
        capture: Called with a scene name after it has been rendered.
    """
    k = pygame

    def shot(name: str) -> None:
        app._render()
        capture(name)

    shot("empty-home")
    press(app, k.K_RETURN)
    shot("empty-music")
    press(app, k.K_BACKSPACE, k.K_RIGHT, k.K_RETURN)
    shot("empty-audiobooks")
    press(app, k.K_BACKSPACE, k.K_LEFT, k.K_DOWN, k.K_RETURN)
    shot("empty-photos")
    press(app, k.K_BACKSPACE)
    app.switch_view("play")
    shot("empty-play")
