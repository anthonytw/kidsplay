"""Tests for the demo server's quiet app factory (``demo/quiet_server.py``)."""

import logging
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from demo import quiet_server
from demo.seed import REPO_ROOT, server_env


@pytest.fixture
def root_handlers() -> Iterator[logging.Logger]:
    """The root logger, with its handlers restored afterwards."""
    root = logging.getLogger()
    saved = list(root.handlers)
    yield root
    root.handlers[:] = saved


def test_quiet_console_raises_only_the_plain_stream_handler(
    root_handlers: logging.Logger, tmp_path: Path
) -> None:
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    file_handler = logging.FileHandler(tmp_path / "x.log")
    file_handler.setLevel(logging.DEBUG)
    memory = logging.Handler(logging.DEBUG)
    root_handlers.handlers[:] = [console, file_handler, memory]

    quiet_server.quiet_console()

    assert console.level == logging.WARNING
    assert file_handler.level == logging.DEBUG  # FileHandler is a StreamHandler too
    assert memory.level == logging.DEBUG
    file_handler.close()


def test_the_real_factory_prints_warnings_but_not_info(tmp_path: Path) -> None:
    """In a fresh process (logging is global): info is hushed, warnings are not."""
    code = textwrap.dedent(
        """
        import logging
        from demo.quiet_server import create_app
        create_app()
        logging.getLogger("uvicorn.error").info("uvicorn-info-line")
        logging.getLogger("kidsplay_server.x").info("info-line-shown")
        logging.getLogger("kidsplay_server.x").warning("warning-line-shown")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=server_env(tmp_path),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    output = result.stdout + result.stderr
    assert "warning-line-shown" in output
    assert "info-line-shown" not in output
    assert "uvicorn-info-line" not in output


def test_demo_hides_the_pygame_banner() -> None:
    import os

    import demo

    assert demo is not None
    assert os.environ["PYGAME_HIDE_SUPPORT_PROMPT"]
