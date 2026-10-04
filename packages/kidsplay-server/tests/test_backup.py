"""Tests for server backup and restore (kidsplay_server.backup).

Media comes from the real photo ingest pipeline (Pillow only, no ffmpeg), so
the store layout and database rows are exactly what a live server produces.
"""

import hashlib
import io
import random
import shutil
import sqlite3
import stat
import tarfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
import zstandard
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from kidsplay_models.device import Device, Profile
from kidsplay_models.media import MediaType
from kidsplay_models.themes import ThemeAsset, ThemeAssetRole, ThemeColors
from kidsplay_server import storage
from kidsplay_server.api.app import create_app
from kidsplay_server.backup import (
    DB_NAME,
    MANIFEST_NAME,
    MEDIA_DIR,
    BackupError,
    create_backup,
    is_archive_path,
    is_restore_target_empty,
    iter_media_files,
    restore_backup,
    snapshot_database,
)
from kidsplay_server.database import (
    configure_conn,
    create_device,
    create_profile,
    init_db,
    set_theme_asset,
    upsert_custom_theme,
)
from kidsplay_server.processing.pipeline import ingest_directory, ingest_file
from kidsplay_server.storage import MediaStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_png(path: Path, seed: int) -> Path:
    """Write a small PNG whose content (and so hash) depends on ``seed``."""
    # Blocky noise, larger than the device screen, so the resized photo and
    # every thumbnail size differ from each other and between seeds (no dedup).
    pixels = random.Random(seed).randbytes(70 * 50 * 3)
    img = Image.frombytes("RGB", (70, 50), pixels).resize(
        (700, 500), Image.Resampling.NEAREST
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    return path


async def ingest_photos(
    db_path: Path, media_root: Path, src_dir: Path, seeds: range
) -> None:
    """Ingest one generated photo per seed through the real pipeline."""
    store = MediaStore(media_root)
    for seed in seeds:
        src = make_png(src_dir / f"photo{seed}.png", seed)
        result = await ingest_file(src, MediaType.PHOTO, db_path, store)
        assert result.errors == []


async def seed_server(db_path: Path, media_root: Path, src_dir: Path) -> Device:
    """Create a profile, a device and two photos; return the device."""
    profile = Profile(name="Leo")
    device = Device(name="Leo's GameBoy", profile_id=profile.id)
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await init_db(conn)
        await create_profile(conn, profile)
        await create_device(conn, device)
        await conn.commit()
    await ingest_photos(db_path, media_root, src_dir, range(2))
    return device


def dump_rows(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Return every row of every table, sorted, for whole-database comparison.

    Values are ``object`` because SQLite columns hold mixed Python types.
    """
    with sqlite3.connect(db_path) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            t: sorted(conn.execute(f'SELECT * FROM "{t}"').fetchall()) for t in tables
        }


def media_hashes(root: Path) -> dict[str, str]:
    """Map each file under ``root`` to the SHA-256 of its content."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def referenced_paths(db_path: Path) -> list[str]:
    """Return every ``processed_files.relative_path`` in a database."""
    with sqlite3.connect(db_path) as conn:
        return [r[0] for r in conn.execute("SELECT relative_path FROM processed_files")]


def read_archive(path: Path) -> dict[str, bytes]:
    """Return ``{member name: content}`` for a .tar.zst archive."""
    with (
        path.open("rb") as raw,
        zstandard.ZstdDecompressor().stream_reader(raw) as zst,
        tarfile.open(fileobj=zst, mode="r|") as tar,
    ):
        out: dict[str, bytes] = {}
        for member in tar:
            f = tar.extractfile(member)
            assert f is not None
            out[member.name] = f.read()
        return out


def write_archive(path: Path, members: dict[str, bytes]) -> Path:
    """Write a .tar.zst archive with the given members."""
    with (
        path.open("wb") as raw,
        zstandard.ZstdCompressor().stream_writer(raw) as zst,
        tarfile.open(fileobj=zst, mode="w|") as tar,
    ):
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def mode(path: Path) -> int:
    """Return the permission bits of ``path``."""
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return ``(db_path, media_root, source_dir)`` for a server in tmp_path."""
    return (
        tmp_path / "srv" / "kidsplay.db",
        tmp_path / "srv" / "media",
        tmp_path / "src",
    )


@pytest.fixture
async def seeded(paths: tuple[Path, Path, Path]) -> AsyncIterator[Device]:
    """A server with one profile, one device and two ingested photos."""
    db_path, media_root, src = paths
    db_path.parent.mkdir(parents=True)
    yield await seed_server(db_path, media_root, src)


# ---------------------------------------------------------------------------
# Acceptance: round trip
# ---------------------------------------------------------------------------


async def _sync_device(app: FastAPI, device: dict[str, str]) -> dict[str, bytes]:
    """Sync like a device: fetch the manifest, then download every file."""
    headers = {"Authorization": f"Bearer {device['api_key']}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest", headers=headers
        )
        assert r.status_code == 200
        files: dict[str, bytes] = {}
        for entry in r.json()["files"]:
            f = await client.get(
                f"/api/v1/sync/file/{entry['content_hash']}", headers=headers
            )
            assert f.status_code == 200
            assert hashlib.sha256(f.content).hexdigest() == entry["content_hash"]
            files[entry["content_hash"]] = f.content
        return files


@pytest.mark.parametrize("target", ["backup.tar.zst", "backup-dir"])
async def test_round_trip_restores_rows_media_and_device_key(
    tmp_path: Path,
    target: str,
    admin_headers_for: Callable[[FastAPI], Awaitable[dict[str, str]]],
) -> None:
    """backup → wipe → restore → identical rows and media → device syncs."""
    db_path, media_root = tmp_path / "srv" / "kidsplay.db", tmp_path / "srv" / "media"
    app = create_app(db_path, media_root)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=await admin_headers_for(app),
    ) as client:
        profile = (await client.post("/api/v1/profiles", json={"name": "Leo"})).json()
        device = (
            await client.post(
                "/api/v1/devices", json={"name": "GB", "profile_id": profile["id"]}
            )
        ).json()
        for seed in range(3):
            src = make_png(tmp_path / "src" / f"p{seed}.png", seed)
            r = await client.post(
                "/api/v1/media/ingest",
                json={
                    "source_path": str(src),
                    "media_type": "photo",
                    "playlist_title": "Photos",
                    "profile_ids": [profile["id"]],
                },
            )
            assert r.status_code == 200
    synced_before = await _sync_device(app, device)
    assert len(synced_before) == 12  # 3 photos x (resized + 3 thumbnails)

    rows_before = dump_rows(db_path)
    hashes_before = media_hashes(media_root)

    dest = tmp_path / target
    result = create_backup(db_path, media_root, dest)
    assert result.missing_referenced == []
    assert result.media_copied == len(hashes_before)

    shutil.rmtree(tmp_path / "srv")
    restore_backup(dest, db_path, media_root)

    assert dump_rows(db_path) == rows_before
    assert media_hashes(media_root) == hashes_before
    assert await _sync_device(create_app(db_path, media_root), device) == synced_before


# ---------------------------------------------------------------------------
# Acceptance: backup racing an ingest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["race.tar.zst", "race-dir"])
async def test_backup_during_ingest_is_consistent(
    tmp_path: Path,
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    """A backup taken mid-ingest references no half-written file.

    The ingest runs for real. Its store copy is slowed so that, on the second
    photo's first thumbnail, the file is half-written and the ingest's
    transaction for that photo is open. The backup runs in another thread at
    exactly that moment, then the ingest finishes. The store publishes files
    atomically, so the half-written copy is a hidden temp file and never at the
    final path; the backup must neither include it nor be confused by it.
    """
    db_path, media_root, src = paths
    db_path.parent.mkdir(parents=True)
    for seed in range(3):
        make_png(src / f"photo{seed}.png", seed)

    real_copy2 = shutil.copy2
    half_written = threading.Event()
    backup_done = threading.Event()
    calls: list[Path] = []
    partial: list[str] = []

    def slow_copy2(source: str, dest: str) -> str:
        calls.append(Path(dest))
        # Per photo: resized photo, then three thumbnails. Call 6 is photo 2's
        # first thumbnail, written while its media row is uncommitted.
        if len(calls) != 6:
            return real_copy2(source, dest)
        data = Path(source).read_bytes()
        with open(dest, "wb") as f:
            f.write(data[: len(data) // 2])
            f.flush()
            partial.append(Path(dest).relative_to(media_root).as_posix())
            half_written.set()
            assert backup_done.wait(30)
            f.write(data[len(data) // 2 :])
        return dest

    monkeypatch.setattr(storage.shutil, "copy2", slow_copy2)

    results = []

    def run_backup() -> None:
        try:
            assert half_written.wait(30)
            results.append(create_backup(db_path, media_root, tmp_path / target))
        finally:
            backup_done.set()

    thread = threading.Thread(target=run_backup)
    thread.start()
    batch = await ingest_directory(
        src, MediaType.PHOTO, db_path, MediaStore(media_root)
    )
    thread.join()

    assert batch.successful == 3
    assert len(results) == 1
    result = results[0]
    # The half-written temp file was on disk during the backup and was ignored;
    # nothing at a final path was ever incomplete, so nothing had to be rejected.
    assert Path(partial[0]).name.startswith(".")
    assert result.media_rejected == []
    assert result.missing_referenced == []

    # Inspect what the backup actually holds.
    if target.endswith(".tar.zst"):
        members = read_archive(tmp_path / target)
        archived_db = tmp_path / "archived.db"
        archived_db.write_bytes(members[DB_NAME])
        media = {
            k.removeprefix(f"{MEDIA_DIR}/"): v
            for k, v in members.items()
            if k.startswith(f"{MEDIA_DIR}/")
        }
    else:
        archived_db = tmp_path / target / DB_NAME
        media = {
            p.relative_to(tmp_path / target / MEDIA_DIR).as_posix(): p.read_bytes()
            for p in (tmp_path / target / MEDIA_DIR).rglob("*")
            if p.is_file()
        }

    # Snapshot is mid-ingest: photo 1 committed, photo 2 not.
    with sqlite3.connect(archived_db) as conn:
        (items,) = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()
    assert items == 1
    refs = referenced_paths(archived_db)
    assert len(refs) == 4
    for rel in refs:
        assert rel in media
    for rel, data in media.items():
        assert Path(rel).name.startswith(hashlib.sha256(data).hexdigest())
    assert partial[0] not in media

    # And the mid-ingest backup restores cleanly.
    restored = restore_backup(
        tmp_path / target, tmp_path / "r" / "kidsplay.db", tmp_path / "r" / "media"
    )
    assert restored.missing_referenced == []
    assert restored.media_rejected == []


# ---------------------------------------------------------------------------
# Acceptance: incremental directory backups
# ---------------------------------------------------------------------------


async def test_second_directory_backup_copies_only_new_files(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, src = paths
    dest = tmp_path / "bk"

    first = create_backup(db_path, media_root, dest)
    assert first.media_copied == 8
    assert first.media_already_present == 0
    old = {
        rel: (dest / MEDIA_DIR / rel).stat() for rel in media_hashes(dest / MEDIA_DIR)
    }

    await ingest_photos(db_path, media_root, src, range(2, 3))
    second = create_backup(db_path, media_root, dest)

    assert second.media_copied == 4
    assert second.media_already_present == 8
    assert media_hashes(dest / MEDIA_DIR) == media_hashes(media_root)
    for rel, st in old.items():
        now = (dest / MEDIA_DIR / rel).stat()
        assert (now.st_ino, now.st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    assert dump_rows(dest / DB_NAME) == dump_rows(db_path)


async def test_unchanged_store_copies_nothing(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    create_backup(db_path, media_root, tmp_path / "bk")
    again = create_backup(db_path, media_root, tmp_path / "bk")
    assert again.media_copied == 0
    assert again.media_already_present == 8


# ---------------------------------------------------------------------------
# Credentials: permissions
# ---------------------------------------------------------------------------


async def test_archive_is_owner_only(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "out" / "kp.tar.zst"
    create_backup(db_path, media_root, dest)
    assert mode(dest) == 0o600
    assert [p.name for p in dest.parent.iterdir()] == ["kp.tar.zst"]


async def test_archive_overwrite_stays_owner_only(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "kp.tar.zst"
    dest.write_bytes(b"old")
    dest.chmod(0o644)
    create_backup(db_path, media_root, dest)
    assert mode(dest) == 0o600
    assert DB_NAME in read_archive(dest)


async def test_directory_backup_is_owner_only(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "bk"
    create_backup(db_path, media_root, dest)
    assert mode(dest) == 0o700
    assert mode(dest / DB_NAME) == 0o600
    assert mode(dest / MANIFEST_NAME) == 0o600
    assert not [p for p in dest.iterdir() if p.name.startswith(".")]


async def test_archive_contains_device_key(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    """The documented reason for 0600: the key is in the backup verbatim."""
    db_path, media_root, _ = paths
    dest = tmp_path / "kp.tar.zst"
    create_backup(db_path, media_root, dest)
    assert seeded.api_key.encode() in read_archive(dest)[DB_NAME]


# ---------------------------------------------------------------------------
# --db-only
# ---------------------------------------------------------------------------


async def test_db_only_archive_has_no_media(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "db.tar.zst"
    result = create_backup(db_path, media_root, dest, db_only=True)
    assert result.media_copied == 0
    assert sorted(read_archive(dest)) == [DB_NAME, MANIFEST_NAME]

    restore_backup(dest, tmp_path / "r.db", tmp_path / "r-media")
    assert dump_rows(tmp_path / "r.db") == dump_rows(db_path)


async def test_db_only_directory_has_no_media(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    create_backup(db_path, media_root, tmp_path / "bk", db_only=True)
    assert not (tmp_path / "bk" / MEDIA_DIR).exists()


# ---------------------------------------------------------------------------
# Restore safety
# ---------------------------------------------------------------------------


async def test_restore_refuses_non_empty_database(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "kp.tar.zst"
    create_backup(db_path, media_root, dest, db_only=True)
    before = dump_rows(db_path)
    with pytest.raises(BackupError, match="--force"):
        restore_backup(dest, db_path, tmp_path / "empty-media")
    assert dump_rows(db_path) == before


async def test_restore_refuses_non_empty_media_store(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "kp.tar.zst"
    create_backup(db_path, media_root, dest)
    with pytest.raises(BackupError, match="not empty"):
        restore_backup(dest, tmp_path / "new.db", media_root)
    assert not (tmp_path / "new.db").exists()


async def test_restore_force_replaces_database(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, src = paths
    dest = tmp_path / "kp.tar.zst"
    create_backup(db_path, media_root, dest)
    rows = dump_rows(db_path)
    await ingest_photos(db_path, media_root, src, range(5, 6))
    assert dump_rows(db_path) != rows

    restore_backup(dest, db_path, media_root, force=True)
    assert dump_rows(db_path) == rows


async def test_restore_into_fresh_schema_needs_no_force(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    """A server that started once has tables but no rows: that is empty."""
    db_path, media_root, _ = paths
    dest = tmp_path / "kp.tar.zst"
    create_backup(db_path, media_root, dest)
    fresh = tmp_path / "fresh.db"
    async with aiosqlite.connect(fresh) as conn:
        await init_db(conn)
    restore_backup(dest, fresh, tmp_path / "fresh-media")
    assert dump_rows(fresh) == dump_rows(db_path)


def test_is_restore_target_empty(tmp_path: Path) -> None:
    db_path, media_root = tmp_path / "x.db", tmp_path / "media"
    assert is_restore_target_empty(db_path, media_root)
    db_path.touch()
    assert is_restore_target_empty(db_path, media_root)
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE "odd ""name" (x)')
    assert is_restore_target_empty(db_path, media_root)
    with sqlite3.connect(db_path) as conn:
        conn.execute('INSERT INTO "odd ""name" VALUES (1)')
    assert not is_restore_target_empty(db_path, media_root)
    db_path.unlink()
    (media_root / "sub").mkdir(parents=True)
    assert is_restore_target_empty(db_path, media_root)
    (media_root / "sub" / "f").touch()
    assert not is_restore_target_empty(db_path, media_root)


def test_restore_missing_source(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="not found"):
        restore_backup(tmp_path / "nope.tar.zst", tmp_path / "db", tmp_path / "m")


@pytest.mark.parametrize(
    "name", ["../evil", "media/../../evil", "/etc/evil", "media/audio/ab/notahash"]
)
def test_restore_rejects_unexpected_member(tmp_path: Path, name: str) -> None:
    archive = write_archive(tmp_path / "bad.tar.zst", {name: b"x"})
    with pytest.raises(BackupError, match="Unexpected archive entry"):
        restore_backup(archive, tmp_path / "r" / "db", tmp_path / "r" / "media")
    assert not (tmp_path / "evil").exists()


def test_restore_rejects_archive_without_manifest(tmp_path: Path) -> None:
    db = tmp_path / "src.db"
    sqlite3.connect(db).close()
    archive = write_archive(tmp_path / "bad.tar.zst", {DB_NAME: db.read_bytes()})
    with pytest.raises(BackupError, match="incomplete"):
        restore_backup(archive, tmp_path / "r" / "db", tmp_path / "r" / "media")
    assert not (tmp_path / "r" / "db").exists()


async def test_truncated_archive_leaves_target_untouched(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    good = tmp_path / "good.tar.zst"
    create_backup(db_path, media_root, good)
    truncated = tmp_path / "truncated.tar.zst"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    r_db, r_media = tmp_path / "r" / "kidsplay.db", tmp_path / "r" / "media"

    with pytest.raises(BackupError, match="Cannot read archive"):
        restore_backup(truncated, r_db, r_media)
    assert is_restore_target_empty(r_db, r_media)
    assert not r_db.exists()

    # A plain retry with a good archive needs no --force.
    result = restore_backup(good, r_db, r_media)
    assert result.missing_referenced == []
    assert media_hashes(r_media) == media_hashes(media_root)


async def test_archive_without_manifest_writes_no_media(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    members = read_archive(
        create_backup(db_path, media_root, tmp_path / "a.tar.zst").target
    )
    del members[MANIFEST_NAME]
    archive = write_archive(tmp_path / "no-manifest.tar.zst", members)
    r_db, r_media = tmp_path / "r" / "kidsplay.db", tmp_path / "r" / "media"

    with pytest.raises(BackupError, match="incomplete"):
        restore_backup(archive, r_db, r_media)
    assert is_restore_target_empty(r_db, r_media)


def test_restore_rejects_duplicate_database(tmp_path: Path) -> None:
    db = tmp_path / "src.db"
    sqlite3.connect(db).close()
    archive = tmp_path / "dup.tar.zst"
    with (
        archive.open("wb") as raw,
        zstandard.ZstdCompressor().stream_writer(raw) as zst,
        tarfile.open(fileobj=zst, mode="w|") as tar,
    ):
        for _ in range(2):
            info = tarfile.TarInfo(DB_NAME)
            info.size = db.stat().st_size
            tar.addfile(info, io.BytesIO(db.read_bytes()))
    with pytest.raises(BackupError, match="more than one database"):
        restore_backup(archive, tmp_path / "r" / "db", tmp_path / "r" / "media")
    assert not (tmp_path / "r" / "db").exists()


def test_restore_rejects_unknown_format_version(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "new.tar.zst", {MANIFEST_NAME: b'{"format_version": 99}'}
    )
    with pytest.raises(BackupError, match="format version"):
        restore_backup(archive, tmp_path / "r" / "db", tmp_path / "r" / "media")


def test_restore_rejects_non_archive(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar.zst"
    junk.write_bytes(b"not zstd at all")
    with pytest.raises(BackupError, match="Cannot read archive"):
        restore_backup(junk, tmp_path / "r" / "db", tmp_path / "r" / "media")


def test_restore_rejects_non_backup_directory(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    with pytest.raises(BackupError, match="not a KidsPlay backup"):
        restore_backup(tmp_path / "dir", tmp_path / "r" / "db", tmp_path / "r" / "m")


async def test_restore_skips_corrupt_media_and_reports_it(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "bk"
    create_backup(db_path, media_root, dest)
    victim = sorted(media_hashes(dest / MEDIA_DIR))[0]
    (dest / MEDIA_DIR / victim).write_bytes(b"bit rot")

    result = restore_backup(dest, tmp_path / "r.db", tmp_path / "r-media")
    assert result.media_rejected == [victim]
    assert result.missing_referenced == [victim]
    assert result.media_restored == 7


# ---------------------------------------------------------------------------
# Backup edge cases
# ---------------------------------------------------------------------------


async def test_corrupt_store_file_is_reported_missing(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    victim = referenced_paths(db_path)[0]
    (media_root / victim).write_bytes(b"truncated")

    result = create_backup(db_path, media_root, tmp_path / "kp.tar.zst")
    assert result.media_rejected == [victim]
    assert result.missing_referenced == [victim]
    assert f"{MEDIA_DIR}/{victim}" not in read_archive(tmp_path / "kp.tar.zst")


def test_backup_without_database_fails(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="Database not found"):
        create_backup(tmp_path / "none.db", tmp_path / "m", tmp_path / "x.tar.zst")
    assert not (tmp_path / "x.tar.zst").exists()


async def test_directory_target_that_is_a_file_fails(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    (tmp_path / "file").write_text("x")
    with pytest.raises(BackupError, match="not a directory"):
        create_backup(db_path, media_root, tmp_path / "file")


def test_snapshot_copies_every_table(tmp_path: Path) -> None:
    """Tables the backup code has never heard of are included."""
    db = tmp_path / "live.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE admin_secrets (id INTEGER, secret TEXT)")
        conn.execute("INSERT INTO admin_secrets VALUES (1, 's3cret')")
    snapshot_database(db, tmp_path / "snap.db")
    assert dump_rows(tmp_path / "snap.db") == dump_rows(db)


def test_snapshot_sees_committed_state_only(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
    writer = sqlite3.connect(db)
    try:
        writer.execute("INSERT INTO t VALUES (2)")  # open, uncommitted
        snapshot_database(db, tmp_path / "snap.db")
    finally:
        writer.rollback()
        writer.close()
    assert dump_rows(tmp_path / "snap.db") == {"t": [(1,)]}


def test_iter_media_files_skips_non_store_files(tmp_path: Path) -> None:
    h = hashlib.sha256(b"a").hexdigest()
    root = tmp_path / "m"
    good = root / "audio" / h[:2] / f"{h}.mp3"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"a")
    (good.parent / f".{h}.mp3.tmp").write_bytes(b"a")
    wrong_prefix = root / "audio" / "zz" / f"{h}.mp3"
    wrong_prefix.parent.mkdir()
    wrong_prefix.write_bytes(b"a")
    (root / "README").write_text("x")
    assert list(iter_media_files(root)) == [(f"audio/{h[:2]}/{h}.mp3", good)]
    assert list(iter_media_files(tmp_path / "missing")) == []


def test_is_archive_path() -> None:
    assert is_archive_path(Path("/b/kp.tar.zst"))
    assert not is_archive_path(Path("/b/kp"))
    assert not is_archive_path(Path("/b/kp.tar.gz"))


@pytest.mark.parametrize("name", ["kp.TAR.ZST", "kp.Tar.Zst", "KP.tar.zst"])
def test_is_archive_path_ignores_case(name: str) -> None:
    assert is_archive_path(Path("/b") / name)


@pytest.mark.parametrize("name", ["x.TAR.ZST", "x.Tar.Zst"])
async def test_upper_case_archive_name_writes_an_archive_not_a_directory(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device, name: str
) -> None:
    """Regression: the name check lowercased but archive detection did not."""
    db_path, media_root, _ = paths
    dest = tmp_path / name
    create_backup(db_path, media_root, dest)
    assert dest.is_file()
    assert MANIFEST_NAME in read_archive(dest)
    restore_backup(dest, tmp_path / "r" / "db", tmp_path / "r" / "media")


# ---------------------------------------------------------------------------
# Follow-ups (#24)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["foo.tar.gz", "foo.tgz", "foo.tar", "foo.zst", "foo.tar.bz2", "FOO.TAR.GZ"],
)
async def test_misnamed_archive_target_is_rejected(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device, name: str
) -> None:
    db_path, media_root, _ = paths
    with pytest.raises(BackupError, match=r"looks like an archive name"):
        create_backup(db_path, media_root, tmp_path / name)
    assert not (tmp_path / name).exists()  # no directory was created either


@pytest.mark.parametrize("name", ["kp.tar.zst", "backup", "nightly-2026.09", "a.tars"])
async def test_plain_and_zst_tar_names_are_accepted(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device, name: str
) -> None:
    db_path, media_root, _ = paths
    create_backup(db_path, media_root, tmp_path / name, db_only=True)
    assert (tmp_path / name).exists()


async def test_restore_rejects_damaged_database_before_touching_target(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "bk"
    create_backup(db_path, media_root, dest)
    # Bit rot in the middle of the database file: still opens, fails quick_check.
    raw = bytearray((dest / DB_NAME).read_bytes())
    for i in range(4096 * 2, len(raw), 97):
        raw[i] ^= 0xFF
    (dest / DB_NAME).write_bytes(bytes(raw))

    target_db, target_media = tmp_path / "r" / "db", tmp_path / "r" / "media"
    with pytest.raises(BackupError, match="integrity check"):
        restore_backup(dest, target_db, target_media)
    assert not target_db.exists()
    assert not target_media.exists()  # no empty media/ left behind


async def test_restore_rejects_database_that_is_not_sqlite(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "bk"
    create_backup(db_path, media_root, dest)
    (dest / DB_NAME).write_bytes(b"this is not a database" * 500)
    with pytest.raises(BackupError, match="integrity check"):
        restore_backup(dest, tmp_path / "r.db", tmp_path / "r-media", force=True)
    assert not (tmp_path / "r.db").exists()


async def test_verify_rewrites_bit_rotted_backup_file(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "bk"
    create_backup(db_path, media_root, dest)
    victim = sorted(media_hashes(dest / MEDIA_DIR))[0]
    (dest / MEDIA_DIR / victim).write_bytes(b"bit rot")

    # Without --verify the existing file is trusted, rot and all.
    plain = create_backup(db_path, media_root, dest)
    assert plain.media_repaired == []
    assert (dest / MEDIA_DIR / victim).read_bytes() == b"bit rot"

    result = create_backup(db_path, media_root, dest, verify=True)
    assert result.media_repaired == [victim]
    assert result.media_copied == 1
    assert result.media_already_present == 7
    assert result.missing_referenced == []
    assert media_hashes(dest / MEDIA_DIR) == media_hashes(media_root)


async def test_verify_with_healthy_backup_copies_nothing(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    create_backup(db_path, media_root, tmp_path / "bk")
    again = create_backup(db_path, media_root, tmp_path / "bk", verify=True)
    assert (again.media_copied, again.media_already_present) == (0, 8)
    assert again.media_repaired == []


async def test_verify_reports_file_it_cannot_repair(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    """Rot in both the store and the backup: the file stays reported missing."""
    db_path, media_root, _ = paths
    dest = tmp_path / "bk"
    create_backup(db_path, media_root, dest)
    victim = referenced_paths(db_path)[0]
    (dest / MEDIA_DIR / victim).write_bytes(b"rot")
    (media_root / victim).write_bytes(b"rot too")
    result = create_backup(db_path, media_root, dest, verify=True)
    assert result.media_rejected == [victim]
    assert result.media_repaired == []
    assert result.missing_referenced == [victim]


async def test_restored_database_is_owner_only(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "kp.tar.zst"
    create_backup(db_path, media_root, dest)
    fresh = tmp_path / "r" / "kidsplay.db"
    restore_backup(dest, fresh, tmp_path / "r" / "media")
    assert mode(fresh) == 0o600


async def test_restore_over_world_readable_database_tightens_it(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    dest = tmp_path / "kp.tar.zst"
    create_backup(db_path, media_root, dest)
    db_path.chmod(0o644)
    restore_backup(dest, db_path, media_root, force=True)
    assert mode(db_path) == 0o600


async def _add_theme_asset(db_path: Path, media_root: Path, tmp_path: Path) -> str:
    """Give the server a custom theme with one background; return its store path."""
    source = tmp_path / "bg.webp"
    source.write_bytes(b"pretend this is a webp")
    content_hash, rel = MediaStore(media_root).store(source, "themes", ".webp")
    colors = ThemeColors(
        bg="#123456",
        surface="#123456",
        surface_sel="#123456",
        primary="#123456",
        text="#123456",
        text_dim="#123456",
        text_bright="#123456",
        accent="#123456",
        progress_bg="#123456",
    )
    async with aiosqlite.connect(db_path) as conn:
        await configure_conn(conn)
        await upsert_custom_theme(conn, "sunset", "Sunset", colors, datetime.now(UTC))
        asset = ThemeAsset(
            role=ThemeAssetRole.BACKGROUND,
            content_hash=content_hash,
            relative_path=rel,
            size_bytes=source.stat().st_size,
        )
        await set_theme_asset(conn, "sunset", asset, "image/webp")
        await conn.commit()
    return rel


@pytest.mark.parametrize("archive", [True, False])
async def test_backup_checks_theme_assets_for_completeness(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device, archive: bool
) -> None:
    db_path, media_root, _ = paths
    rel = await _add_theme_asset(db_path, media_root, tmp_path)
    dest = tmp_path / ("kp.tar.zst" if archive else "bk")

    complete = create_backup(db_path, media_root, dest)
    assert complete.missing_referenced == []

    (media_root / rel).unlink()
    incomplete = create_backup(db_path, media_root, tmp_path / "second.tar.zst")
    assert incomplete.missing_referenced == [rel]
    # Restoring a backup whose database points at a lost theme file says so too.
    restored = restore_backup(dest, tmp_path / "r.db", tmp_path / "r-media")
    assert restored.missing_referenced == []
    assert (tmp_path / "r-media" / rel).is_file()


async def test_failed_restore_leaves_no_empty_media_directory(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    good = tmp_path / "good.tar.zst"
    create_backup(db_path, media_root, good)
    truncated = tmp_path / "truncated.tar.zst"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    r_db, r_media = tmp_path / "r" / "kidsplay.db", tmp_path / "r" / "media"

    with pytest.raises(BackupError):
        restore_backup(truncated, r_db, r_media)
    assert not r_media.exists()
    assert not r_db.parent.exists()


async def test_failed_restore_keeps_a_media_directory_that_already_existed(
    tmp_path: Path, paths: tuple[Path, Path, Path], seeded: Device
) -> None:
    db_path, media_root, _ = paths
    bad = tmp_path / "bad.tar.zst"
    bad.write_bytes(b"not zstd at all")
    r_media = tmp_path / "r" / "media"
    r_media.mkdir(parents=True)

    with pytest.raises(BackupError):
        restore_backup(bad, tmp_path / "r" / "kidsplay.db", r_media)
    assert r_media.is_dir()
