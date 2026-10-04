"""Wake the import queue worker as soon as a job is queued.

The worker polls the database every few seconds when the queue is empty. That
is fine for a link a parent pasted, but an upload would then wait several
seconds before its loudness normalization even starts. Whoever queues a job
calls ``wake`` right after committing it; the worker for that database
registers an event here and stops sleeping.

The registry is keyed by database path, so several apps in one process (the
tests) each wake only their own worker.
"""

import asyncio
from pathlib import Path

_workers: dict[Path, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}


def register(db_path: Path) -> asyncio.Event:
    """Register the running worker for ``db_path``.

    Must be called from the worker's event loop.

    Args:
        db_path: The database the worker serves.

    Returns:
        The event the worker should sleep on; it is set by ``wake``.
    """
    event = asyncio.Event()
    _workers[db_path] = (asyncio.get_running_loop(), event)
    return event


def unregister(db_path: Path, event: asyncio.Event) -> None:
    """Forget the worker registered with ``event``.

    Args:
        db_path: The database the worker served.
        event: The event ``register`` returned. A newer worker for the same
            database is left alone.
    """
    entry = _workers.get(db_path)
    if entry is not None and entry[1] is event:
        del _workers[db_path]


def wake(db_path: Path) -> None:
    """Wake the worker for ``db_path``, if one is running.

    Safe to call from any thread or event loop. Does nothing when no worker
    is running (the job is picked up whenever one starts).

    Args:
        db_path: The database a job was just queued in.
    """
    entry = _workers.get(db_path)
    if entry is None:
        return
    loop, event = entry
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        # The worker's loop has closed; the entry is stale.
        _workers.pop(db_path, None)
