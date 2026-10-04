"""Generate the bundled demo media in ``demo/media/``.

Everything here is synthesized from scratch, so the output can be dedicated
to the public domain (CC0 1.0, see ``THIRD_PARTY_NOTICES.md``):

- **Music:** public-domain melodies rendered by a tiny additive synth in pure
  Python, encoded to MP3 by ffmpeg, tagged with mutagen and given cover art
  drawn with Pillow.
- **Audiobooks:** two of Aesop's fables (George Fyler Townsend's 1867
  translation, public domain) read by ffmpeg's ``flite`` speech synthesizer.
- **Photos:** landscapes drawn procedurally with Pillow.

The committed files are the source of truth for the demo; this script only
exists so they can be regenerated or extended. It needs ``ffmpeg`` built with
``libmp3lame`` and ``libflite`` (true of Debian/Ubuntu's ``ffmpeg`` package).
**Homebrew's ffmpeg has no flite**, so on macOS run this in Debian/Ubuntu (a
container or a Linux machine). The script checks for both features first and
refuses to start without them. It also never touches the existing files until
every new one has been generated: it builds in a scratch directory and swaps
it in at the end, so a failure leaves ``demo/media/`` exactly as it was. The
scratch directory is a sibling of ``demo/media/`` (same filesystem, so the swap
is a rename), so even a killed run leaves nothing inside it.
Output is deterministic for a given ffmpeg, flite and Pillow version.

Usage::

    uv run --all-packages python -m demo.make_sample_media
"""

from __future__ import annotations

import io
import math
import random
import shutil
import struct
import subprocess
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import mutagen.id3
from PIL import Image, ImageDraw

MEDIA_DIR = Path(__file__).parent / "media"

ARTIST = "KidsPlay Music Box"
NARRATOR = "KidsPlay Reader"
SAMPLE_RATE = 22050

# ---------------------------------------------------------------------------
# Melodies
# ---------------------------------------------------------------------------

_NOTE_OFFSETS = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}

# A tune is a list of (note, beats). A note is a name plus octave, e.g. "C5",
# "F#4" or "Bb3"; "R" is a rest.
Tune = list[tuple[str, float]]


def note_frequency(note: str) -> float:
    """Return the frequency in Hz of a note name such as ``"A4"`` or ``"F#5"``.

    Args:
        note: Letter, optional ``#``/``b`` accidental, then the octave number.

    Returns:
        Equal-tempered frequency, with A4 = 440 Hz.
    """
    letter, rest = note[0], note[1:]
    semitones = _NOTE_OFFSETS[letter]
    if rest.startswith("#"):
        semitones += 1
        rest = rest[1:]
    elif rest.startswith("b"):
        semitones -= 1
        rest = rest[1:]
    semitones += (int(rest) - 4) * 12
    return 440.0 * 2 ** (semitones / 12)


def _phrase(notes: str, beats: float = 1.0) -> Tune:
    return [(n, beats) for n in notes.split()]


TWINKLE: Tune = (
    _phrase("C5 C5 G5 G5 A5 A5")
    + [("G5", 2)]
    + _phrase("F5 F5 E5 E5 D5 D5")
    + [("C5", 2)]
    + _phrase("G5 G5 F5 F5 E5 E5")
    + [("D5", 2)]
    + _phrase("G5 G5 F5 F5 E5 E5")
    + [("D5", 2)]
    + _phrase("C5 C5 G5 G5 A5 A5")
    + [("G5", 2)]
    + _phrase("F5 F5 E5 E5 D5 D5")
    + [("C5", 2)]
)
TWINKLE_BASS: Tune = _phrase(
    "C3 C3 F3 C3 F3 C3 G3 C3 C3 F3 C3 G3 C3 F3 C3 G3 C3 C3 F3 C3 F3 C3 G3 C3", 2
)

FRERE_JACQUES: Tune = (
    _phrase("F4 G4 A4 F4") * 2
    + (_phrase("A4 Bb4") + [("C5", 2)]) * 2
    + (_phrase("C5 D5 C5 Bb4", 0.5) + _phrase("A4 F4")) * 2
    + (_phrase("F4 C4") + [("F4", 2)]) * 2
)
FRERE_JACQUES_BASS: Tune = [("F3", 4)] * 8

MARY_LAMB: Tune = (
    _phrase("E5 D5 C5 D5 E5 E5")
    + [("E5", 2)]
    + _phrase("D5 D5")
    + [("D5", 2)]
    + _phrase("E5 G5")
    + [("G5", 2)]
    + _phrase("E5 D5 C5 D5 E5 E5 E5 E5 D5 D5 E5 D5")
    + [("C5", 4)]
)
MARY_LAMB_BASS: Tune = _phrase("C3 C3 G3 C3 C3 C3 G3 C3", 4)

ODE_TO_JOY: Tune = (
    _phrase("E5 E5 F5 G5 G5 F5 E5 D5 C5 C5 D5 E5")
    + [("E5", 1.5), ("D5", 0.5), ("D5", 2)]
    + _phrase("E5 E5 F5 G5 G5 F5 E5 D5 C5 C5 D5 E5")
    + [("D5", 1.5), ("C5", 0.5), ("C5", 2)]
)
ODE_TO_JOY_BASS: Tune = _phrase("C3 G3 C3 G3 C3 G3 G3 C3", 4)

LULLABY: Tune = (
    _phrase("E5 E5")
    + [("G5", 2)]
    + _phrase("E5 E5")
    + [("G5", 2)]
    + _phrase("E5 G5")
    + [("C6", 2), ("B5", 2)]
    + _phrase("A5 A5")
    + [("G5", 2)]
    + _phrase("D5 E5")
    + [("F5", 2)]
    + _phrase("D5 D5 E5")
    + [("F5", 2)]
    + _phrase("D5 F5 B5 A5")
    + [("G5", 2), ("B5", 2), ("C6", 4)]
)
LULLABY_BASS: Tune = _phrase("C3 C3 C3 F3 C3 G3 G3 G3 G3 C3", 3)


def render_tune(
    melody: Tune,
    bass: Tune,
    bpm: float,
    repeats: int = 2,
) -> list[float]:
    """Render a melody and a bass line as music-box style samples.

    Args:
        melody: Melody notes and their lengths in beats.
        bass: Bass notes, played softer and an octave or two lower.
        bpm: Tempo in beats per minute.
        repeats: How many times to play the tune through.

    Returns:
        Mono samples in the range -1.0 to 1.0 at ``SAMPLE_RATE``.
    """
    beat = 60.0 / bpm
    total_beats = sum(b for _, b in melody) * repeats
    samples = [0.0] * (int((total_beats * beat + 2.0) * SAMPLE_RATE))

    tune_len = sum(b for _, b in melody) * beat

    def add_line(line: Tune, level: float, decay: float) -> None:
        for rep in range(repeats):
            t = rep * tune_len
            for note, beats in line:
                if note != "R" and t < (rep + 1) * tune_len:
                    freq = note_frequency(note)
                    _add_note(samples, freq, t, beats * beat, level, decay)
                t += beats * beat

    add_line(melody, 0.45, 3.0)
    add_line(bass, 0.22, 1.5)
    return samples


def _add_note(
    samples: list[float],
    freq: float,
    start: float,
    length: float,
    level: float,
    decay: float,
) -> None:
    """Mix one plucked note (fundamental plus two soft overtones) into samples."""
    first = int(start * SAMPLE_RATE)
    ring = length + 0.6  # let the note ring into the next one
    count = min(int(ring * SAMPLE_RATE), len(samples) - first)
    w = 2 * math.pi * freq / SAMPLE_RATE
    for i in range(count):
        t = i / SAMPLE_RATE
        attack = min(1.0, t / 0.005)
        env = attack * math.exp(-decay * t)
        tone = math.sin(w * i) + 0.3 * math.sin(2 * w * i) + 0.1 * math.sin(3 * w * i)
        samples[first + i] += level * env * tone / 1.4


def write_wav(samples: list[float], path: Path) -> None:
    """Write mono float samples as a 16-bit PCM WAV file.

    Args:
        samples: Samples in -1.0 to 1.0; anything outside is clipped.
        path: Destination file.
    """
    frames = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)


def encode_mp3(src: Path, dest: Path, bitrate: str) -> None:
    """Encode an audio file (or a lavfi source) to a bit-exact mono MP3.

    Args:
        src: Input WAV file.
        dest: Output MP3 path.
        bitrate: ffmpeg bitrate, e.g. ``"64k"``.
    """
    _ffmpeg(["-i", str(src), *_mp3_args(bitrate), str(dest)])


def _mp3_args(bitrate: str) -> list[str]:
    return [
        "-map_metadata",
        "-1",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-codec:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-id3v2_version",
        "0",
        "-write_xing",
        "0",
    ]


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def tag_mp3(
    path: Path,
    *,
    title: str,
    artist: str,
    album: str,
    track: int,
    cover: bytes,
) -> None:
    """Write ID3 tags and embedded cover art.

    Args:
        path: The MP3 file to tag in place.
        title: Track title.
        artist: Artist or narrator.
        album: Album (becomes the playlist title on ingest).
        track: Track number.
        cover: JPEG bytes for the front cover.
    """
    tags = mutagen.id3.ID3()
    tags.add(mutagen.id3.TIT2(encoding=3, text=title))
    tags.add(mutagen.id3.TPE1(encoding=3, text=artist))
    tags.add(mutagen.id3.TALB(encoding=3, text=album))
    tags.add(mutagen.id3.TRCK(encoding=3, text=str(track)))
    tags.add(mutagen.id3.TCOP(encoding=3, text="CC0 1.0 (public domain)"))
    tags.add(
        mutagen.id3.APIC(
            encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover
        )
    )
    tags.save(path, v2_version=3)


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

RGB = tuple[int, int, int]


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _gradient(size: tuple[int, int], stops: list[RGB]) -> Image.Image:
    """Vertical gradient through evenly spaced colour stops."""
    w, h = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    segments = len(stops) - 1
    for y in range(h):
        pos = y / max(1, h - 1) * segments
        i = min(int(pos), segments - 1)
        draw.line([(0, y), (w, y)], fill=_lerp(stops[i], stops[i + 1], pos - i))
    return img


def _hills(
    draw: ImageDraw.ImageDraw,
    size: tuple[int, int],
    base: float,
    amp: float,
    color: RGB,
    rng: random.Random,
) -> None:
    w, h = size
    phase, freq = rng.uniform(0, 6.28), rng.uniform(1.2, 2.2)
    pts = [
        (x, h * base + h * amp * math.sin(phase + freq * 2 * math.pi * x / w))
        for x in range(0, w + 8, 8)
    ]
    draw.polygon([(0, h), *pts, (w, h)], fill=color)


def _star(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill: RGB) -> None:
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    draw.polygon(pts, fill=fill)


def _flower(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, petal: RGB
) -> None:
    for i in range(6):
        ang = i * math.pi / 3
        px, py = cx + r * math.cos(ang), cy + r * math.sin(ang)
        draw.ellipse([px - r * 0.7, py - r * 0.7, px + r * 0.7, py + r * 0.7], petal)
    draw.ellipse(
        [cx - r * 0.6, cy - r * 0.6, cx + r * 0.6, cy + r * 0.6], (255, 214, 64)
    )


def _finish(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Downsample a 2x canvas to its final size, which anti-aliases the shapes."""
    return img.resize(size, Image.Resampling.LANCZOS)


def scene_sunset(size: tuple[int, int], rng: random.Random) -> Image.Image:
    """Sun setting over rolling hills."""
    w, h = size
    img = _gradient(size, [(72, 52, 140), (236, 110, 110), (255, 190, 110)])
    draw = ImageDraw.Draw(img)
    draw.ellipse([w * 0.55, h * 0.38, w * 0.75, h * 0.38 + w * 0.2], (255, 236, 150))
    for base, color in [
        (0.62, (126, 72, 110)),
        (0.72, (84, 56, 96)),
        (0.84, (48, 36, 70)),
    ]:
        _hills(draw, size, base, 0.05, color, rng)
    return img


def scene_night(size: tuple[int, int], rng: random.Random) -> Image.Image:
    """Starry night with a crescent moon over a dark meadow."""
    w, h = size
    img = _gradient(size, [(10, 14, 40), (28, 40, 96), (60, 70, 130)])
    draw = ImageDraw.Draw(img)
    for _ in range(90):
        x, y, r = rng.uniform(0, w), rng.uniform(0, h * 0.7), rng.uniform(1, 4)
        draw.ellipse([x - r, y - r, x + r, y + r], (255, 250, 220))
    for _ in range(4):
        _star(
            draw, rng.uniform(0, w), rng.uniform(0, h * 0.5), w * 0.02, (255, 236, 150)
        )
    mx, my, mr = w * 0.78, h * 0.22, w * 0.08
    draw.ellipse([mx - mr, my - mr, mx + mr, my + mr], (255, 244, 200))
    draw.ellipse(
        [mx - mr * 0.55, my - mr * 1.1, mx + mr * 1.35, my + mr * 0.8], (22, 30, 72)
    )
    _hills(draw, size, 0.82, 0.04, (14, 38, 40), rng)
    return img


def scene_sailboat(size: tuple[int, int], rng: random.Random) -> Image.Image:
    """A small sailboat on the sea."""
    w, h = size
    img = _gradient(size, [(120, 190, 240), (200, 232, 250)])
    draw = ImageDraw.Draw(img)
    draw.ellipse([w * 0.08, h * 0.08, w * 0.22, h * 0.08 + w * 0.14], (255, 222, 90))
    sea = _gradient((w, int(h * 0.45)), [(40, 120, 190), (18, 70, 130)])
    img.paste(sea, (0, int(h * 0.55)))
    for _ in range(40):
        x, y = rng.uniform(0, w), rng.uniform(h * 0.58, h)
        draw.arc([x, y, x + w * 0.05, y + h * 0.02], 200, 340, (200, 230, 250), 3)
    bx, by = w * 0.45, h * 0.62
    draw.polygon(
        [
            (bx - w * 0.12, by),
            (bx + w * 0.14, by),
            (bx + w * 0.1, by + h * 0.06),
            (bx - w * 0.09, by + h * 0.06),
        ],
        (180, 60, 50),
    )
    draw.line([(bx, by), (bx, by - h * 0.36)], (90, 60, 40), max(2, w // 200))
    draw.polygon(
        [(bx + 4, by - h * 0.35), (bx + 4, by - 6), (bx + w * 0.14, by - 6)],
        (255, 255, 255),
    )
    draw.polygon(
        [(bx - 4, by - h * 0.3), (bx - 4, by - 6), (bx - w * 0.1, by - 6)],
        (250, 210, 90),
    )
    return img


def scene_meadow(size: tuple[int, int], rng: random.Random) -> Image.Image:
    """A meadow full of flowers."""
    w, h = size
    img = _gradient(size, [(150, 210, 250), (220, 240, 250)])
    draw = ImageDraw.Draw(img)
    for cx in (0.2, 0.62):
        for dx, dy, r in [(0, 0, 0.06), (0.06, -0.02, 0.07), (0.12, 0, 0.05)]:
            x, y, rr = w * (cx + dx), h * (0.18 + dy), w * r
            draw.ellipse([x - rr, y - rr * 0.7, x + rr, y + rr * 0.7], (255, 255, 255))
    _hills(draw, size, 0.5, 0.03, (120, 190, 90), rng)
    _hills(draw, size, 0.62, 0.03, (86, 160, 70), rng)
    petals = [(240, 90, 120), (250, 160, 60), (180, 110, 230), (255, 255, 255)]
    for _ in range(70):
        y = rng.uniform(h * 0.6, h * 1.02)
        x = rng.uniform(0, w)
        r = w * 0.008 + (y / h) * w * 0.018
        draw.line([(x, y), (x, y + r * 3)], (50, 120, 50), max(2, int(r / 4)))
        _flower(draw, x, y, r, rng.choice(petals))
    return img


def scene_rainbow(size: tuple[int, int], rng: random.Random) -> Image.Image:
    """A rainbow after the rain."""
    w, h = size
    img = _gradient(size, [(160, 200, 235), (230, 240, 250)])
    draw = ImageDraw.Draw(img)
    bands = [
        (230, 60, 60),
        (245, 150, 50),
        (245, 220, 70),
        (90, 190, 90),
        (70, 140, 220),
        (130, 90, 200),
    ]
    cx, cy, r0, band = w * 0.5, h * 0.95, w * 0.46, w * 0.03
    for i, color in enumerate(bands):
        r = r0 - i * band
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], color)
    r = r0 - len(bands) * band
    inner = _gradient((w, h), [(160, 200, 235), (230, 240, 250)])
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse([cx - r, cy - r, cx + r, cy + r], 255)
    img.paste(inner, (0, 0), mask)
    draw = ImageDraw.Draw(img)
    _hills(draw, size, 0.8, 0.03, (100, 175, 80), rng)
    for x0 in (0.05, 0.7):
        for dx, r in [(0, 0.05), (0.06, 0.07), (0.13, 0.05)]:
            x, y, rr = w * (x0 + dx), h * 0.2, w * r
            draw.ellipse([x - rr, y - rr * 0.7, x + rr, y + rr * 0.7], (250, 250, 255))
    return img


def scene_mountains(size: tuple[int, int], rng: random.Random) -> Image.Image:
    """Snowy mountains under a clear sky."""
    w, h = size
    img = _gradient(size, [(90, 150, 220), (190, 220, 245)])
    draw = ImageDraw.Draw(img)
    for peak_x, peak_h, base_w, color in [
        (0.25, 0.28, 0.5, (110, 120, 150)),
        (0.62, 0.18, 0.6, (90, 100, 135)),
        (0.9, 0.34, 0.4, (120, 130, 160)),
    ]:
        px, py, bw = w * peak_x, h * peak_h, w * base_w
        draw.polygon([(px - bw / 2, h * 0.8), (px, py), (px + bw / 2, h * 0.8)], color)
        snow = (py + h * 0.12 - py) / (h * 0.8 - py)
        sx = bw / 2 * snow
        draw.polygon(
            [(px - sx, py + h * 0.12), (px, py), (px + sx, py + h * 0.12)],
            (250, 250, 255),
        )
    _hills(draw, size, 0.8, 0.02, (60, 130, 80), rng)
    for _ in range(26):
        x, y = rng.uniform(0, w), rng.uniform(h * 0.82, h * 0.98)
        t = h * 0.07
        draw.polygon([(x - t / 3, y), (x, y - t), (x + t / 3, y)], (30, 90, 55))
    return img


Scene = Callable[[tuple[int, int], random.Random], Image.Image]


def render_scene(scene: Scene, size: tuple[int, int], seed: int) -> Image.Image:
    """Draw a scene at 2x and downsample it.

    Args:
        scene: One of the ``scene_*`` functions.
        size: Final image size.
        seed: Seed for the scene's random details.

    Returns:
        The finished RGB image.
    """
    big = (size[0] * 2, size[1] * 2)
    return _finish(scene(big, random.Random(seed)), size)


def cover_art(motif: Scene, seed: int) -> bytes:
    """Draw square cover art from a scene.

    Args:
        motif: Scene drawn as the cover.
        seed: Seed for the scene's random details.

    Returns:
        JPEG bytes (300x300).
    """
    return _jpeg(render_scene(motif, (300, 300), seed), 85)


def _jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Song:
    """One synthesized track."""

    filename: str
    title: str
    melody: Tune
    bass: Tune
    bpm: float
    motif: Scene


@dataclass(frozen=True)
class Fable:
    """One read-aloud audiobook chapter."""

    filename: str
    title: str
    text: str
    motif: Scene


@dataclass(frozen=True)
class Photo:
    """One procedurally drawn photo."""

    filename: str
    scene: Scene


ALBUMS: dict[str, list[Song]] = {
    "Nursery Tunes": [
        Song(
            "01-twinkle.mp3",
            "Twinkle, Twinkle",
            TWINKLE,
            TWINKLE_BASS,
            132,
            scene_night,
        ),
        Song(
            "02-frere-jacques.mp3",
            "Frère Jacques",
            FRERE_JACQUES,
            FRERE_JACQUES_BASS,
            150,
            scene_meadow,
        ),
        Song(
            "03-mary-lamb.mp3",
            "Mary Had a Little Lamb",
            MARY_LAMB,
            MARY_LAMB_BASS,
            144,
            scene_rainbow,
        ),
    ],
    "Bedtime Classics": [
        Song(
            "01-lullaby.mp3",
            "Brahms' Lullaby",
            LULLABY,
            LULLABY_BASS,
            120,
            scene_sunset,
        ),
        Song(
            "02-ode-to-joy.mp3",
            "Ode to Joy",
            ODE_TO_JOY,
            ODE_TO_JOY_BASS,
            132,
            scene_mountains,
        ),
    ],
}

# Aesop's Fables, translated by George Fyler Townsend (1867); public domain.
AUDIOBOOKS: dict[str, list[Fable]] = {
    "Aesop's Fables": [
        Fable(
            "01-hare-and-tortoise.mp3",
            "The Hare and the Tortoise",
            "The Hare and the Tortoise. "
            "A Hare one day ridiculed the short feet and slow pace of the "
            "Tortoise, who replied, laughing: Though you be swift as the wind, "
            "I will beat you in a race. The Hare, believing her assertion to be "
            "simply impossible, assented to the proposal. On the day appointed "
            "for the race the two started together. The Tortoise never for a "
            "moment stopped, but went on with a slow but steady pace straight "
            "to the end of the course. The Hare, lying down by the wayside, "
            "fell fast asleep. At last waking up, and moving as fast as he "
            "could, he saw the Tortoise had reached the goal, and was "
            "comfortably dozing after her fatigue. Slow but steady wins the "
            "race.",
            scene_meadow,
        ),
        Fable(
            "02-lion-and-mouse.mp3",
            "The Lion and the Mouse",
            "The Lion and the Mouse. "
            "A Lion was awakened from sleep by a Mouse running over his face. "
            "Rising up angrily, he caught him and was about to kill him, when "
            "the Mouse piteously entreated, saying: If you would only spare "
            "my life, I would be sure to repay your kindness. The Lion laughed "
            "and let him go. It happened shortly after this that the Lion was "
            "caught by some hunters, who bound him by strong ropes to the "
            "ground. The Mouse, recognizing his roar, came and gnawed the rope "
            "with his teeth, and set him free. Little friends may prove great "
            "friends.",
            scene_sunset,
        ),
    ],
}

PHOTO_ALBUMS: dict[str, list[Photo]] = {
    "Day Trips": [
        Photo("beach-sailboat.jpg", scene_sailboat),
        Photo("mountain-hike.jpg", scene_mountains),
        Photo("sunset-drive.jpg", scene_sunset),
    ],
    "Backyard": [
        Photo("flower-meadow.jpg", scene_meadow),
        Photo("rainbow.jpg", scene_rainbow),
        Photo("starry-night.jpg", scene_night),
    ],
}

PHOTO_SIZE = (960, 720)


SUBDIRS = ("music", "audiobooks", "photos")

# What generate() needs from ffmpeg: (listing flag, name) pairs.
_FFMPEG_FEATURES = (("-filters", "flite"), ("-encoders", "libmp3lame"))


def missing_ffmpeg_features() -> list[str]:
    """Return the ffmpeg features this script needs that the build lacks.

    Returns:
        Names such as ``"flite"``; empty when ffmpeg can do everything.
    """
    missing: list[str] = []
    for flag, name in _FFMPEG_FEATURES:
        listing = subprocess.run(
            ["ffmpeg", "-hide_banner", flag],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if not any(name in line.split() for line in listing.splitlines()):
            missing.append(name)
    return missing


def generate(out_dir: Path) -> None:
    """Regenerate every sample file under ``out_dir``.

    The new files are built in a scratch directory next to ``out_dir`` (same
    filesystem, so the swap is a rename; nothing hidden is ever left inside
    ``out_dir``) and only swapped in once all of them exist, so a failure (or a
    missing ffmpeg feature) never leaves ``out_dir`` without its media.

    Args:
        out_dir: Destination root; ``music/``, ``audiobooks/`` and ``photos/``
            are replaced.

    Raises:
        SystemExit: If ffmpeg is missing, or lacks ``flite`` or ``libmp3lame``.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required to generate the sample audio")
    missing = missing_ffmpeg_features()
    if missing:
        raise SystemExit(
            f"this ffmpeg lacks {', '.join(missing)}, which the sample media "
            "needs. Homebrew's ffmpeg has no flite: generate on Debian/Ubuntu "
            "(apt install ffmpeg), e.g. in a container. Nothing was changed."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".sample-media-", dir=out_dir.parent))
    try:
        _build(staging)
        _swap_in(staging, out_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _swap_in(staging: Path, out_dir: Path) -> None:
    """Replace ``out_dir``'s media directories with the ones built in ``staging``.

    Each old directory is moved aside, not deleted, until its replacement is in
    place; if a move fails the old ones are put back.
    """
    aside = staging / ".old"
    aside.mkdir()
    moved: list[str] = []
    try:
        for sub in SUBDIRS:
            if (out_dir / sub).exists():
                (out_dir / sub).rename(aside / sub)
                moved.append(sub)
            (staging / sub).rename(out_dir / sub)
    except OSError:
        for sub in moved:
            shutil.rmtree(out_dir / sub, ignore_errors=True)
            (aside / sub).rename(out_dir / sub)
        raise


def _build(out_dir: Path) -> None:
    """Write every sample file under ``out_dir`` (an empty scratch directory)."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "tune.wav"
        for album, songs in ALBUMS.items():
            album_dir = out_dir / "music" / album
            album_dir.mkdir(parents=True)
            for n, song in enumerate(songs, 1):
                write_wav(render_tune(song.melody, song.bass, song.bpm), wav)
                dest = album_dir / song.filename
                encode_mp3(wav, dest, "64k")
                cover = cover_art(song.motif, seed=n)
                tag_mp3(
                    dest,
                    title=song.title,
                    artist=ARTIST,
                    album=album,
                    track=n,
                    cover=cover,
                )
                print(f"  {dest.relative_to(out_dir)}")

    for book, fables in AUDIOBOOKS.items():
        book_dir = out_dir / "audiobooks" / book
        book_dir.mkdir(parents=True)
        for n, fable in enumerate(fables, 1):
            dest = book_dir / fable.filename
            text = fable.text.replace("'", "").replace(":", ",")
            _ffmpeg(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"flite=text='{text}':voice=slt",
                    *_mp3_args("48k"),
                    str(dest),
                ]
            )
            cover = cover_art(fable.motif, seed=10 + n)
            tag_mp3(
                dest,
                title=fable.title,
                artist=NARRATOR,
                album=book,
                track=n,
                cover=cover,
            )
            print(f"  {dest.relative_to(out_dir)}")

    seed = 100
    for album, photos in PHOTO_ALBUMS.items():
        album_dir = out_dir / "photos" / album
        album_dir.mkdir(parents=True)
        for photo in photos:
            seed += 1
            dest = album_dir / photo.filename
            dest.write_bytes(_jpeg(render_scene(photo.scene, PHOTO_SIZE, seed), 88))
            print(f"  {dest.relative_to(out_dir)}")


def main() -> None:
    """Regenerate ``demo/media/`` and print the total size."""
    print(f"Generating sample media in {MEDIA_DIR}")
    generate(MEDIA_DIR)
    total = sum(p.stat().st_size for p in MEDIA_DIR.rglob("*") if p.is_file())
    print(f"Total: {total / 1_000_000:.2f} MB")


if __name__ == "__main__":
    main()
