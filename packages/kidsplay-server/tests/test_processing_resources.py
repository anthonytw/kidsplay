"""Tests for the ffmpeg resource budget (nice level, one job at a time)."""

import asyncio
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from kidsplay_server.api.app import create_app
from kidsplay_server.config import Settings
from kidsplay_server.processing import audio, resources
from kidsplay_server.processing.resources import (
    JobCancelledError,
    ResourceLimits,
    configure_limits,
    get_limits,
    run_limited,
    run_limited_async,
)


@pytest.fixture(autouse=True)
def _reset_limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("KIDSPLAY_PROCESSING_NICE", raising=False)
    monkeypatch.delenv("KIDSPLAY_PROCESSING_JOBS", raising=False)
    configure_limits(ResourceLimits())
    yield
    configure_limits(ResourceLimits())


class TestResourceLimits:
    def test_defaults_change_nothing(self) -> None:
        assert ResourceLimits() == ResourceLimits(nice=0, max_jobs=0)

    @pytest.mark.parametrize("nice", [-1, 20])
    def test_nice_out_of_range(self, nice: int) -> None:
        with pytest.raises(ValueError, match="nice"):
            ResourceLimits(nice=nice)

    def test_negative_jobs(self) -> None:
        with pytest.raises(ValueError, match="max_jobs"):
            ResourceLimits(max_jobs=-1)


class TestNice:
    def test_child_runs_at_lower_priority(self) -> None:
        configure_limits(ResourceLimits(nice=7))
        proc = run_limited(["python3", "-c", "import os; print(os.nice(0))"])
        assert proc.returncode == 0
        assert int(proc.stdout) == min(19, os.nice(0) + 7)

    def test_zero_nice_runs_the_command_as_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(resources.subprocess, "run", fake_run)
        run_limited(["ffmpeg", "-version"])
        assert seen == [["ffmpeg", "-version"]]

    def test_missing_nice_binary_falls_back_with_one_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(resources.shutil, "which", lambda _name: None)
        monkeypatch.setattr(resources, "_warned_no_nice", False)
        assert resources._command(["ffmpeg"], 10) == ["ffmpeg"]
        assert resources._command(["ffmpeg"], 10) == ["ffmpeg"]
        assert len([r for r in caplog.records if "nice" in r.message]) == 1

    def test_ffmpeg_goes_through_the_limits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure_limits(ResourceLimits(nice=10))
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "report")

        monkeypatch.setattr(resources.subprocess, "run", fake_run)
        assert audio._run_ffmpeg(["-i", "x.mp3"]) == "report"
        assert seen[0][1:3] == ["-n", "10"]
        assert seen[0][3:5] == ["ffmpeg", "-hide_banner"]
        assert seen[0][0].endswith("nice")


class TestJobLimit:
    @staticmethod
    def _peak_concurrency(
        monkeypatch: pytest.MonkeyPatch, max_jobs: int, threads: int = 4
    ) -> int:
        configure_limits(ResourceLimits(max_jobs=max_jobs))
        lock = threading.Lock()
        running = peak = 0

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(resources.subprocess, "run", fake_run)
        pool = [
            threading.Thread(target=run_limited, args=(["x"],)) for _ in range(threads)
        ]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        return peak

    def test_one_job_at_a_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._peak_concurrency(monkeypatch, max_jobs=1) == 1

    def test_two_jobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._peak_concurrency(monkeypatch, max_jobs=2) == 2

    def test_unlimited_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._peak_concurrency(monkeypatch, max_jobs=0) > 1

    def test_slot_is_released_when_the_command_is_missing(self) -> None:
        configure_limits(ResourceLimits(max_jobs=1))
        for _ in range(2):  # a leaked slot would hang the second call
            with pytest.raises(FileNotFoundError):
                run_limited(["definitely-not-a-real-executable"])


class TestCancel:
    """A shutdown must not wait for a long ffmpeg encode."""

    def test_running_command_is_killed(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(JobCancelledError):
            run_limited(["sleep", "30"], cancel=cancel)
        assert time.monotonic() - started < 5

    def test_kill_jobs_kills_synchronously_without_waiting_for_a_poll(
        self, tmp_path: Path
    ) -> None:
        pid_file = tmp_path / "pid"
        cancel = threading.Event()
        outcome: list[BaseException] = []

        def job() -> None:
            try:
                run_limited(
                    ["sh", "-c", f"echo $$ > {pid_file}; exec sleep 60"], cancel=cancel
                )
            except JobCancelledError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=job)
        thread.start()
        deadline = time.monotonic() + 5
        while not (pid_file.exists() and pid_file.read_text().strip()):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pid = int(pid_file.read_text())

        resources.kill_jobs(cancel)
        # The process group is dead on return, before the job's next poll.
        # It may be a zombie until the job's thread reaps it.
        thread.join(5)
        assert not thread.is_alive()
        assert outcome
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not resources._live

    def test_children_of_the_command_are_killed_too(self, tmp_path: Path) -> None:
        marker = tmp_path / "child-survived"
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        script = f"(sleep 2; touch {marker}) & wait"
        with pytest.raises(JobCancelledError):
            run_limited(["sh", "-c", script], cancel=cancel)
        time.sleep(2.5)
        assert not marker.exists()

    def test_waiting_for_a_slot_is_cancellable_and_leaks_nothing(self) -> None:
        configure_limits(ResourceLimits(max_jobs=1))
        slots = resources._slots
        assert slots is not None
        slots.acquire()  # another job holds the only slot
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        with pytest.raises(JobCancelledError):
            run_limited(["true"], cancel=cancel)
        slots.release()
        assert run_limited(["true"]).returncode == 0

    def test_uncancelled_run_returns_output_and_status(self) -> None:
        proc = run_limited(
            ["sh", "-c", "echo out; echo err >&2; exit 3"], cancel=threading.Event()
        )
        assert (proc.stdout.strip(), proc.stderr.strip(), proc.returncode) == (
            "out",
            "err",
            3,
        )

    def test_cancelled_before_start_runs_nothing(self) -> None:
        configure_limits(ResourceLimits(max_jobs=1))
        cancel = threading.Event()
        cancel.set()
        slots = resources._slots
        assert slots is not None
        slots.acquire()
        with pytest.raises(JobCancelledError):
            run_limited(["true"], cancel=cancel)
        slots.release()


class TestRunLimitedAsync:
    async def test_returns_bytes_and_status(self) -> None:
        proc = await run_limited_async(["sh", "-c", "echo out; echo err >&2; exit 2"])
        assert (proc.stdout, proc.stderr, proc.returncode) == (b"out\n", b"err\n", 2)

    async def test_runs_at_the_configured_niceness(self) -> None:
        configure_limits(ResourceLimits(nice=7))
        proc = await run_limited_async(
            ["python3", "-c", "import os; print(os.nice(0))"]
        )
        assert int(proc.stdout) == min(19, os.nice(0) + 7)

    async def test_one_job_at_a_time_across_sync_and_async(self) -> None:
        """An async job (yt-dlp) waits for a running thread job (ffmpeg)."""
        configure_limits(ResourceLimits(max_jobs=1))
        slots = resources._slots
        assert slots is not None
        slots.acquire()
        task = asyncio.create_task(run_limited_async(["true"]))
        await asyncio.sleep(0.5)
        assert not task.done()
        slots.release()
        assert (await asyncio.wait_for(task, 5)).returncode == 0

    async def test_cancelling_kills_the_process_and_frees_the_slot(self) -> None:
        configure_limits(ResourceLimits(max_jobs=1))
        task = asyncio.create_task(run_limited_async(["sleep", "30"]))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        proc = await asyncio.wait_for(run_limited_async(["true"]), 5)
        assert proc.returncode == 0

    async def test_cancel_while_waiting_for_a_slot_does_not_leak_one(self) -> None:
        configure_limits(ResourceLimits(max_jobs=1))
        slots = resources._slots
        assert slots is not None
        slots.acquire()
        task = asyncio.create_task(run_limited_async(["true"]))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        slots.release()
        assert slots.acquire(blocking=False)  # still exactly one slot
        assert not slots.acquire(blocking=False)
        slots.release()

    async def test_missing_executable_raises_and_frees_the_slot(self) -> None:
        configure_limits(ResourceLimits(max_jobs=1))
        for _ in range(2):
            with pytest.raises(FileNotFoundError):
                await run_limited_async(["definitely-not-a-real-executable"])


class TestConfiguration:
    def test_env_defaults(self) -> None:
        assert Settings().limits == ResourceLimits()

    def test_env_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIDSPLAY_PROCESSING_NICE", "10")
        monkeypatch.setenv("KIDSPLAY_PROCESSING_JOBS", "1")
        assert Settings().limits == ResourceLimits(nice=10, max_jobs=1)

    @pytest.mark.parametrize(
        ("var", "value"),
        [
            ("KIDSPLAY_PROCESSING_NICE", "high"),
            ("KIDSPLAY_PROCESSING_NICE", "25"),
            ("KIDSPLAY_PROCESSING_JOBS", "-2"),
            ("KIDSPLAY_PROCESSING_JOBS", "1.5"),
        ],
    )
    def test_bad_values_stop_startup(
        self, monkeypatch: pytest.MonkeyPatch, var: str, value: str
    ) -> None:
        monkeypatch.setenv(var, value)
        with pytest.raises(ValueError, match=r"nice|max_jobs|whole number"):
            Settings()

    def test_create_app_applies_and_resets_limits(self, tmp_path: Path) -> None:
        create_app(tmp_path / "a.db", tmp_path / "a", limits=ResourceLimits(3, 1))
        assert get_limits() == ResourceLimits(nice=3, max_jobs=1)
        create_app(tmp_path / "b.db", tmp_path / "b")
        assert get_limits() == ResourceLimits()
