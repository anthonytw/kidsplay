"""Tests for the ``kidsplay-server`` command line (kidsplay_server.cli)."""

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from kidsplay_server import cli
from kidsplay_server.backup import DB_NAME, MEDIA_DIR

HASH = "ab" + "0" * 62  # content deliberately does not match: a corrupt file


def make_db(path: Path, rows: int = 1, refs: list[str] | None = None) -> Path:
    """Create a small database with a ``processed_files`` table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE devices (api_key TEXT)")
        conn.executemany(
            "INSERT INTO devices VALUES (?)", [(f"key{i}",) for i in range(rows)]
        )
        conn.execute("CREATE TABLE processed_files (relative_path TEXT)")
        conn.executemany(
            "INSERT INTO processed_files VALUES (?)", [(r,) for r in refs or []]
        )
    return path


def run(*args: str | Path) -> Result:
    """Invoke the CLI with string arguments."""
    return CliRunner().invoke(cli.main, [str(a) for a in args])


def ok(*args: str | Path) -> Result:
    """Invoke the CLI and assert it succeeded."""
    result = run(*args)
    assert result.exit_code == 0, result.output
    return result


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    db = make_db(tmp_path / "srv" / "db.sqlite")
    archive = tmp_path / "kp.tar.zst"

    r = run("backup", archive, "--db-path", db, "--media-store", tmp_path / "m")
    assert r.exit_code == 0, r.output
    assert "Backed up database and 0 new media file(s)" in r.output

    target = tmp_path / "restored" / "db.sqlite"
    r = run("restore", archive, "--db-path", target, "--media-store", tmp_path / "rm")
    assert r.exit_code == 0, r.output
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT api_key FROM devices").fetchall() == [("key0",)]


def test_backup_db_only(tmp_path: Path) -> None:
    db = make_db(tmp_path / "db.sqlite")
    r = run(
        "backup",
        tmp_path / "bk",
        "--db-only",
        "--db-path",
        db,
        "--media-store",
        tmp_path / "m",
    )
    assert r.exit_code == 0, r.output
    assert "Backed up database ->" in r.output
    assert (tmp_path / "bk" / DB_NAME).is_file()


def test_paths_default_to_server_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = make_db(tmp_path / "db.sqlite")
    monkeypatch.setattr(cli.settings, "db_path", db)
    monkeypatch.setattr(cli.settings, "media_store_root", tmp_path / "m")
    r = run("backup", tmp_path / "kp.tar.zst")
    assert r.exit_code == 0, r.output
    assert (tmp_path / "kp.tar.zst").is_file()


def test_restore_refuses_non_empty_target_without_force(tmp_path: Path) -> None:
    src = make_db(tmp_path / "src.db", rows=1)
    archive = tmp_path / "kp.tar.zst"
    ok("backup", archive, "--db-path", src, "--media-store", tmp_path / "m")
    existing = make_db(tmp_path / "live.db", rows=3)

    r = run("restore", archive, "--db-path", existing, "--media-store", tmp_path / "m")
    assert r.exit_code == 1
    assert "not empty" in r.output
    assert "--force" in r.output

    r = run(
        "restore",
        archive,
        "--force",
        "--db-path",
        existing,
        "--media-store",
        tmp_path / "m",
    )
    assert r.exit_code == 0, r.output
    with sqlite3.connect(existing) as conn:
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone() == (1,)


def test_backup_missing_database_is_an_error(tmp_path: Path) -> None:
    r = run(
        "backup",
        tmp_path / "kp.tar.zst",
        "--db-path",
        tmp_path / "none.db",
        "--media-store",
        tmp_path / "m",
    )
    assert r.exit_code == 1
    assert "Database not found" in r.output


def test_backup_exits_non_zero_when_referenced_media_is_missing(
    tmp_path: Path,
) -> None:
    rel = f"audio/ab/{HASH}.mp3"
    db = make_db(tmp_path / "db.sqlite", refs=[rel])
    corrupt = tmp_path / "m" / rel
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not the content the hash names")

    r = run("backup", tmp_path / "bk", "--db-path", db, "--media-store", tmp_path / "m")
    assert r.exit_code == 1
    assert f"missing: {rel}" in r.output
    assert "1 incomplete skipped" in r.output


def test_restore_reports_corrupt_backup_media(tmp_path: Path) -> None:
    rel = f"audio/ab/{HASH}.mp3"
    backup_dir = tmp_path / "bk"
    make_db(tmp_path / "src.db", refs=[rel])
    ok(
        "backup",
        backup_dir,
        "--db-only",
        "--db-path",
        tmp_path / "src.db",
        "--media-store",
        tmp_path / "m",
    )
    bad = backup_dir / MEDIA_DIR / rel
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"bit rot")

    r = run(
        "restore",
        backup_dir,
        "--db-path",
        tmp_path / "r.db",
        "--media-store",
        tmp_path / "rm",
    )
    assert r.exit_code == 1
    assert f"corrupt, not restored: {rel}" in r.output
    assert f"missing: {rel}" in r.output


def test_backup_rejects_misnamed_archive(tmp_path: Path) -> None:
    db = make_db(tmp_path / "db.sqlite")
    r = run("backup", tmp_path / "foo.tar.gz", "--db-path", db)
    assert r.exit_code != 0
    assert "looks like an archive name" in r.output
    assert not (tmp_path / "foo.tar.gz").exists()


def test_backup_verify_rewrites_corrupt_copy(tmp_path: Path) -> None:
    from kidsplay_server.storage import MediaStore

    media = tmp_path / "m"
    src = tmp_path / "f.bin"
    src.write_bytes(b"photo bytes")
    _, rel = MediaStore(media).store(src, "photos", "_640x480.webp")
    db = make_db(tmp_path / "db.sqlite", refs=[rel])
    dest = tmp_path / "bk"
    ok("backup", dest, "--db-path", db, "--media-store", media)
    (dest / MEDIA_DIR / rel).write_bytes(b"rot")

    r = ok("backup", dest, "--verify", "--db-path", db, "--media-store", media)
    assert "1 corrupt backup file(s) rewritten" in r.output
    assert (dest / MEDIA_DIR / rel).read_bytes() == b"photo bytes"
