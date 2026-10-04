"""``just demo``: a server, sample media and the player, in one command.

Starts a KidsPlay server with a temporary data directory, imports the bundled
sample media, creates two profiles (Ada and Leo) and a device for Ada, syncs
it, then opens the player in a window. Closing the player window (or Esc)
stops everything and deletes the temporary data.

Usage::

    uv run --all-packages python -m demo [--port 8000] [--keep] [--no-player]
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from demo.seed import (
    DEMO_ADMIN_PASSWORD,
    MEDIA_DIR,
    PROFILES,
    device_config,
    login,
    running_server,
    seed,
    sync_device,
    write_device_home,
)

CONTROLS = """\
Player controls (keyboard stands in for the handheld's buttons):
  arrows          move
  Enter / A       select
  B / Backspace   back
  X / Space       play / pause
  Y               repeat mode
  Esc             quit the demo
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the demo's command-line options.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` if None.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        prog="just demo", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "--port", type=int, default=None, help="server port (default: any free port)"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the temporary data directory"
    )
    parser.add_argument(
        "--no-player",
        action="store_true",
        help="only run the seeded server (web UI), until Ctrl-C",
    )
    return parser.parse_args(argv)


def player_command() -> list[str]:
    """Command that runs the device player with the current interpreter.

    Returns:
        An argv list equivalent to ``kidsplay-player``.
    """
    return [sys.executable, "-c", "from kidsplay_device.app import main; main()"]


def _ignore_further_interrupts() -> None:
    """Ignore SIGINT from here on, so cleanup runs to completion."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def run(args: argparse.Namespace) -> int:
    """Run the demo until the player (or, with ``--no-player``, Ctrl-C) exits.

    Args:
        args: Options from :func:`parse_args`.

    Returns:
        Process exit code.
    """
    if shutil.which("ffmpeg") is None:
        print(
            "warning: ffmpeg is not on PATH. The demo's MP3s import without it "
            "(not loudness-normalized), "
            "but importing other formats or URLs from the web UI will fail.",
            file=sys.stderr,
        )

    # Turn SIGTERM into a normal exit so the cleanup below still runs (it stops
    # the server process and deletes the temporary data).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))

    data_dir = Path(tempfile.mkdtemp(prefix="kidsplay-demo-"))
    device_home = data_dir / "device-home"
    try:
        print(f"Starting the server (data in {data_dir}) ...")
        with running_server(data_dir, args.port) as base_url:
            print(f"Importing sample media from {MEDIA_DIR} ...")
            with httpx.Client(base_url=base_url, timeout=300.0) as client:
                login(client)
                device = seed(client)
            config = device_config(base_url, device, device_home)
            write_device_home(config, device_home)
            files = sync_device(config)
            print(
                f"Created profiles {', '.join(PROFILES)} and a device for "
                f"{PROFILES[0]}; synced {files} files to it.\n"
            )
            print(f"Web UI:  {base_url}/  (password: {DEMO_ADMIN_PASSWORD})\n")

            if args.no_player:
                print("Press Ctrl-C to stop.")
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    _ignore_further_interrupts()
                    return 0

            print(CONTROLS)
            env = dict(os.environ, HOME=str(device_home))
            try:
                return subprocess.run(player_command(), env=env, check=False).returncode
            except KeyboardInterrupt:
                _ignore_further_interrupts()
                return 0
    finally:
        # A second Ctrl-C (``just`` forwards SIGINT to us as well as the
        # terminal sending it) must not cut the cleanup short.
        _ignore_further_interrupts()
        if args.keep:
            print(f"Kept demo data in {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``python -m demo``.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` if None.
    """
    sys.exit(run(parse_args(argv)))


if __name__ == "__main__":
    main()
