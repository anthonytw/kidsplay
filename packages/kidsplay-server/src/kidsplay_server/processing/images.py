"""Server-side image processing: thumbnail generation and photo resizing.

All output is WebP for ~30% size savings vs JPEG at equivalent quality.
Thumbnails maintain aspect ratio within the requested bounding box using
Pillow's ``Image.thumbnail()``, which never upscales.

Typical usage:
    from kidsplay_server.processing.images import generate_thumbnails, process_photo
    from kidsplay_models.processing import ThumbnailSize

    thumbs = generate_thumbnails(artwork_bytes, list(ThumbnailSize), store)
    for size, content_hash, rel_path in thumbs:
        ...

    photo_hash, photo_path = process_photo(path, 640, 480, store)
"""

import io
import logging
import tempfile
from pathlib import Path

from PIL import Image

from kidsplay_models.processing import ThumbnailSize
from kidsplay_server.storage import MediaStore

logger = logging.getLogger(__name__)

# Default WebP encode quality. 85 gives a good size/quality trade-off and is
# well within what pygame-ce can display cleanly on a 640x480 screen. The
# pipeline passes the server's ``webp_quality`` setting instead.
_WEBP_QUALITY = 85


def _to_rgb(img: Image.Image) -> Image.Image:
    """Convert an image to RGB if needed for WebP encoding.

    WebP supports both RGB and RGBA. Palette (P) and other exotic modes
    are converted to RGB to avoid encode errors.

    Args:
        img: Input Pillow image, any mode.

    Returns:
        The same image if mode is already RGB or RGBA, otherwise a new
        RGB image.
    """
    if img.mode in ("RGB", "RGBA"):
        return img
    return img.convert("RGB")


def generate_thumbnails(
    image_bytes: bytes,
    sizes: list[ThumbnailSize],
    media_store: MediaStore,
    *,
    quality: int = _WEBP_QUALITY,
) -> list[tuple[ThumbnailSize, str, str]]:
    """Generate WebP thumbnails at each requested size and persist them.

    Uses ``Image.thumbnail()`` which shrinks to fit within the bounding box
    while maintaining aspect ratio. Images already smaller than the requested
    size are NOT upscaled.

    The suffix stored in the media store is ``_{width}x{height}.webp``
    (e.g. ``_200x200.webp``), matching the path format described in
    ``ARCHITECTURE.md``.

    Args:
        image_bytes: Raw image bytes (any format Pillow can open).
        sizes: Thumbnail sizes to generate. Typically all three
            ``ThumbnailSize`` values.
        media_store: Destination content-addressed store.
        quality: WebP encode quality, 1-100.

    Returns:
        List of ``(ThumbnailSize, content_hash, relative_path)`` tuples,
        one per requested size, in the same order as ``sizes``.
    """
    results: list[tuple[ThumbnailSize, str, str]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_base = Path(tmp_dir)

        with Image.open(io.BytesIO(image_bytes)) as img:
            img = _to_rgb(img)

            for size in sizes:
                thumb = img.copy()
                thumb.thumbnail(size.dimensions, Image.Resampling.LANCZOS)

                tmp_path = tmp_base / f"thumb_{size.value}.webp"
                thumb.save(tmp_path, format="WEBP", quality=quality)

                content_hash, rel_path = media_store.store(
                    tmp_path,
                    "thumbnails",
                    f"_{size.value}.webp",
                )
                results.append((size, content_hash, rel_path))

    return results


def process_photo(
    file_path: Path,
    max_width: int,
    max_height: int,
    media_store: MediaStore,
    *,
    crop: dict[str, float] | None = None,
    quality: int = _WEBP_QUALITY,
) -> tuple[str, str]:
    """Resize a photo to fit the device display and store it as WebP.

    If ``crop`` is supplied, the image is cropped to the given region
    before resizing.  Crop coordinates are pixel offsets on the original
    (un-resized) image, as produced by Cropper.js:
    ``{"x": float, "y": float, "width": float, "height": float}``.

    Uses ``Image.thumbnail()`` so images smaller than the target dimensions
    are NOT upscaled — they are only converted to WebP. Aspect ratio is
    always preserved.

    The stored filename suffix encodes the actual output dimensions,
    e.g. ``_640x480.webp`` or ``_480x480.webp`` for a square photo on a
    640x480 device.

    Args:
        file_path: Source image path (JPEG, PNG, WebP, BMP, or any Pillow-
            readable format).
        max_width: Maximum output width in pixels (device screen width).
        max_height: Maximum output height in pixels (device screen height).
        media_store: Destination content-addressed store.
        crop: Optional crop box ``{"x", "y", "width", "height"}`` in
            original-image pixel coordinates.  Applied before resizing.
        quality: WebP encode quality, 1-100.

    Returns:
        Tuple of ``(content_hash, relative_path)`` for the stored WebP file.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "photo.webp"

        with Image.open(file_path) as img:
            img = _to_rgb(img)
            if crop is not None:
                src_w, src_h = img.size
                # Clamp the crop box to the image bounds. A box that extends
                # past the edge (from a de-centered/zoomed-out crop in the UI)
                # would make Pillow pad the outside region with black, baking
                # asymmetric borders into the photo so it renders offset on the
                # device. Clamping keeps the crop fully inside the image.
                x = max(0, min(int(crop["x"]), src_w - 1))
                y = max(0, min(int(crop["y"]), src_h - 1))
                w = max(1, min(int(crop["width"]), src_w - x))
                h = max(1, min(int(crop["height"]), src_h - y))
                img = img.crop((x, y, x + w, y + h))
            img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            w, h = img.size
            img.save(tmp_path, format="WEBP", quality=quality)

        content_hash, rel_path = media_store.store(
            tmp_path,
            "photos",
            f"_{w}x{h}.webp",
        )

    return content_hash, rel_path
