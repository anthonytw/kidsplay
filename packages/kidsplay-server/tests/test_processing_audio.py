"""Tests for kidsplay_server.processing.audio.

Uses real files on disk — no mocking. Test MP3s are created with a minimal
valid MPEG1 Layer3 frame so mutagen can detect format and duration, plus
ID3 tags written by mutagen's ID3 API.

A single MPEG1/Layer3/128kbps/44100Hz frame is 417 bytes:
    floor(144 * 128000 / 44100) = 417
After mutagen prepends an ID3v2 header, the file is a valid (tiny) MP3.
"""

import io
from pathlib import Path

import mutagen.id3
from PIL import Image

from kidsplay_server.processing.audio import extract_artwork, extract_metadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal silent MPEG1 Layer3 frame (128 kbps, 44100 Hz, stereo).
# Header bytes: 0xFF 0xFB = sync + MPEG1 + Layer3 + no CRC
#               0x90     = 128 kbps + 44100 Hz + no padding
#               0x00     = stereo + no emphasis
# Four silent MPEG1/Layer3/128kbps/44100Hz/stereo frames (417 bytes each).
# mutagen requires at least 2 consecutive valid frames before it accepts a
# sync; 4 frames triggers the "enough_frames" branch and sets sketchy=False.
_SINGLE_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
_MPEG_FRAME = _SINGLE_FRAME * 4


def _make_artwork_bytes(size: int = 8) -> bytes:
    """Return raw PNG bytes for a small solid-colour image."""
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color=(200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


def make_mp3(
    path: Path,
    *,
    title: str = "Test Title",
    artist: str = "Test Artist",
    album: str = "Test Album",
    with_artwork: bool = True,
) -> Path:
    """Write a minimal valid MP3 file with ID3 tags to ``path``.

    ID3 tags are written first (``ID3.save()`` creates the file), then a
    single silent MPEG1 Layer3 frame is physically appended. The resulting
    file structure — [ID3v2 header][MPEG frame] — is what mutagen expects
    when opening an MP3.
    """
    tags = mutagen.id3.ID3()
    tags.add(mutagen.id3.TIT2(encoding=3, text=[title]))
    tags.add(mutagen.id3.TPE1(encoding=3, text=[artist]))
    tags.add(mutagen.id3.TALB(encoding=3, text=[album]))
    tags.add(mutagen.id3.TRCK(encoding=3, text=["3/12"]))

    if with_artwork:
        tags.add(
            mutagen.id3.APIC(
                encoding=3,
                mime="image/png",
                type=3,
                desc="",
                data=_make_artwork_bytes(),
            )
        )

    tags.save(str(path))  # creates the file (or overwrites) with ID3 data only

    # Append the MPEG frame after the ID3 block so mutagen can detect duration.
    with path.open("ab") as f:
        f.write(_MPEG_FRAME)

    return path


# ---------------------------------------------------------------------------
# extract_metadata
# ---------------------------------------------------------------------------


class TestExtractMetadata:
    def test_extracts_title(self, tmp_path: Path) -> None:
        path = make_mp3(tmp_path / "song.mp3", title="La Bamba")
        meta = extract_metadata(path)
        assert meta["title"] == "La Bamba"

    def test_extracts_artist(self, tmp_path: Path) -> None:
        path = make_mp3(tmp_path / "song.mp3", artist="Los Lobos")
        meta = extract_metadata(path)
        assert meta["artist"] == "Los Lobos"

    def test_extracts_album(self, tmp_path: Path) -> None:
        path = make_mp3(tmp_path / "song.mp3", album="Ritchie Valens")
        meta = extract_metadata(path)
        assert meta["album"] == "Ritchie Valens"

    def test_extracts_track_number(self, tmp_path: Path) -> None:
        path = make_mp3(tmp_path / "song.mp3")
        meta = extract_metadata(path)
        assert meta["track_number"] == 3

    def test_extracts_duration(self, tmp_path: Path) -> None:
        path = make_mp3(tmp_path / "song.mp3")
        meta = extract_metadata(path)
        # One MPEG frame ≈ 26 ms → ceil gives 1 second
        assert meta["duration_seconds"] is not None
        assert meta["duration_seconds"] >= 1

    def test_fallback_title_from_filename(self, tmp_path: Path) -> None:
        """When no title tag is present, filename stem is used."""
        path = tmp_path / "my_favourite_song.mp3"
        path.write_bytes(_MPEG_FRAME)  # no ID3 tags
        meta = extract_metadata(path)
        assert meta["title"] == "my_favourite_song"

    def test_fallback_album_from_parent_directory(self, tmp_path: Path) -> None:
        """When no album tag is present, parent directory name is used."""
        subdir = tmp_path / "Pica-Pica"
        subdir.mkdir()
        path = subdir / "song.mp3"
        path.write_bytes(_MPEG_FRAME)
        meta = extract_metadata(path)
        assert meta["album"] == "Pica-Pica"

    def test_missing_artist_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "song.mp3"
        path.write_bytes(_MPEG_FRAME)
        meta = extract_metadata(path)
        assert meta["artist"] is None

    def test_unreadable_file_returns_fallbacks(self, tmp_path: Path) -> None:
        """A file that mutagen cannot open falls back gracefully — no crash."""
        path = tmp_path / "corrupt.mp3"
        path.write_bytes(b"\x00" * 10)
        meta = extract_metadata(path)
        # Must not raise; fallback to filename
        assert meta["title"] == "corrupt"
        assert meta["artist"] is None
        assert meta["duration_seconds"] is None

    def test_returns_all_expected_keys(self, tmp_path: Path) -> None:
        path = make_mp3(tmp_path / "song.mp3")
        meta = extract_metadata(path)
        assert set(meta.keys()) == {
            "title",
            "artist",
            "album",
            "duration_seconds",
            "track_number",
        }


# ---------------------------------------------------------------------------
# extract_artwork
# ---------------------------------------------------------------------------


class TestExtractArtwork:
    def test_returns_bytes_when_artwork_present(self, tmp_path: Path) -> None:
        path = make_mp3(tmp_path / "song.mp3", with_artwork=True)
        data = extract_artwork(path)
        assert data is not None
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_returned_bytes_are_valid_image(self, tmp_path: Path) -> None:
        """The extracted bytes can be opened by Pillow."""
        path = make_mp3(tmp_path / "song.mp3", with_artwork=True)
        data = extract_artwork(path)
        assert data is not None
        img = Image.open(io.BytesIO(data))
        assert img.size == (8, 8)  # matches _make_artwork_bytes() size

    def test_returns_none_when_no_artwork(self, tmp_path: Path) -> None:
        path = make_mp3(tmp_path / "song.mp3", with_artwork=False)
        assert extract_artwork(path) is None

    def test_returns_none_for_unreadable_file(self, tmp_path: Path) -> None:
        path = tmp_path / "garbage.mp3"
        path.write_bytes(b"\x00" * 4)
        assert extract_artwork(path) is None

    def test_returns_none_for_nonexistent_file(self, tmp_path: Path) -> None:
        assert extract_artwork(tmp_path / "missing.mp3") is None
