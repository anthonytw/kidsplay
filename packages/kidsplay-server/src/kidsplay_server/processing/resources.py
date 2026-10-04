"""Resource budget for the server's external processing jobs (ffmpeg).

A dedicated server can run these flat out. In all-in-one mode the server
shares a Raspberry Pi CM4 with the player, whose UI and audio must not stutter
while a parent imports an album. Two limits, both off by default:

``nice``
    Run every ffmpeg subprocess at this niceness (0-19, 0 = unchanged).

``max_jobs``
    Run at most this many ffmpeg subprocesses at once across the whole
    server (0 = unlimited). Ingest requests, the import queue (loudness
    normalization and importers) all share the limit. Importer subprocesses
    such as yt-dlp count as one job for their whole run, ffmpeg
    post-processing included, so they never run beside a normalization.

The limits are process-wide by nature (they bound this machine's CPU), so
they are configured once by ``create_app`` and read by ``run_limited`` (for
blocking code in a worker thread) and ``run_limited_async`` (for asyncio
code).

A job can be cancelled: ``run_limited`` takes a ``threading.Event`` and
``run_limited_async`` is cancelled with its task. The process (and any
children it started, such as yt-dlp's ffmpeg) is then killed instead of being
waited for, so a server shutdown does not sit through a long encode.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_NICE = 19

_POLL_SECONDS = 0.2
"""How often a waiting job checks whether it was cancelled."""


class JobCancelledError(Exception):
    """A limited job was cancelled before it finished; its process was killed."""


@dataclass(frozen=True)
class ResourceLimits:
    """Limits on external processing jobs.

    Attributes:
        nice: Niceness for ffmpeg, 0 (unchanged) to 19 (lowest priority).
        max_jobs: Concurrent ffmpeg jobs allowed; 0 means no limit.
    """

    nice: int = 0
    max_jobs: int = 0

    def __post_init__(self) -> None:
        """Validate the limits.

        Raises:
            ValueError: If ``nice`` is outside 0-19 or ``max_jobs`` is negative.
        """
        if not 0 <= self.nice <= MAX_NICE:
            raise ValueError(f"nice must be between 0 and {MAX_NICE}, not {self.nice}")
        if self.max_jobs < 0:
            raise ValueError(f"max_jobs must be 0 or more, not {self.max_jobs}")


_lock = threading.Lock()
_limits = ResourceLimits()
_slots: threading.BoundedSemaphore | None = None
_warned_no_nice = False
_live: dict[threading.Event, set[int]] = {}
"""Process-group ids of running cancellable jobs, by their cancel event."""


def configure_limits(limits: ResourceLimits) -> None:
    """Set the process-wide limits (called by ``create_app``).

    Jobs already running finish under the previous limits.

    Args:
        limits: The limits to apply from now on.
    """
    global _limits, _slots
    with _lock:
        _limits = limits
        _slots = (
            threading.BoundedSemaphore(limits.max_jobs) if limits.max_jobs else None
        )


def get_limits() -> ResourceLimits:
    """Return the limits currently in force."""
    return _limits


def _command(cmd: list[str], nice: int) -> list[str]:
    """Prefix ``cmd`` with ``nice`` when a niceness is set and nice exists."""
    global _warned_no_nice
    if nice == 0:
        return cmd
    nice_bin = shutil.which("nice")
    if nice_bin is None:
        if not _warned_no_nice:
            logger.warning("`nice` not found; running processing at normal priority")
            _warned_no_nice = True
        return cmd
    return [nice_bin, "-n", str(nice), *cmd]


def _kill_tree(pid: int) -> None:
    """Kill a job's process group (the job started its own session)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def kill_jobs(cancel: threading.Event) -> None:
    """Set ``cancel`` and kill its running jobs' process groups right now.

    ``run_limited`` notices a set event only on its next poll, up to a
    fraction of a second later. A caller that is about to let the process
    exit (server shutdown) uses this to kill synchronously instead, so no
    ffmpeg is orphaned.

    Args:
        cancel: The event passed to ``run_limited``.
    """
    cancel.set()
    with _lock:
        pids = list(_live.get(cancel, ()))
    for pid in pids:
        _kill_tree(pid)


def _acquire(slots: threading.BoundedSemaphore, cancel: threading.Event) -> None:
    """Wait for a job slot, giving up if ``cancel`` is set.

    Raises:
        JobCancelledError: If ``cancel`` was set while waiting.
    """
    while not slots.acquire(timeout=_POLL_SECONDS):
        if cancel.is_set():
            raise JobCancelledError("cancelled while waiting for a job slot")
    if cancel.is_set():
        slots.release()
        raise JobCancelledError("cancelled while waiting for a job slot")


def _run_cancellable(
    cmd: list[str], cancel: threading.Event
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` in its own process group, killing it if ``cancel`` is set."""
    if cancel.is_set():
        raise JobCancelledError("cancelled before it started")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    with _lock:
        _live.setdefault(cancel, set()).add(proc.pid)
    try:
        return _wait_cancellable(cmd, proc, cancel)
    finally:
        with _lock:
            pids = _live.get(cancel)
            if pids is not None:
                pids.discard(proc.pid)
                if not pids:
                    del _live[cancel]


def _wait_cancellable(
    cmd: list[str], proc: subprocess.Popen[str], cancel: threading.Event
) -> subprocess.CompletedProcess[str]:
    """Wait for ``proc``, killing its group if ``cancel`` is set."""
    if cancel.is_set():  # set between the first check and registration
        _kill_tree(proc.pid)
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                _kill_tree(proc.pid)
                proc.communicate()
                raise JobCancelledError("cancelled while running") from None
        except BaseException:
            _kill_tree(proc.pid)
            proc.communicate()
            raise
    if cancel.is_set() and proc.returncode == -signal.SIGKILL:
        raise JobCancelledError("cancelled while running")  # killed by kill_jobs
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def run_limited(
    cmd: list[str], *, cancel: threading.Event | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command under the configured niceness and job limit.

    Blocks until a job slot is free. Output is captured as text (undecodable
    bytes replaced) and a non-zero exit does not raise.

    Args:
        cmd: The command line.
        cancel: If given and set (from another thread) while the command is
            waiting or running, the command is killed and ``JobCancelledError``
            raised, within a fraction of a second.

    Returns:
        The completed process.

    Raises:
        FileNotFoundError: If the executable does not exist.
        JobCancelledError: If ``cancel`` was set before the command finished.
    """
    with _lock:
        limits, slots = _limits, _slots
    if slots is not None:
        if cancel is None:
            slots.acquire()
        else:
            _acquire(slots, cancel)
    try:
        full = _command(cmd, limits.nice)
        if cancel is not None:
            return _run_cancellable(full, cancel)
        return subprocess.run(
            full,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    finally:
        if slots is not None:
            slots.release()


@contextlib.asynccontextmanager
async def _slot(slots: threading.BoundedSemaphore | None) -> AsyncIterator[None]:
    """Hold a job slot for the ``async with`` body (a no-op without a limit).

    Polls instead of blocking a thread, so a task waiting for a slot can be
    cancelled without leaking one.
    """
    if slots is None:
        yield
        return
    while not slots.acquire(blocking=False):
        await asyncio.sleep(_POLL_SECONDS / 2)
    try:
        yield
    finally:
        slots.release()


async def run_limited_async(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run a command under the niceness and job limit, from asyncio code.

    For importer subprocesses (yt-dlp): the whole command, including any
    ffmpeg it starts itself, occupies one job slot, and everything runs at
    the configured niceness (children inherit it). Cancelling the awaiting
    task kills the command and its children.

    Args:
        cmd: The command line.

    Returns:
        The completed process, with output as bytes.

    Raises:
        OSError: If the command cannot be started (e.g. missing executable).
    """
    with _lock:
        limits, slots = _limits, _slots
    async with _slot(slots):
        proc = await asyncio.create_subprocess_exec(
            *_command(cmd, limits.nice),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await proc.communicate()
        except BaseException:
            _kill_tree(proc.pid)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
    return subprocess.CompletedProcess(
        cmd, proc.returncode if proc.returncode is not None else -1, stdout, stderr
    )
