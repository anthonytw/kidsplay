"""Tests for kidsplay_server.processing.images.

All tests use real Pillow images and a real MediaStore in tmp_path.
No mocking.

Source image conventions used throughout:
  - 600×600 PNG — square, larger than all thumbnail sizes → all three
    thumbnail sizes exercise the shrink path.
  - 800×600 PNG — 4:3 landscape → exercise aspect-ratio preservation.
  - 320×240 PNG — smaller than MEDIUM/LARGE thumbnail bounds → those
    sizes should NOT upscale.
"""

import io
from pathlib import Path

from PIL import Image

from kidsplay_models.processing import ThumbnailSize
from kidsplay_server.processing.images import generate_thumbnails, process_photo
from kidsplay_server.storage import MediaStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_png(width: int, height: int) -> bytes:
    """Return raw PNG bytes for a solid-colour image of the given dimensions."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(80, 140, 200)).save(buf, format="PNG")
    return buf.getvalue()


def make_png_file(path: Path, width: int, height: int) -> Path:
    path.write_bytes(make_png(width, height))
    return path


def read_webp_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) of a WebP file."""
    with Image.open(path) as img:
        return img.size


# ---------------------------------------------------------------------------
# generate_thumbnails
# ---------------------------------------------------------------------------


class TestGenerateThumbnails:
    def test_returns_one_entry_per_size(self, store: MediaStore) -> None:
        sizes = [ThumbnailSize.SMALL, ThumbnailSize.MEDIUM]
        result = generate_thumbnails(make_png(600, 600), sizes, store)
        assert len(result) == 2

    def test_returns_all_three_sizes(self, store: MediaStore) -> None:
        result = generate_thumbnails(make_png(600, 600), list(ThumbnailSize), store)
        assert len(result) == 3

    def test_result_order_matches_input_sizes(self, store: MediaStore) -> None:
        sizes = [ThumbnailSize.LARGE, ThumbnailSize.SMALL, ThumbnailSize.MEDIUM]
        result = generate_thumbnails(make_png(600, 600), sizes, store)
        returned_sizes = [size for size, _, _ in result]
        assert returned_sizes == sizes

    def test_thumbnails_exist_in_store(self, store: MediaStore) -> None:
        result = generate_thumbnails(make_png(600, 600), list(ThumbnailSize), store)
        for _, _, rel_path in result:
            assert store.exists(rel_path), f"Missing file: {rel_path}"

    def test_small_thumbnail_dimensions(self, store: MediaStore) -> None:
        """600×600 source → 60×60 small thumbnail (exact fit, square source)."""
        result = generate_thumbnails(make_png(600, 600), [ThumbnailSize.SMALL], store)
        _, _, rel = result[0]
        w, h = read_webp_dimensions(store.get_absolute_path(rel))
        assert w <= 60 and h <= 60
        assert w == 60 or h == 60  # at least one axis fills the bound

    def test_medium_thumbnail_dimensions(self, store: MediaStore) -> None:
        result = generate_thumbnails(make_png(600, 600), [ThumbnailSize.MEDIUM], store)
        _, _, rel = result[0]
        w, h = read_webp_dimensions(store.get_absolute_path(rel))
        assert w <= 200 and h <= 200
        assert w == 200 or h == 200

    def test_large_thumbnail_dimensions(self, store: MediaStore) -> None:
        result = generate_thumbnails(make_png(600, 600), [ThumbnailSize.LARGE], store)
        _, _, rel = result[0]
        w, h = read_webp_dimensions(store.get_absolute_path(rel))
        assert w <= 480 and h <= 480
        assert w == 480 or h == 480

    def test_aspect_ratio_preserved_for_non_square(self, store: MediaStore) -> None:
        """800×600 source: thumbnail width/height ratio should match 4:3."""
        result = generate_thumbnails(make_png(800, 600), [ThumbnailSize.SMALL], store)
        _, _, rel = result[0]
        w, h = read_webp_dimensions(store.get_absolute_path(rel))
        # 800×600 at 60×60 bound → 60×45
        assert w == 60
        assert h == 45

    def test_small_source_not_upscaled(self, store: MediaStore) -> None:
        """320×240 source must not grow beyond its original size."""
        result = generate_thumbnails(make_png(320, 240), [ThumbnailSize.MEDIUM], store)
        _, _, rel = result[0]
        w, h = read_webp_dimensions(store.get_absolute_path(rel))
        assert w <= 320 and h <= 240

    def test_output_is_webp(self, store: MediaStore) -> None:
        result = generate_thumbnails(make_png(600, 600), [ThumbnailSize.SMALL], store)
        _, _, rel = result[0]
        path = store.get_absolute_path(rel)
        with Image.open(path) as img:
            assert img.format == "WEBP"

    def test_relative_path_format(self, store: MediaStore) -> None:
        """Path should match thumbnails/{hash[:2]}/{hash}_{size}.webp."""
        result = generate_thumbnails(make_png(600, 600), [ThumbnailSize.MEDIUM], store)
        content_hash, rel = result[0][1], result[0][2]
        expected = f"thumbnails/{content_hash[:2]}/{content_hash}_200x200.webp"
        assert rel == expected

    def test_content_hash_is_sha256_hex(self, store: MediaStore) -> None:
        result = generate_thumbnails(make_png(600, 600), [ThumbnailSize.SMALL], store)
        content_hash = result[0][1]
        assert len(content_hash) == 64
        assert all(c in "0123456789abcdef" for c in content_hash)

    def test_idempotent_same_bytes(self, store: MediaStore) -> None:
        """Calling twice with identical bytes returns identical results."""
        data = make_png(200, 200)
        r1 = generate_thumbnails(data, [ThumbnailSize.SMALL], store)
        r2 = generate_thumbnails(data, [ThumbnailSize.SMALL], store)
        assert r1[0][1] == r2[0][1]  # same hash
        assert r1[0][2] == r2[0][2]  # same path

    def test_different_bytes_different_hashes(self, store: MediaStore) -> None:
        # Use visually distinct images so thumbnails have different content.
        buf1 = io.BytesIO()
        buf2 = io.BytesIO()
        Image.new("RGB", (200, 200), (255, 0, 0)).save(buf1, format="PNG")
        Image.new("RGB", (200, 200), (0, 255, 0)).save(buf2, format="PNG")
        r1 = generate_thumbnails(buf1.getvalue(), [ThumbnailSize.SMALL], store)
        r2 = generate_thumbnails(buf2.getvalue(), [ThumbnailSize.SMALL], store)
        assert r1[0][1] != r2[0][1]

    def test_palette_image_handled(self, store: MediaStore) -> None:
        """Palette-mode (P) images are converted to RGB without crashing."""
        buf = io.BytesIO()
        img = Image.new("P", (100, 100))
        img.save(buf, format="PNG")
        result = generate_thumbnails(buf.getvalue(), [ThumbnailSize.SMALL], store)
        assert len(result) == 1
        assert store.exists(result[0][2])


# ---------------------------------------------------------------------------
# process_photo
# ---------------------------------------------------------------------------


class TestProcessPhoto:
    def test_returns_hash_and_path(self, store: MediaStore, tmp_path: Path) -> None:
        src = make_png_file(tmp_path / "photo.png", 400, 300)
        content_hash, rel_path = process_photo(src, 640, 480, store)
        assert isinstance(content_hash, str) and len(content_hash) == 64
        assert isinstance(rel_path, str) and rel_path.startswith("photos/")

    def test_file_exists_in_store(self, store: MediaStore, tmp_path: Path) -> None:
        src = make_png_file(tmp_path / "photo.png", 400, 300)
        _, rel_path = process_photo(src, 640, 480, store)
        assert store.exists(rel_path)

    def test_output_is_webp(self, store: MediaStore, tmp_path: Path) -> None:
        src = make_png_file(tmp_path / "photo.png", 400, 300)
        _, rel_path = process_photo(src, 640, 480, store)
        with Image.open(store.get_absolute_path(rel_path)) as img:
            assert img.format == "WEBP"

    def test_large_image_shrunk_to_fit(self, store: MediaStore, tmp_path: Path) -> None:
        """1280×960 (4:3) source → fits exactly in 640×480 (same ratio)."""
        src = make_png_file(tmp_path / "photo.png", 1280, 960)
        _, rel_path = process_photo(src, 640, 480, store)
        w, h = read_webp_dimensions(store.get_absolute_path(rel_path))
        assert w <= 640 and h <= 480

    def test_small_image_not_upscaled(self, store: MediaStore, tmp_path: Path) -> None:
        """320×240 photo must not be larger than 640×480 in output."""
        src = make_png_file(tmp_path / "photo.png", 320, 240)
        _, rel_path = process_photo(src, 640, 480, store)
        w, h = read_webp_dimensions(store.get_absolute_path(rel_path))
        assert w <= 320 and h <= 240

    def test_aspect_ratio_preserved(self, store: MediaStore, tmp_path: Path) -> None:
        """800×600 source at 640×480 bound → 640×480 (same 4:3 ratio)."""
        src = make_png_file(tmp_path / "photo.png", 800, 600)
        _, rel_path = process_photo(src, 640, 480, store)
        w, h = read_webp_dimensions(store.get_absolute_path(rel_path))
        assert w == 640
        assert h == 480

    def test_dimensions_encoded_in_path(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        """Relative path should end with _{width}x{height}.webp."""
        src = make_png_file(tmp_path / "photo.png", 800, 600)
        _, rel_path = process_photo(src, 640, 480, store)
        # 800×600 at 640×480 bound fits exactly
        assert rel_path.endswith("_640x480.webp")

    def test_idempotent(self, store: MediaStore, tmp_path: Path) -> None:
        src = make_png_file(tmp_path / "photo.png", 400, 300)
        h1, p1 = process_photo(src, 640, 480, store)
        h2, p2 = process_photo(src, 640, 480, store)
        assert h1 == h2 and p1 == p2

    def test_in_bounds_crop_applied(self, store: MediaStore, tmp_path: Path) -> None:
        """A crop fully inside the image is applied verbatim."""
        src = make_png_file(tmp_path / "photo.png", 400, 300)
        crop = {"x": 100.0, "y": 50.0, "width": 200.0, "height": 150.0}
        _, rel_path = process_photo(src, 640, 480, store, crop=crop)
        w, h = read_webp_dimensions(store.get_absolute_path(rel_path))
        assert (w, h) == (200, 150)

    def _assert_no_black_padding(self, path: Path) -> None:
        """Every corner should be the source colour, never black padding."""
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            w, h = rgb.size
            for px in (
                rgb.getpixel((0, 0)),
                rgb.getpixel((w - 1, 0)),
                rgb.getpixel((0, h - 1)),
                rgb.getpixel((w - 1, h - 1)),
            ):
                # Source is (80, 140, 200); black padding would be ~(0, 0, 0).
                assert isinstance(px, tuple)
                assert sum(px) > 120, f"corner looks like padding: {px}"

    def test_crop_past_edge_is_clamped(self, store: MediaStore, tmp_path: Path) -> None:
        """A crop extending past the right/bottom edge is clamped, no padding."""
        src = make_png_file(tmp_path / "photo.png", 400, 300)
        # Requests a 400×300 box starting at (300,200): only 100×100 is inside.
        crop = {"x": 300.0, "y": 200.0, "width": 400.0, "height": 300.0}
        _, rel_path = process_photo(src, 640, 480, store, crop=crop)
        w, h = read_webp_dimensions(store.get_absolute_path(rel_path))
        assert (w, h) == (100, 100)
        self._assert_no_black_padding(store.get_absolute_path(rel_path))

    def test_crop_negative_offset_is_clamped(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        """A crop starting before the origin is clamped to (0,0), no padding."""
        src = make_png_file(tmp_path / "photo.png", 400, 300)
        crop = {"x": -50.0, "y": -50.0, "width": 200.0, "height": 200.0}
        _, rel_path = process_photo(src, 640, 480, store, crop=crop)
        w, h = read_webp_dimensions(store.get_absolute_path(rel_path))
        assert (w, h) == (200, 200)
        self._assert_no_black_padding(store.get_absolute_path(rel_path))


class TestWebpQuality:
    def test_lower_quality_smaller_thumbnail(self, store: MediaStore) -> None:
        buf = io.BytesIO()
        Image.effect_noise((480, 480), 64).convert("RGB").save(buf, format="PNG")
        sizes = [ThumbnailSize.LARGE]
        ((_, _, high_rel),) = generate_thumbnails(buf.getvalue(), sizes, store)
        ((_, _, low_rel),) = generate_thumbnails(
            buf.getvalue(), sizes, store, quality=5
        )
        high = (store.root / high_rel).stat().st_size
        low = (store.root / low_rel).stat().st_size
        assert low < high

    def test_lower_quality_smaller_photo(
        self, store: MediaStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "noise.png"
        Image.effect_noise((640, 480), 64).convert("RGB").save(src, format="PNG")
        _, high_rel = process_photo(src, 640, 480, store)
        _, low_rel = process_photo(src, 640, 480, store, quality=5)
        high = (store.root / high_rel).stat().st_size
        low = (store.root / low_rel).stat().st_size
        assert low < high
