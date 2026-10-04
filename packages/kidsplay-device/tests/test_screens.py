"""The player at every supported resolution, in both languages.

Acceptance test for hardware profiles (#6): the real ``MusicPlayerApp`` runs
headless at 320×240, 640×480, 800×480 and 1280×720 and is walked through every
screen (home, the music/audiobook/photo lists, the photo viewer, play, settings,
the sleep screen, the volume overlay), once in English and once in Spanish,
whose strings are longer.

Each scene is saved as a PNG (in ``$KIDSPLAY_SHOTS_DIR`` if set, so a person can
look at them; otherwise in pytest's temp directory, so they are never in git)
and the test asserts on the frames it saved:

- the frame has the size of the screen and is not blank;
- nothing was drawn beyond the edge of the surface it was drawn on. Every
  ``blit`` goes through ``SpySurface``, which records any that spill over, so
  text or art that overflows or clips fails the test;
- the walk really reached each screen (the app's current view is checked);
- text fits (#44). Every ``Font.render`` and every ``truncate_text`` goes
  through a spy, so a scene fails if a label the app wrote itself (anything but
  the child's own titles, which may be as long as they like) was cut short with
  an ellipsis, if two pieces of text overlap, if text was drawn outside the
  clip rectangle, or if text is drawn in a font too small to read.
"""

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pygame
import pytest
from PIL import Image

from kidsplay_device import app as app_module
from kidsplay_device import i18n, views
from kidsplay_device.app import MusicPlayerApp
from kidsplay_device.fonts import load_text_fonts
from kidsplay_device.layout import Layout
from kidsplay_device.views import SLEEP_BG

from . import scenes

SIZES = [(240, 180), (320, 240), (640, 480), (800, 480), (1280, 720)]
LANGUAGES = ["en", "es"]


#: Smallest text height, in pixels (``Font.get_height``), that counts as readable
#: on a 3-inch handheld. The smallest style is 30 px on the 640×480 design.
MIN_TEXT_HEIGHT = 10

#: The real font class, before ``spy`` swaps in ``SpyFont``.
RealFont = pygame.font.Font


@dataclass
class TextBlit:
    """One piece of text drawn onto a surface."""

    target: int  # id() of the surface it was drawn on
    text: str
    rect: pygame.Rect  # the glyphs' bounding box, in the target's coordinates
    font_height: int


class Recorder:
    """What the spies saw while one scene was drawn."""

    def __init__(self) -> None:
        self.rendered: dict[int, tuple[pygame.Surface, str, int]] = {}
        self.blits: list[TextBlit] = []
        self.spills: list[str] = []
        self.truncated: list[tuple[str, str]] = []
        self.clipped: list[str] = []
        # (box, number of text blits made before it was drawn), per popup.
        self.popups: list[tuple[pygame.Rect, int]] = []

    def reset_scene(self) -> None:
        """Forget the last scene's text (the rendered surfaces are kept)."""
        self.blits = []
        self.truncated = []
        self.clipped = []
        self.popups = []


RECORDER = Recorder()


def is_icon(text: str) -> bool:
    """Whether text is a Font Awesome glyph (private use area), not words."""
    return bool(text) and all(0xE000 <= ord(c) <= 0xF8FF for c in text)


class SpyFont(RealFont):
    """A font that remembers which surface holds which text."""

    def render(  # ty: ignore[invalid-method-override] # pygame's stub has a wider signature
        self,
        text: str | bytes | None,
        antialias: bool,
        color: pygame.typing.ColorLike,
        bgcolor: pygame.typing.ColorLike | None = None,
    ) -> pygame.Surface:
        if bgcolor is None:
            surface = super().render(text, antialias, color)
        else:
            surface = super().render(text, antialias, color, bgcolor)
        # Keep the surface alive so its id() cannot be reused by another.
        RECORDER.rendered[id(surface)] = (surface, str(text), self.get_height())
        return surface


class SpySurface(pygame.Surface):
    """A surface that records every blit reaching past its own edges, and every
    piece of text drawn on it."""

    spills: list[str] = []

    def blit(  # ty: ignore[invalid-method-override] # pygame's stub has a wider signature
        self,
        source: pygame.Surface,
        dest: pygame.typing.Point | pygame.Rect,
        area: pygame.typing.RectLike | None = None,
        special_flags: int = 0,
    ) -> pygame.Rect:
        size = pygame.Rect(area).size if area is not None else source.get_size()
        pos = dest.topleft if isinstance(dest, pygame.Rect) else dest
        rect = pygame.Rect(pos, size)
        if not self.get_rect().contains(rect):
            SpySurface.spills.append(f"{rect} does not fit {self.get_size()}")
        seen = RECORDER.rendered.get(id(source))
        if seen is not None and area is None:
            _, text, font_height = seen
            glyphs = source.get_bounding_rect().move(pos)
            if glyphs.width and glyphs.height:
                RECORDER.blits.append(TextBlit(id(self), text, glyphs, font_height))
                if not self.get_clip().contains(glyphs):
                    RECORDER.clipped.append(
                        f"{text!r} at {glyphs} is outside the clip {self.get_clip()}"
                    )
        return super().blit(source, dest, area, special_flags)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[SpySurface]]:
    global RECORDER
    RECORDER = Recorder()
    SpySurface.spills = []
    monkeypatch.setattr(pygame, "Surface", SpySurface)
    monkeypatch.setattr(pygame.font, "Font", SpyFont)

    real_truncate = views.truncate_text

    def truncate_text(font: pygame.font.Font, text: str, max_width: int) -> str:
        shown = real_truncate(font, text, max_width)
        if shown != text:
            RECORDER.truncated.append((text, shown))
        return shown

    monkeypatch.setattr(views, "truncate_text", truncate_text)
    monkeypatch.setattr(app_module, "truncate_text", truncate_text)

    real_overlay = MusicPlayerApp._draw_volume_overlay

    def draw_volume_overlay(self: MusicPlayerApp) -> None:
        RECORDER.popups.append((self._volume_overlay_rect(), len(RECORDER.blits)))
        real_overlay(self)

    monkeypatch.setattr(MusicPlayerApp, "_draw_volume_overlay", draw_volume_overlay)
    yield SpySurface


def text_problems() -> list[str]:
    """What is wrong with the text drawn since the last scene, if anything."""
    problems: list[str] = []
    allowed = scenes.media_texts()
    for original, shown in RECORDER.truncated:
        if original not in allowed:
            problems.append(f"label {original!r} was cut short to {shown!r}")
    problems += RECORDER.clipped
    words = [b for b in RECORDER.blits if not is_icon(b.text)]
    for b in words:
        if b.font_height < MIN_TEXT_HEIGHT:
            problems.append(
                f"{b.text!r} is drawn in a {b.font_height}px font "
                f"(minimum {MIN_TEXT_HEIGHT}px)"
            )
    for i, a in enumerate(RECORDER.blits):
        for b in RECORDER.blits[i + 1 :]:
            if a.target == b.target and a.text != b.text and a.rect.colliderect(b.rect):
                problems.append(
                    f"text {a.text!r} {a.rect} overlaps {b.text!r} {b.rect}"
                )
    for box, drawn_before in RECORDER.popups:
        for b in RECORDER.blits[:drawn_before]:
            if not is_icon(b.text) and box.colliderect(b.rect):
                problems.append(f"popup {box} covers text {b.text!r} {b.rect}")
    return problems


def shots_dir(tmp_path: Path, size: tuple[int, int], lang: str) -> Path:
    root = Path(os.environ.get("KIDSPLAY_SHOTS_DIR") or tmp_path)
    out = root / f"{size[0]}x{size[1]}-{lang}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def run_walk(
    app: MusicPlayerApp,
    walk: Callable[[MusicPlayerApp, Callable[[str], None]], None],
    size: tuple[int, int],
    out: Path,
) -> dict[str, str]:
    """Run a walk with the spy screen; return each scene's view name."""
    # initialize() drew into a real display surface; swap in a spying one, so the
    # playback bar, overlay and sleep screen (drawn straight onto it) are checked.
    app._screen = SpySurface(size)
    views: dict[str, str] = {}

    def capture(name: str) -> None:
        screen = app._screen
        assert screen is not None
        assert SpySurface.spills == [], f"{name}: {SpySurface.spills}"
        assert text_problems() == [], name
        RECORDER.reset_scene()
        pygame.image.save(screen, str(out / f"{name}.png"))
        views[name] = app._current_view

    walk(app, capture)
    return views


@pytest.mark.usefixtures("headless")
@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
class TestEveryScreen:
    def test_every_screen_fits_and_is_reachable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        spy: type[SpySurface],
        size: tuple[int, int],
        lang: str,
    ) -> None:
        app = scenes.make_app(tmp_path / "player", monkeypatch, size=size)
        i18n.activate(lang)
        out = shots_dir(tmp_path, size, lang)
        views = run_walk(app, scenes.walk, size, out)

        assert views == scenes.WALK_VIEWS  # every screen was reached
        for name in scenes.WALK_VIEWS:
            with Image.open(out / f"{name}.png") as frame:
                assert frame.size == size, name
                colours = frame.convert("RGB").getcolors(maxcolors=size[0] * size[1])
            assert colours is not None and len(colours) >= 4, f"{name} is blank"

    def test_sleep_screen_has_no_playback_bar(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        spy: type[SpySurface],
        size: tuple[int, int],
        lang: str,
    ) -> None:
        app = scenes.make_app(tmp_path / "player", monkeypatch, size=size)
        i18n.activate(lang)
        out = shots_dir(tmp_path, size, lang)
        run_walk(app, scenes.walk, size, out)
        with Image.open(out / "sleep.png") as frame:
            rgb = frame.convert("RGB")
            # Bottom corners and the middle of the bottom row: all sleep colour.
            w, h = size
            assert {rgb.getpixel((x, h - 1)) for x in (0, w // 2, w - 1)} == {SLEEP_BG}

    def test_empty_library_screens_fit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        spy: type[SpySurface],
        size: tuple[int, int],
        lang: str,
    ) -> None:
        app = scenes.make_app(
            tmp_path / "player", monkeypatch, size=size, library=False
        )
        i18n.activate(lang)
        out = shots_dir(tmp_path, size, lang)
        views = run_walk(app, scenes.walk_empty, size, out)
        assert views == {
            "empty-home": "home",
            "empty-music": "music",
            "empty-audiobooks": "audiobooks",
            "empty-photos": "photos",
            "empty-play": "play",
        }


@pytest.mark.usefixtures("headless")
def test_spanish_frames_differ_from_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: type[SpySurface]
) -> None:
    """The language really switched: the same scenes render differently."""
    frames: dict[str, dict[str, bytes]] = {}
    for lang in LANGUAGES:
        app = scenes.make_app(tmp_path / lang, monkeypatch, size=(640, 480))
        i18n.activate(lang)
        out = shots_dir(tmp_path, (640, 480), lang)
        run_walk(app, scenes.walk, (640, 480), out)
        frames[lang] = {
            n: (out / f"{n}.png").read_bytes()
            for n in ("home", "settings", "sleep", "play-volume")
        }
        pygame.quit()
    for name in frames["en"]:
        assert frames["en"][name] != frames["es"][name], name


def test_the_spy_catches_a_blit_past_the_edge(spy: type[SpySurface]) -> None:
    """Mutation check: the guard above must actually fail on overflow."""
    target = SpySurface((100, 50))
    target.blit(pygame.Surface((20, 10)), (10, 10))
    assert SpySurface.spills == []
    target.blit(pygame.Surface((20, 10)), (90, 10))  # 10 px too far right
    target.blit(pygame.Surface((20, 10)), (10, 45))  # 5 px too low
    target.blit(pygame.Surface((20, 10)), (-1, 0))  # 1 px too far left
    assert len(SpySurface.spills) == 3


# ---------------------------------------------------------------------------
# Mutation checks: each text guard must actually fail on the fault it names
# ---------------------------------------------------------------------------


@pytest.fixture
def font(spy: type[SpySurface]) -> Iterator[pygame.font.Font]:
    pygame.font.init()
    RECORDER.reset_scene()
    yield pygame.font.Font(None, 30)
    pygame.font.quit()


def test_the_spy_catches_a_cut_short_label(font: pygame.font.Font) -> None:
    label = views.truncate_text(font, "Contraste de colores", 60)
    assert label.endswith("…")
    assert text_problems() == [
        f"label 'Contraste de colores' was cut short to {label!r}"
    ]


def test_the_spy_lets_the_childs_own_titles_be_cut_short(
    font: pygame.font.Font,
) -> None:
    views.truncate_text(font, scenes.LONG_TITLE, 60)
    assert RECORDER.truncated
    assert text_problems() == []


def test_the_spy_lets_text_that_fits_through(font: pygame.font.Font) -> None:
    assert views.truncate_text(font, "Back", 200) == "Back"
    assert RECORDER.truncated == []


def test_the_spy_catches_overlapping_text(font: pygame.font.Font) -> None:
    target = SpySurface((200, 100))
    target.blit(font.render("Volume", True, (255, 255, 255)), (10, 10))
    target.blit(font.render("Repeat", True, (255, 255, 255)), (30, 15))
    problems = text_problems()
    assert len(problems) == 1 and "overlaps" in problems[0]


def test_the_spy_catches_a_popup_covering_text(font: pygame.font.Font) -> None:
    target = SpySurface((200, 100))
    target.blit(font.render("Artist", True, (255, 255, 255)), (10, 60))
    RECORDER.popups.append((pygame.Rect(5, 50, 100, 40), len(RECORDER.blits)))
    problems = text_problems()
    assert len(problems) == 1 and "covers text 'Artist'" in problems[0]


def test_the_spy_lets_a_popup_clear_of_text_through(font: pygame.font.Font) -> None:
    target = SpySurface((200, 100))
    target.blit(font.render("Artist", True, (255, 255, 255)), (10, 10))
    RECORDER.popups.append((pygame.Rect(5, 50, 100, 40), len(RECORDER.blits)))
    # Text drawn after the popup (its own label) is on top of it, not covered.
    target.blit(font.render("Volume 50%", True, (255, 255, 255)), (10, 55))
    assert text_problems() == []


def test_the_spy_lets_adjacent_and_repeated_text_through(
    font: pygame.font.Font,
) -> None:
    target = SpySurface((300, 100))
    one = font.render("Volume", True, (255, 255, 255))
    target.blit(one, (10, 10))
    target.blit(
        font.render("Repeat", True, (255, 255, 255)), (10 + one.get_width(), 10)
    )
    target.blit(font.render("Volume", True, (0, 0, 0)), (11, 11))  # a drop shadow
    assert text_problems() == []


def test_text_on_different_surfaces_does_not_overlap(font: pygame.font.Font) -> None:
    a, b = SpySurface((100, 50)), SpySurface((100, 50))
    a.blit(font.render("Volume", True, (255, 255, 255)), (10, 10))
    b.blit(font.render("Repeat", True, (255, 255, 255)), (10, 10))
    assert text_problems() == []


def test_the_spy_catches_text_outside_the_clip_rect(font: pygame.font.Font) -> None:
    target = SpySurface((200, 100))
    target.set_clip(pygame.Rect(0, 0, 40, 100))
    target.blit(font.render("Contraste", True, (255, 255, 255)), (5, 5))
    problems = text_problems()
    assert len(problems) == 1 and "outside the clip" in problems[0]


def test_the_spy_catches_a_font_too_small_to_read(spy: type[SpySurface]) -> None:
    pygame.font.init()
    RECORDER.reset_scene()
    tiny = pygame.font.Font(None, 11)
    assert tiny.get_height() < MIN_TEXT_HEIGHT
    SpySurface((100, 50)).blit(tiny.render("Volume", True, (255, 255, 255)), (5, 5))
    problems = text_problems()
    assert len(problems) == 1 and "font (minimum" in problems[0]
    pygame.font.quit()


def test_the_smallest_text_style_is_readable_at_every_size() -> None:
    """The font floor keeps the 240×180 screen above the readability minimum."""
    pygame.font.init()
    try:
        for width, height in SIZES:
            scale = Layout.for_size(width, height).scale
            heights = {
                name: font.get_height() for name, font in load_text_fonts(scale).items()
            }
            assert min(heights.values()) >= MIN_TEXT_HEIGHT, (width, height, heights)
    finally:
        pygame.font.quit()
