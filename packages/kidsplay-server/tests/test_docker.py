"""Tests for the Docker build files (checked as text; no image is built)."""

import os
import subprocess
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).resolve().parents[3] / "docker"
SELECT = DOCKER_DIR / "select-server-package.sh"


def _select(value: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "KIDSPLAY_WITH_YTDLP"}
    if value is not None:
        env["KIDSPLAY_WITH_YTDLP"] = value
    return subprocess.run(
        ["sh", str(SELECT)], capture_output=True, text=True, env=env, check=False
    )


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", "ON"])
def test_truthy_values_install_the_ytdlp_plugin(value: str) -> None:
    result = _select(value)
    assert result.returncode == 0
    assert result.stdout.strip() == "kidsplay-importer-ytdlp"


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "Off"])
def test_falsy_values_build_core_only(value: str) -> None:
    result = _select(value)
    assert result.returncode == 0
    assert result.stdout.strip() == "kidsplay-server"


@pytest.mark.parametrize("value", ["", "2", "tru", "enabled", "y"])
def test_anything_else_fails_loudly(value: str) -> None:
    result = _select(value)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "KIDSPLAY_WITH_YTDLP must be" in result.stderr


def test_unset_fails_loudly() -> None:
    assert _select(None).returncode != 0


def test_dockerfile_uses_the_selector_and_never_syncs_at_start() -> None:
    dockerfile = (DOCKER_DIR / "Dockerfile").read_text()
    assert "select-server-package" in dockerfile
    entrypoint = dockerfile[dockerfile.index("ENTRYPOINT") :]
    # `uv run` without --no-sync installs the dev group on every start.
    assert '"uv", "run", "--no-sync"' in entrypoint
