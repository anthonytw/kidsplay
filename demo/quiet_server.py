"""The demo server's app factory: the real app, with a quiet console.

``kidsplay_server`` logs every request-sized event at INFO on the console
(one line per imported file, and so on), which buries the demo's own friendly
output. This wraps ``create_app_from_env`` and raises only the *console*
handler to WARNING. The in-memory handler behind the web UI's log page keeps
everything, so the demo's Logs page still shows what happened. The server
package itself is unchanged.

Used as ``uvicorn demo.quiet_server:create_app --factory`` by
:func:`demo.seed.running_server`; ``python -m demo --verbose`` uses the plain
factory instead.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from typing import TYPE_CHECKING

from kidsplay_server.api.app import create_app_from_env
from kidsplay_server.config import settings
from kidsplay_server.logging_setup import configure_logging
from kidsplay_server.processing import queue_worker

if TYPE_CHECKING:
    from fastapi import FastAPI


def quiet_console(level: int = logging.WARNING) -> None:
    """Raise the console output to ``level``.

    Only plain stream handlers are touched (file and in-memory handlers keep
    their own levels), and uvicorn's own lifecycle messages ("Started server
    process", ...), which ``configure_logging`` sets to INFO, follow.

    Args:
        level: Lowest level the console still prints.
    """
    for handler in logging.getLogger().handlers:
        if type(handler) is logging.StreamHandler:
            handler.setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)


QUEUE_POLL_ENV = "KIDSPLAY_DEMO_QUEUE_POLL_SECONDS"
"""Test-only: how often the queue worker looks for new items (seconds).

Unset, the server's own default applies. A link pasted into the queue does not
wake the worker, so a test that waits on one would sit out the whole poll."""


def create_app() -> FastAPI:
    """Build the server app as ``create_app_from_env`` does, but quietly.

    Logging is configured (and quieted) *before* the app is built, because the
    app logs at INFO while it starts (importer discovery, the session key).
    ``configure_logging`` is idempotent, so the call inside
    ``create_app_from_env`` then changes nothing.

    Returns:
        The configured FastAPI application.
    """
    configure_logging(settings.log_file_path)
    quiet_console()
    poll = os.environ.get(QUEUE_POLL_ENV)
    if poll:
        queue_worker._POLL_INTERVAL = float(poll)  # ty: ignore[invalid-assignment]  # the module constant is an int literal; a test knob needs a float
    return create_app_from_env()
