"""Validation and storage of theme assets (background images, font, UI sounds).

Uploads are checked here, before they reach the media store, because the
device loads them without any further care: a broken file would only be
skipped at runtime, so the parent gets the error at upload time instead.

- Background images are re-encoded as RGB WebP no larger than 1280×720 (never
  upscaled), so a phone photo does not become a 10 MB file for a 640×480
  screen. The device scales the result to its own screen once, when the theme
  is applied.
- Fonts must be TrueType/OpenType files that FreeType can open. Whether a font
  may be redistributed is up to whoever uploads it.
- Sounds must be Ogg Vorbis or WAV, at most ``MAX_SOUND_SECONDS`` long.

Stored files are content-addressed under ``themes/`` like all media, and like
all media they are never modified or deleted in place.
"""

import io
import tempfile
from pathlib import Path

import mutagen
import mutagen.oggvorbis
import mutagen.wave
from PIL import Image, ImageFont, UnidentifiedImageError

from kidsplay_models import ThemeAsset, ThemeAssetRole
from kidsplay_server.storage import MediaStore

STORE_SUBDIR = "themes"
"""Where theme assets live in the media store."""

MAX_IMAGE_SIZE = (1280, 720)
MAX_IMAGE_SIDE = 4096
"""Longest side of an uploaded image, in pixels: generous for the largest
screen a device supports, since the image is downscaled to ``MAX_IMAGE_SIZE``."""
MAX_IMAGE_PIXELS = MAX_IMAGE_SIDE * MAX_IMAGE_SIDE
"""Largest uploaded image, in pixels (4096×4096). A small file can decode to
gigabytes, so the size is checked from the header before any pixels are read.
It is far below Pillow's own warning threshold (about 89 million), so Pillow's
decompression-bomb checks stay a second line of defence."""
MAX_UPLOAD_BYTES: dict[str, int] = {
    "image": 10 * 1024 * 1024,
    "font": 8 * 1024 * 1024,
    "sound": 2 * 1024 * 1024,
}
MAX_SOUND_SECONDS = 10.0

_IMAGE_ROLES = {ThemeAssetRole.BACKGROUND, ThemeAssetRole.HOME_BACKGROUND}
_SOUND_ROLES = {
    ThemeAssetRole.SOUND_MOVE,
    ThemeAssetRole.SOUND_SELECT,
    ThemeAssetRole.SOUND_BACK,
    ThemeAssetRole.SOUND_OPEN,
}
_FONT_MAGIC = {
    b"\x00\x01\x00\x00": ".ttf",
    b"true": ".ttf",
    b"OTTO": ".otf",
    b"ttcf": ".ttf",
}
_FONT_MIME = {".ttf": "font/ttf", ".otf": "font/otf"}


class ThemeAssetError(ValueError):
    """An uploaded theme asset is unusable; the message says why."""


def kind_of(role: ThemeAssetRole) -> str:
    """The upload size class of a role: ``image``, ``font`` or ``sound``.

    Args:
        role: The asset's role.

    Returns:
        The key into ``MAX_UPLOAD_BYTES``.
    """
    if role in _IMAGE_ROLES:
        return "image"
    if role in _SOUND_ROLES:
        return "sound"
    return "font"


def store_theme_asset(
    role: ThemeAssetRole, data: bytes, store: MediaStore, webp_quality: int
) -> tuple[ThemeAsset, str]:
    """Validate an uploaded asset and put it in the media store.

    Args:
        role: What the asset is for.
        data: The uploaded bytes.
        store: The media store.
        webp_quality: Encode quality for background images.

    Returns:
        The stored asset and its MIME type.

    Raises:
        ThemeAssetError: If the file is too large or not a usable file of the
            kind the role needs.

    Note:
        Blocking: it decodes images and parses fonts and sounds, and writes the
        store. Async callers run it in a worker thread (``asyncio.to_thread``).
    """
    kind = kind_of(role)
    if len(data) > MAX_UPLOAD_BYTES[kind]:
        limit = MAX_UPLOAD_BYTES[kind] // (1024 * 1024)
        raise ThemeAssetError(f"The {kind} is larger than {limit} MB")
    if not data:
        raise ThemeAssetError("The file is empty")
    if kind == "image":
        payload, suffix, mime = (
            _process_image(data, webp_quality),
            ".webp",
            "image/webp",
        )
    elif kind == "font":
        suffix = _check_font(data)
        payload, mime = data, _FONT_MIME[suffix]
    else:
        suffix, mime = _check_sound(data)
        payload = data
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"asset{suffix}"
        source.write_bytes(payload)
        content_hash, rel = store.store(source, STORE_SUBDIR, suffix)
    size = store.get_absolute_path(rel).stat().st_size
    asset = ThemeAsset(
        role=role, content_hash=content_hash, relative_path=rel, size_bytes=size
    )
    return asset, mime


def _process_image(data: bytes, quality: int) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as img:
            # Only the header has been read so far: refuse a decompression bomb
            # before allocating any pixels.
            width, height = img.size
            if max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                raise ThemeAssetError(
                    f"The image is too large ({width}×{height} pixels); "
                    f"the limit is {MAX_IMAGE_SIDE}×{MAX_IMAGE_SIDE}"
                )
            img.load()
            rgb = img.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise ThemeAssetError("The image is too large to process") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise ThemeAssetError("The file is not a readable image") from exc
    rgb.thumbnail(MAX_IMAGE_SIZE, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    rgb.save(out, "WEBP", quality=quality)
    return out.getvalue()


def _check_font(data: bytes) -> str:
    suffix = _FONT_MAGIC.get(data[:4])
    if suffix is None:
        raise ThemeAssetError("The file is not a TrueType or OpenType font")
    try:
        ImageFont.truetype(io.BytesIO(data), 20)
    except OSError as exc:
        raise ThemeAssetError("The font cannot be opened") from exc
    return suffix


def _check_sound(data: bytes) -> tuple[str, str]:
    try:
        if data[:4] == b"OggS":
            suffix, mime = ".ogg", "audio/ogg"
            ogg_info = mutagen.oggvorbis.OggVorbis(io.BytesIO(data)).info
            if not isinstance(ogg_info, mutagen.oggvorbis.OggVorbisInfo):
                raise ThemeAssetError("The sound cannot be read")
            length = ogg_info.length
        elif data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            suffix, mime = ".wav", "audio/wav"
            wav_info = mutagen.wave.WAVE(io.BytesIO(data)).info
            if not isinstance(wav_info, mutagen.wave.WaveStreamInfo):
                raise ThemeAssetError("The sound cannot be read")
            length = wav_info.length
        else:
            raise ThemeAssetError("The sound must be an Ogg Vorbis or a WAV file")
    except mutagen.MutagenError as exc:
        raise ThemeAssetError("The sound cannot be read") from exc
    if length > MAX_SOUND_SECONDS:
        raise ThemeAssetError(
            f"The sound is longer than {MAX_SOUND_SECONDS:.0f} seconds"
        )
    return suffix, mime
