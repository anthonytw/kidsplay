"""Centralised logging configuration for the KidsPlay server.

Sets up three log destinations:
- Console (INFO+): clean output for container/terminal logs.
- File (DEBUG+): ``TimedRotatingFileHandler`` with daily rotation and 7-day
  backups — the primary tool for post-mortem debugging.
- In-memory ring buffer (DEBUG+): feeds the real-time ``/logs`` SSE endpoint
  in the web UI without any disk I/O per viewer.

Call ``configure_logging()`` once at startup (``create_app_from_env``), then
use ``logging.getLogger(__name__)`` in every module as usual.

The ``get_broadcaster()`` function returns the module-level singleton used by
the SSE streaming endpoint.
"""

import asyncio
import contextlib
import json
import logging
import logging.handlers
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class LogEntry:
    """A single structured log record stored in the ring buffer."""

    ts: str  # ISO-8601 timestamp with milliseconds
    level: str  # DEBUG / INFO / WARNING / ERROR / CRITICAL
    logger: str  # Logger name, e.g. ``kidsplay_server.processing.pipeline``
    message: str  # Raw log message (may include formatted traceback)

    def to_sse_data(self) -> str:
        """Serialize to a JSON string suitable for an SSE ``data:`` payload."""
        return json.dumps(
            {
                "ts": self.ts,
                "level": self.level,
                "logger": self.logger,
                "message": self.message,
            },
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Broadcaster (pub/sub for SSE clients)
# ---------------------------------------------------------------------------


class LogBroadcaster:
    """In-memory ring buffer with asyncio pub/sub for SSE log streaming.

    ``InMemoryLogHandler.emit()`` calls ``add_entry()`` synchronously from
    within the asyncio event loop thread (all kidsplay-server logging happens
    inside coroutines or asyncio tasks).  Each connected SSE client gets its
    own ``asyncio.Queue``; slow consumers receive up to *maxqueue* entries
    before older ones are silently dropped.

    Args:
        maxlen: Maximum number of log entries retained in the ring buffer.
        maxqueue: Maximum depth of each per-subscriber queue.
    """

    def __init__(self, maxlen: int = 500, maxqueue: int = 200) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=maxlen)
        self._subscribers: list[asyncio.Queue[LogEntry]] = []
        self._maxqueue = maxqueue

    def add_entry(self, entry: LogEntry) -> None:
        """Append *entry* to the ring buffer and notify all SSE subscribers."""
        self._entries.append(entry)
        for q in list(self._subscribers):
            # Slow consumer; drop rather than block the logging path.
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(entry)

    def get_recent(self) -> list[LogEntry]:
        """Return a snapshot of buffered entries, oldest first."""
        return list(self._entries)

    def subscribe(self, q: "asyncio.Queue[LogEntry]") -> None:
        """Register *q* to receive new log entries."""
        self._subscribers.append(q)

    def unsubscribe(self, q: "asyncio.Queue[LogEntry]") -> None:
        """Remove *q* from the subscriber list (safe if already removed)."""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(q)


# ---------------------------------------------------------------------------
# logging.Handler that feeds the broadcaster
# ---------------------------------------------------------------------------


class InMemoryLogHandler(logging.Handler):
    """A ``logging.Handler`` that pushes formatted records to a ``LogBroadcaster``.

    Args:
        broadcaster: The broadcaster to push entries to.
    """

    def __init__(self, broadcaster: LogBroadcaster) -> None:
        super().__init__()
        self._broadcaster = broadcaster

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += (
                    "\n"
                    + "".join(traceback.format_exception(*record.exc_info)).rstrip()
                )
            entry = LogEntry(
                ts=datetime.fromtimestamp(record.created).isoformat(
                    timespec="milliseconds"
                ),
                level=record.levelname,
                logger=record.name,
                message=msg,
            )
            self._broadcaster.add_entry(entry)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_broadcaster = LogBroadcaster(maxlen=500)
_configured = False


def get_broadcaster() -> LogBroadcaster:
    """Return the module-level ``LogBroadcaster`` singleton."""
    return _broadcaster


# ---------------------------------------------------------------------------
# Public configuration entry point
# ---------------------------------------------------------------------------


def configure_logging(log_file: Path | None = None) -> None:
    """Configure server-wide logging.  Safe to call multiple times (idempotent).

    Attaches three handlers to the root logger:

    * **Console** — ``INFO`` and above, for readable terminal / container output.
    * **File** — ``DEBUG`` and above, daily-rotating with 7-day retention, for
      post-mortem analysis.  Omitted when *log_file* is ``None``.
    * **In-memory** — ``DEBUG`` and above, feeds the ``/logs`` SSE endpoint.

    Noisy third-party loggers (``uvicorn.access``, ``httpx``, ``aiosqlite``)
    are raised to ``WARNING`` so they do not pollute the web UI or log file.

    Args:
        log_file: Destination path for the rotating log file.  Parent
            directories are created automatically.  Pass ``None`` to disable
            file logging (e.g. in unit tests).
    """
    global _configured
    if _configured:
        return
    _configured = True

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console — INFO+ keeps stdout readable.
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    # File — DEBUG+ with daily rotation (7-day retention).
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # In-memory ring buffer — DEBUG+ for the web UI SSE stream.
    mem = InMemoryLogHandler(_broadcaster)
    mem.setLevel(logging.DEBUG)
    root.addHandler(mem)

    # Quieten noisy third-party loggers.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)
