"""Tests for ``kidsplay media normalize`` and loudness in ``media show``.

Runs against the real server from conftest.py. The tone is generated with
ffmpeg inside the test; no audio files are committed.
"""

import io
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result
from rich.console import Console

from kidsplay_cli import media as media_module
from kidsplay_cli.main import cli

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@pytest.fixture(autouse=True)
def quick_normalize_poll() -> Iterator[None]:
    """Let ``--wait`` look every 50 ms: the 3 s tones normalize in under a
    second, and the 2 s default would make every ``--wait`` sit out a full poll."""
    with patch.object(media_module, "_NORMALIZE_POLL_SECONDS", 0.05):
        yield


def run(server_url: str, *args: str) -> Result:
    """Invoke the CLI with --server injected and a wide terminal."""
    return CliRunner().invoke(
        cli,
        ["--server", server_url, *args],
        catch_exceptions=False,
        env={"COLUMNS": "200"},
    )


def ingest_tone(server_url: str, tmp_path: Path, name: str, frequency: int) -> str:
    """Generate a 3 s tone, ingest it as music, and return its media ID.

    Give each test its own ``frequency``: identical audio would be deduplicated.
    """
    path = tmp_path / f"{name}.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration=3",
            "-af",
            "volume=-12dB",
            str(path),
        ],
        check=True,
    )
    result = run(
        server_url, "media", "ingest", str(path), "--type", "music", "--playlist", name
    )
    assert result.exit_code == 0, result.output
    listing = run(server_url, "media", "list", "--search", name)
    match = re.search(_UUID, listing.output)
    assert match, listing.output
    return match.group(0)


class TestMediaNormalize:
    def test_requires_a_selection(self, server_url: str) -> None:
        result = run(server_url, "media", "normalize")
        assert result.exit_code == 2
        assert "--all" in result.output

    def test_all_and_ids_conflict(self, server_url: str) -> None:
        result = run(
            server_url,
            "media",
            "normalize",
            "--all",
            "00000000-0000-0000-0000-000000000001",
        )
        assert result.exit_code == 2

    def test_status_with_selection_conflicts(self, server_url: str) -> None:
        result = run(server_url, "media", "normalize", "--all", "--status")
        assert result.exit_code == 2

    def test_all_wait(self, server_url: str, tmp_path: Path) -> None:
        ingest_tone(server_url, tmp_path, "normalize-all-tone", 440)
        result = run(server_url, "media", "normalize", "--all", "--wait")
        assert result.exit_code == 0, result.output
        assert "Loudness backfill: finished" in result.output
        assert "0 failed" in result.output

        again = run(server_url, "media", "normalize", "--all", "--wait")
        # Everything is already at the target: nothing is re-encoded.
        assert again.exit_code == 0, again.output
        assert "0 normalized" in again.output

    def test_ids_and_status(self, server_url: str, tmp_path: Path) -> None:
        media_id = ingest_tone(server_url, tmp_path, "normalize-id-tone", 550)
        result = run(server_url, "media", "normalize", media_id, "--wait")
        assert result.exit_code == 0, result.output
        # Ingest returns before normalizing: the job it queued is the one this
        # request found, so it is normalized once.
        assert "1 normalized" in result.output
        assert "(of 1)" in result.output

        again = run(server_url, "media", "normalize", media_id, "--wait")
        assert again.exit_code == 0, again.output
        assert "1 skipped" in again.output
        assert "(of 1)" in again.output

        status = run(server_url, "media", "normalize", "--status")
        assert status.exit_code == 0
        assert "(of 1)" in status.output

    def test_errors_are_capped(self) -> None:
        output = io.StringIO()
        capture = Console(file=output, width=200)
        with patch.object(media_module, "console", capture):
            media_module._print_normalize_status(
                {
                    "running": False,
                    "started_at": "2026-01-01T00:00:00",
                    "total": 40,
                    "normalized": 5,
                    "skipped": 0,
                    "unchanged": 0,
                    "failed": 35,
                    "errors": [f"id-{i}: boom" for i in range(20)],
                    "errors_omitted": 15,
                }
            )
        lines = output.getvalue().splitlines()
        assert sum("boom" in line for line in lines) == 10
        assert "…and 25 more failed" in output.getvalue()

    def test_show_includes_loudness(self, server_url: str, tmp_path: Path) -> None:
        media_id = ingest_tone(server_url, tmp_path, "show-loudness-tone", 660)
        # Ingest queued the normalization; wait for the worker to do it.
        assert run(server_url, "media", "normalize", media_id, "--wait").exit_code == 0
        result = run(server_url, "media", "show", media_id)
        assert result.exit_code == 0
        assert re.search(r"Loudness:\s+-\d+\.\d LUFS → -16 LUFS \(\+", result.output)
