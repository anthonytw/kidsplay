"""Acceptance checks on the committed sample media in ``demo/media/``.

Issue #11: the bundled media must stay under 5 MB, and every file's license
must be recorded. Every file must come from the generator's catalogue (so it
is covered by the CC0 dedication in ``THIRD_PARTY_NOTICES.md``); a file added
by hand fails here until its license is recorded and the test updated.
"""

from pathlib import Path

from demo.make_sample_media import ALBUMS, AUDIOBOOKS, MEDIA_DIR, PHOTO_ALBUMS

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_BUNDLE_BYTES = 5 * 1024 * 1024


def _bundled_files() -> list[Path]:
    return sorted(p for p in MEDIA_DIR.rglob("*") if p.is_file())


def _catalogue() -> set[str]:
    names = {
        f"music/{album}/{s.filename}" for album, songs in ALBUMS.items() for s in songs
    }
    names |= {
        f"audiobooks/{book}/{f.filename}"
        for book, fables in AUDIOBOOKS.items()
        for f in fables
    }
    names |= {
        f"photos/{album}/{p.filename}"
        for album, photos in PHOTO_ALBUMS.items()
        for p in photos
    }
    return names


def test_bundle_is_under_5_mb() -> None:
    total = sum(p.stat().st_size for p in _bundled_files())
    assert 0 < total < MAX_BUNDLE_BYTES


def test_every_bundled_file_is_a_generated_catalogue_entry() -> None:
    bundled = {p.relative_to(MEDIA_DIR).as_posix() for p in _bundled_files()}
    assert bundled == _catalogue()


def test_third_party_notices_record_every_file_license() -> None:
    notices = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    assert "CC0 1.0" in notices
    for path in _bundled_files():
        assert f"`{path.relative_to(REPO_ROOT).as_posix()}`" in notices, path
