"""Tests for the sample media generator (``demo/make_sample_media.py``).

Rendering whole tunes and calling ffmpeg is slow or needs ffmpeg, so those
paths are exercised with short inputs or a mocked subprocess.
"""

import io
import random
import subprocess
import wave
from pathlib import Path
from unittest.mock import MagicMock

import mutagen.id3
import pytest
from PIL import Image

from demo import make_sample_media as msm


def test_note_frequency_a4_is_440() -> None:
    assert msm.note_frequency("A4") == pytest.approx(440.0)


def test_note_frequency_handles_octaves_and_accidentals() -> None:
    assert msm.note_frequency("A5") == pytest.approx(880.0)
    assert msm.note_frequency("C4") == pytest.approx(261.63, abs=0.01)
    assert msm.note_frequency("F#4") == pytest.approx(msm.note_frequency("Gb4"))


def test_render_tune_length_and_range() -> None:
    melody = [("C5", 1.0), ("R", 1.0), ("E5", 2.0)]
    samples = msm.render_tune(melody, [("C3", 4.0)], bpm=240, repeats=1)
    # 4 beats at 240 bpm = 1 s, plus 2 s of tail.
    assert len(samples) == 3 * msm.SAMPLE_RATE
    assert max(abs(s) for s in samples) <= 1.0
    assert any(s != 0.0 for s in samples)


def test_write_wav_writes_mono_16_bit(tmp_path: Path) -> None:
    path = tmp_path / "t.wav"
    msm.write_wav([0.0, 0.5, -0.5, 2.0], path)
    with wave.open(str(path)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == msm.SAMPLE_RATE
        assert wav.getnframes() == 4


def test_encode_mp3_calls_ffmpeg_bit_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = MagicMock()
    monkeypatch.setattr(msm.subprocess, "run", run)
    msm.encode_mp3(tmp_path / "in.wav", tmp_path / "out.mp3", "64k")
    cmd = run.call_args.args[0]
    assert cmd[0] == "ffmpeg"
    assert "libmp3lame" in cmd
    assert "+bitexact" in cmd
    assert cmd[-1] == str(tmp_path / "out.mp3")


def test_tag_mp3_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "t.mp3"
    path.write_bytes(b"\xff\xfb" + b"\x00" * 64)
    msm.tag_mp3(path, title="T", artist="A", album="B", track=2, cover=b"jpeg")
    tags = mutagen.id3.ID3(path)
    assert str(tags["TIT2"]) == "T"
    assert str(tags["TALB"]) == "B"
    assert str(tags["TRCK"]) == "2"
    assert tags.getall("APIC")[0].data == b"jpeg"


@pytest.mark.parametrize(
    "scene",
    [
        msm.scene_sunset,
        msm.scene_night,
        msm.scene_sailboat,
        msm.scene_meadow,
        msm.scene_rainbow,
        msm.scene_mountains,
    ],
)
def test_render_scene_is_sized_and_deterministic(scene: msm.Scene) -> None:
    a = msm.render_scene(scene, (64, 48), seed=3)
    b = msm.render_scene(scene, (64, 48), seed=3)
    assert a.size == (64, 48)
    assert a.tobytes() == b.tobytes()


def test_scene_uses_the_given_rng() -> None:
    a = msm.scene_night((64, 48), random.Random(1))
    b = msm.scene_night((64, 48), random.Random(2))
    assert a.tobytes() != b.tobytes()


def test_cover_art_is_a_300px_jpeg() -> None:
    img = Image.open(io.BytesIO(msm.cover_art(msm.scene_meadow, seed=1)))
    assert img.format == "JPEG"
    assert img.size == (300, 300)


def test_generate_writes_the_whole_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_ffmpeg(args: list[str]) -> None:
        Path(args[-1]).write_bytes(b"\xff\xfb" + b"\x00" * 64)

    monkeypatch.setattr(msm.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(msm, "missing_ffmpeg_features", list)
    monkeypatch.setattr(msm, "_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(msm, "render_tune", lambda *a, **k: [0.0] * 10)
    monkeypatch.setattr(msm, "PHOTO_SIZE", (32, 24))
    msm.generate(tmp_path)

    written = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.*")}
    assert "music/Nursery Tunes/01-twinkle.mp3" in written
    assert "audiobooks/Aesop's Fables/02-lion-and-mouse.mp3" in written
    assert "photos/Backyard/rainbow.jpg" in written
    expected = (
        sum(len(v) for v in msm.ALBUMS.values())
        + sum(len(v) for v in msm.AUDIOBOOKS.values())
        + sum(len(v) for v in msm.PHOTO_ALBUMS.values())
    )
    assert len(written) == expected
    tags = mutagen.id3.ID3(tmp_path / "music/Nursery Tunes/01-twinkle.mp3")
    assert str(tags["TIT2"]) == "Twinkle, Twinkle"


def test_generate_requires_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(msm.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit):
        msm.generate(tmp_path)


def _existing_media(root: Path) -> None:
    for sub in msm.SUBDIRS:
        (root / sub).mkdir(parents=True)
        (root / sub / "keep.txt").write_text(f"old {sub}")


def _snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*.txt")}


def test_missing_ffmpeg_features_reads_the_listings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listings = {
        "-filters": " ... flite             |->A       Synthesize with flite\n",
        "-encoders": " A....D libmp3lame  libmp3lame MP3 (MPEG audio layer 3)\n",
    }

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=listings[cmd[-1]])

    monkeypatch.setattr(msm.subprocess, "run", fake_run)
    assert msm.missing_ffmpeg_features() == []

    listings["-filters"] = " ... afade   A->A   Fade in/out\n"  # Homebrew's build
    assert msm.missing_ffmpeg_features() == ["flite"]
    listings["-encoders"] = " A....D aac  AAC\n"
    assert msm.missing_ffmpeg_features() == ["flite", "libmp3lame"]


def test_generate_without_flite_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Homebrew's ffmpeg has no flite: refuse before touching the media."""
    _existing_media(tmp_path)
    before = _snapshot(tmp_path)
    monkeypatch.setattr(msm.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(msm, "missing_ffmpeg_features", lambda: ["flite"])

    with pytest.raises(SystemExit, match="Homebrew"):
        msm.generate(tmp_path)

    assert _snapshot(tmp_path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(msm.SUBDIRS)


def test_generate_that_fails_midway_leaves_the_old_media_and_no_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _existing_media(tmp_path)
    before = _snapshot(tmp_path)
    calls = 0

    def flaky_ffmpeg(args: list[str]) -> None:
        nonlocal calls
        calls += 1
        if any(a.startswith("flite=") for a in args):
            raise subprocess.CalledProcessError(1, "ffmpeg")  # the first fable
        Path(args[-1]).write_bytes(b"\xff\xfb" + b"\x00" * 64)

    monkeypatch.setattr(msm.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(msm, "missing_ffmpeg_features", list)
    monkeypatch.setattr(msm, "_ffmpeg", flaky_ffmpeg)
    monkeypatch.setattr(msm, "render_tune", lambda *a, **k: [0.0] * 10)
    monkeypatch.setattr(msm, "PHOTO_SIZE", (32, 24))

    with pytest.raises(subprocess.CalledProcessError):
        msm.generate(tmp_path)

    assert calls > 1, "the music was generated before the failure"
    assert _snapshot(tmp_path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(msm.SUBDIRS)


def test_generate_replaces_the_old_media_when_it_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _existing_media(tmp_path)

    def fake_ffmpeg(args: list[str]) -> None:
        Path(args[-1]).write_bytes(b"\xff\xfb" + b"\x00" * 64)

    monkeypatch.setattr(msm.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(msm, "missing_ffmpeg_features", list)
    monkeypatch.setattr(msm, "_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(msm, "render_tune", lambda *a, **k: [0.0] * 10)
    monkeypatch.setattr(msm, "PHOTO_SIZE", (32, 24))
    msm.generate(tmp_path)

    assert _snapshot(tmp_path) == {}, "the old files are gone"
    assert (tmp_path / "music/Nursery Tunes/01-twinkle.mp3").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(msm.SUBDIRS)


def test_main_reports_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_generate(out: Path) -> None:
        (out / "x.bin").write_bytes(b"0" * 2_000_000)

    monkeypatch.setattr(msm, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(msm, "generate", fake_generate)
    msm.main()
    assert "Total: 2.00 MB" in capsys.readouterr().out
