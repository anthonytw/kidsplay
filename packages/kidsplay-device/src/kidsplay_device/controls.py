"""Parental controls: volume cap, bedtime and the clock they rely on.

Pure logic -- no pygame. ``app.py`` owns the mixer calls and asks this
module *what* volume to use and *whether* it is bedtime.

Volume
------
``VolumeControl`` turns the profile's ``max_volume`` (and, when the in-app
volume buttons are enabled, the kid's own level) into the value passed to
``pygame.mixer.music.set_volume``. That is digital attenuation before the OS
mixer and any hardware volume dial, both of which only multiply it further
down, so the cap bounds the loudest possible output whatever those controls
are set to.

Bedtime and the clock
---------------------
The handheld has no RTC. It may boot offline with a clock restored from its
last shutdown, or one that is simply wrong, and ``kidsplay-timeset`` fixes
it only once the server is reachable. ``TimeSource`` therefore picks the
best time available:

1. The server's clock, when a sync since boot has seen the server's
   ``Date`` header: that time plus the monotonic time elapsed since.
2. Otherwise the system clock, unless it reads *earlier* than the last time
   the server was seen. A clock going backwards past a real sync is certainly
   wrong, so bedtime is not enforced (and this is logged) rather than risk
   locking a kid out at noon.

3. Otherwise, if the clock looks *restored at boot* (see below), bedtime
   is not enforced until a sync confirms the time.

A server sync is not the only confirmation. Once ``timedatectl`` reports
``NTPSynchronized=yes``, the system clock has been corrected by a real time
source, so it is trusted whatever the checks above say (``run_ntp_watch``
polls for it in the background, so a device that gets network late catches up
without a restart). That is what confirms the time in all-in-one mode, where a
sync with the local server cannot. Without a network, NTP never reports
synchronized and bedtime fails open as before. The player never *waits* for
NTP at boot: a wait with no route to an NTP peer would hold the kiosk at a
black screen.

Restored clocks: Pi OS restores the clock at boot from the last shutdown
(fake-hwclock / timesyncd), so a handheld switched off at 20:30 and started
offline at noon reads 20:31 -- later than the last sync, yet 15 hours wrong.
The app therefore records a *heartbeat* (the wall clock, the kernel boot id
and whether the clock was trusted) every few minutes and on clean shutdown.
At startup, a clock within ``RESTORED_CLOCK_WINDOW`` after the heartbeat of a
*different* boot (or before it) has the signature of a restored clock, and
stays untrusted for the whole boot until a sync anchors it: a restored clock
lags real time by however long the device was off, and keeps lagging as it
runs. A heartbeat from the *same* boot means the clock has run continuously
(a player restart), so it is trusted unless that heartbeat was itself marked
untrusted.

Remaining limits: a device off for less than the window, or with no
heartbeat yet (first run), cannot be told from a device that was simply
restarted; and a restored clock more than the window after the heartbeat is
trusted. See ``docs/SETTINGS.md``.
"""

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from kidsplay_models import BedtimeMode, ProfileSettings, Weekday

from .database import ClockHeartbeat

logger = logging.getLogger(__name__)

FADE_SECONDS: float = 10.0
"""How long playback fades out when bedtime starts."""

UI_SOUND_LEVEL: float = 0.50
"""UI sound level before the volume cap is applied."""

DUCK_TAIL_SECONDS: float = 0.1
"""Extra time the music stays ducked after a UI sound's nominal end, to cover
the mixer's output latency (a couple of buffers)."""

VOLUME_STEP: int = 10
"""Percentage points per press of an in-app volume button."""

RESTORED_CLOCK_WINDOW: timedelta = timedelta(minutes=15)
"""A clock this close after the last heartbeat (or before it) at boot looks
like one restored from the previous shutdown rather than real elapsed time."""

HEARTBEAT_INTERVAL: float = 300.0
"""Seconds between wall-clock heartbeat writes (SD-card friendly)."""

NTP_POLL_INTERVAL: float = 30.0
"""Seconds between checks for NTP synchronization while it is not yet confirmed."""

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def read_boot_id() -> str | None:
    """Return the kernel's id for this boot, or None where unavailable."""
    try:
        return _BOOT_ID_PATH.read_text().strip() or None
    except OSError:
        return None


def read_ntp_synchronized() -> bool:
    """Ask systemd whether the system clock has been synchronized by NTP.

    Runs ``timedatectl show -p NTPSynchronized --value``.

    Returns:
        True only when systemd says ``yes``. False when it says ``no`` or
        ``timedatectl`` is missing, slow or fails (not a systemd system):
        an unknown clock stays untrusted.
    """
    try:
        proc = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "yes"


def run_ntp_watch(
    time_source: "TimeSource",
    *,
    probe: Callable[[], bool] = read_ntp_synchronized,
    interval: float = NTP_POLL_INTERVAL,
    stop: threading.Event | None = None,
) -> None:
    """Poll until NTP has synchronized the clock, then tell ``time_source``.

    Meant for a daemon thread. Returns after the first positive probe, or when
    ``stop`` is set.

    Args:
        time_source: The clock to confirm.
        probe: Returns True once the system clock is NTP-synchronized.
        interval: Seconds between probes.
        stop: Event that ends the watch early.
    """
    stop = stop or threading.Event()
    while not stop.is_set():
        if probe():
            time_source.note_ntp_synchronized()
            return
        stop.wait(interval)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


class VolumeControl:
    """Effective playback volume under the profile's cap.

    Args:
        settings: The profile settings (cap and whether buttons are on).
        level: The kid's own level in percent, as remembered across reboots.
            Only used while ``settings.volume_buttons`` is on.
    """

    def __init__(self, settings: ProfileSettings, level: int = 100) -> None:
        self._settings = settings
        self._level = max(0, min(100, level))

    @property
    def settings(self) -> ProfileSettings:
        """The profile settings in force."""
        return self._settings

    @settings.setter
    def settings(self, settings: ProfileSettings) -> None:
        self._settings = settings

    @property
    def level(self) -> int:
        """The kid's own level in percent (persisted by the app)."""
        return self._level

    @property
    def cap(self) -> int:
        """The profile's ``max_volume`` in percent."""
        return self._settings.max_volume

    def effective_percent(self) -> int:
        """Playback volume in percent: never above the cap.

        Returns:
            ``min(level, cap)`` with volume buttons on, else the cap.
        """
        if self._settings.volume_buttons:
            return min(self._level, self.cap)
        return self.cap

    def music_volume(self) -> float:
        """Value for ``pygame.mixer.music.set_volume`` (0.0 to 1.0).

        Returns:
            The effective volume as a fraction.
        """
        return self.effective_percent() / 100

    def ui_volume(self) -> float:
        """Value for ``Sound.set_volume`` of the UI sounds.

        Returns:
            ``UI_SOUND_LEVEL`` attenuated like music.
        """
        return UI_SOUND_LEVEL * self.music_volume()

    def step(self, direction: int) -> bool:
        """Apply one press of an in-app volume button.

        Does nothing unless ``volume_buttons`` is on. The result is clamped
        to ``0..cap``.

        Args:
            direction: ``+1`` for volume up, ``-1`` for volume down.

        Returns:
            True if the effective volume changed.
        """
        if not self._settings.volume_buttons:
            return False
        before = self.effective_percent()
        target = before + (VOLUME_STEP if direction > 0 else -VOLUME_STEP)
        self._level = max(0, min(self.cap, target))
        return self.effective_percent() != before


def music_output_volume(
    cap: float, fade_volume: float | None = None, ducked: bool = False
) -> float:
    """The one place that decides the music stream's volume.

    A UI sound is a second stream at ``UI_SOUND_LEVEL * cap`` and SDL sums the
    two, so while one plays the music is held at ``cap - beep level`` and the
    sum stays within the cap. A bedtime fade only ever lowers the result.

    Args:
        cap: The effective volume as a fraction (``VolumeControl.music_volume``).
        fade_volume: The bedtime fader's current volume, or None if no fade.
        ducked: Whether a UI sound is playing over the music.

    Returns:
        The value for ``pygame.mixer.music.set_volume``, never above ``cap``.
    """
    ceiling = cap * (1.0 - UI_SOUND_LEVEL) if ducked else cap
    if fade_volume is None:
        return ceiling
    return min(fade_volume, ceiling)


class Fader:
    """Linear fade from a start volume to silence.

    Args:
        start_volume: Volume at ``started_at`` (0.0 to 1.0).
        started_at: Monotonic time the fade starts.
        duration: Fade length in seconds.
    """

    def __init__(
        self, start_volume: float, started_at: float, duration: float = FADE_SECONDS
    ) -> None:
        self._start_volume = start_volume
        self._started_at = started_at
        self._duration = duration

    def volume_at(self, now: float) -> float:
        """Volume at monotonic time ``now``; never above the start volume.

        Args:
            now: Monotonic time.

        Returns:
            The faded volume, 0.0 once the fade is over.
        """
        progress = self.progress(now)
        return self._start_volume * (1.0 - progress)

    def progress(self, now: float) -> float:
        """Fraction of the fade completed at ``now`` (0.0 to 1.0).

        Args:
            now: Monotonic time.

        Returns:
            The clamped progress.
        """
        if self._duration <= 0:
            return 1.0
        return max(0.0, min(1.0, (now - self._started_at) / self._duration))

    def done(self, now: float) -> bool:
        """Whether the fade has reached silence.

        Args:
            now: Monotonic time.

        Returns:
            True once ``duration`` has elapsed.
        """
        return self.progress(now) >= 1.0


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def _system_now() -> datetime:
    return datetime.now().astimezone()


class TimeSource:
    """Best available local time for enforcing bedtime.

    Args:
        last_synced: Server time at the last sync that reached the server,
            persisted from a previous boot. ``None`` if never synced.
        wall: Returns the system clock as an aware datetime.
        mono: Monotonic seconds (``time.monotonic``).
        heartbeat: The wall-clock heartbeat persisted by an earlier run, used
            to spot a clock restored at boot. ``None`` if there is none.
        boot_id: This boot's id (``read_boot_id``); compared with the
            heartbeat's to tell a player restart from a reboot.
        tz: Time zone for results; the system's local zone if None.
    """

    def __init__(
        self,
        last_synced: datetime | None = None,
        *,
        heartbeat: ClockHeartbeat | None = None,
        boot_id: str | None = None,
        wall: Callable[[], datetime] = _system_now,
        mono: Callable[[], float] = time.monotonic,
        tz: tzinfo | None = None,
    ) -> None:
        self._last_synced = last_synced
        self._wall = wall
        self._mono = mono
        self._tz = tz
        # (server time, monotonic time when seen); replaced atomically so the
        # sync thread can update it while the UI thread reads it.
        self._anchor: tuple[datetime, float] | None = None
        self._ntp_synchronized = False
        self._untrusted_logged = False
        self._boot_id = boot_id
        self._restored_logged = False
        # Decided once, at startup: a restored clock stays wrong all boot.
        self._restored_suspect = self._looks_restored(heartbeat)

    def _looks_restored(self, heartbeat: ClockHeartbeat | None) -> bool:
        if heartbeat is None:
            return False
        if self._boot_id is not None and heartbeat.boot_id == self._boot_id:
            # Same boot: the clock has run on since the heartbeat.
            return not heartbeat.trusted
        wall = self._wall().astimezone(self._tz)
        return wall - heartbeat.wall < RESTORED_CLOCK_WINDOW

    def heartbeat(self) -> ClockHeartbeat:
        """The record to persist so the next start can spot a restored clock.

        Returns:
            The system clock now, this boot's id, and whether the clock is
            trusted (synced since boot, or not suspected of being restored).
        """
        return ClockHeartbeat(
            wall=self._wall(),
            boot_id=self._boot_id,
            trusted=(
                self._anchor is not None
                or self._ntp_synchronized
                or not self._restored_suspect
            ),
        )

    @property
    def synced_since_boot(self) -> bool:
        """Whether the server's time has been seen since this process started."""
        return self._anchor is not None

    def note_server_time(self, server_time: datetime) -> None:
        """Record the server's clock, as read from a sync response.

        Safe to call from the sync thread.

        Args:
            server_time: Aware datetime from the server's ``Date`` header.
        """
        self._anchor = (server_time, self._mono())

    def note_ntp_synchronized(self) -> None:
        """Record that NTP has synchronized the system clock.

        From then on the system clock is trusted even if it looks restored
        or reads earlier than the last sync. Safe to call from any thread.
        """
        if not self._ntp_synchronized:
            logger.info("System clock is NTP-synchronized; trusting it for bedtime")
        self._ntp_synchronized = True

    @property
    def ntp_synchronized(self) -> bool:
        """Whether NTP has synchronized the system clock since this start."""
        return self._ntp_synchronized

    def now(self) -> datetime | None:
        """Return the best available local time, or None if untrustworthy.

        Returns:
            An aware datetime in the configured zone, or ``None`` when the
            clock has neither been synced with the server nor by NTP since
            boot, and reads earlier than the last sync or looks restored.
        """
        anchor = self._anchor
        if anchor is not None:
            server_time, seen_at = anchor
            elapsed = max(0.0, self._mono() - seen_at)
            self._untrusted_logged = False
            self._restored_logged = False
            return (server_time + timedelta(seconds=elapsed)).astimezone(self._tz)
        wall = self._wall().astimezone(self._tz)
        if self._ntp_synchronized:
            self._untrusted_logged = False
            self._restored_logged = False
            return wall
        if self._last_synced is not None and wall < self._last_synced:
            if not self._untrusted_logged:
                logger.warning(
                    "Clock untrusted: system time %s is earlier than the last "
                    "sync (%s) and has not been synced since boot; bedtime is "
                    "not enforced until the server is reached",
                    wall.isoformat(),
                    self._last_synced.isoformat(),
                )
                self._untrusted_logged = True
            return None
        self._untrusted_logged = False
        if self._restored_suspect:
            if not self._restored_logged:
                logger.warning(
                    "Clock untrusted: system time %s looks restored from the "
                    "last shutdown and has not been synced since boot; "
                    "bedtime is not enforced until the server is reached",
                    wall.isoformat(),
                )
                self._restored_logged = True
            return None
        return wall


# ---------------------------------------------------------------------------
# Bedtime
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BedtimeStatus:
    """Whether bedtime is in force, and until when.

    Attributes:
        mode: The mode in force; ``BedtimeMode.OFF`` outside bedtime.
        wake_at: Local wall-clock time bedtime ends, if in bedtime.
    """

    mode: BedtimeMode = BedtimeMode.OFF
    wake_at: datetime | None = None

    @property
    def active(self) -> bool:
        """True during bedtime with a mode other than ``off``."""
        return self.mode is not BedtimeMode.OFF


NOT_BEDTIME = BedtimeStatus()


def bedtime_status(settings: ProfileSettings, now: datetime | None) -> BedtimeStatus:
    """Work out whether it is bedtime.

    A weekday's window runs from its ``bedtime`` to the next ``wake``, so an
    overnight window that started yesterday evening is still checked this
    morning. Times are compared as local wall-clock times.

    Args:
        settings: The profile settings.
        now: The best available local time, or None if the clock is
            untrusted (then bedtime is never enforced).

    Returns:
        The bedtime status; ``NOT_BEDTIME`` when not in force.
    """
    if now is None or settings.bedtime_mode is BedtimeMode.OFF:
        return NOT_BEDTIME
    local = now.replace(tzinfo=None)
    for days_back in (0, 1):
        day = local.date() - timedelta(days=days_back)
        window = settings.bedtime_schedule.get(Weekday.from_index(day.weekday()))
        if window is None:
            continue
        start = datetime.combine(day, window.bedtime)
        end_day = day if window.wake > window.bedtime else day + timedelta(days=1)
        end = datetime.combine(end_day, window.wake)
        if start <= local < end:
            return BedtimeStatus(mode=settings.bedtime_mode, wake_at=end)
    return NOT_BEDTIME
