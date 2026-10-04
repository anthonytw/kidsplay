"""Audio metadata and artwork extraction.

Uses mutagen for tag reading. All functions are synchronous — they perform
only local file I/O and CPU work, suitable for running in a thread pool
executor if needed in an async context.

Supported formats: MP3 (ID3), MP4/AAC (iTunes tags), FLAC, OGG Vorbis,
WAV (ID3). Any format mutagen can open will work; unsupported formats fall
back to filename/directory heuristics gracefully.

Loudness normalization (EBU R128) shells out to ffmpeg's ``loudnorm`` filter
in two passes: the first measures the source, the second applies a gain
(linear mode) or, only when a plain gain would push the true peak over the
ceiling, a dynamic gain with a true-peak limiter. The output is always MP3,
and its true peak is checked after encoding. See ``docs/LOUDNESS.md`` for the
design and the ingest-time cost.
"""

import contextlib
import json
import logging
import math
import re
import shutil
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import mutagen
import mutagen.id3

from kidsplay_models.media import MediaType
from kidsplay_server.processing.resources import run_limited

logger = logging.getLogger(__name__)


def extract_metadata(file_path: Path) -> dict:
    """Extract title, artist, album, duration, and track number from an audio file.

    Uses mutagen's unified easy-tag interface so the same keys work across
    MP3, MP4, FLAC, and OGG Vorbis. Falls back to filesystem structure when
    tags are absent:
      - title → filename stem
      - album → parent directory name

    Args:
        file_path: Path to the audio file to inspect.

    Returns:
        Dict with keys ``title``, ``artist``, ``album``,
        ``duration_seconds``, and ``track_number``. Any value may be
        ``None`` if not available.
    """
    result: dict = {
        "title": None,
        "artist": None,
        "album": None,
        "duration_seconds": None,
        "track_number": None,
    }

    audio = None
    try:
        audio = mutagen.File(file_path, easy=True)
    except Exception as exc:
        logger.warning("mutagen could not open %s: %s", file_path, exc)

    if audio is not None:

        def _tag(key: str) -> str | None:
            val = audio.get(key)
            return str(val[0]) if val else None

        result["title"] = _tag("title")
        result["artist"] = _tag("artist")
        result["album"] = _tag("album")

        track_raw = _tag("tracknumber")
        if track_raw:
            with contextlib.suppress(ValueError, AttributeError):
                result["track_number"] = int(track_raw.split("/")[0])

        try:
            length = getattr(getattr(audio, "info", None), "length", None)
            if length is not None:
                result["duration_seconds"] = int(math.ceil(length))
        except (TypeError, ValueError):
            pass

    # Filesystem fallbacks
    if not result["title"]:
        result["title"] = file_path.stem
    if not result["album"]:
        result["album"] = file_path.parent.name

    return result


def extract_artwork(file_path: Path) -> bytes | None:
    """Extract embedded cover artwork from an audio file.

    Checks (in order):
      1. FLAC picture blocks (``audio.pictures``).
      2. ID3 APIC frames (MP3, WAV, AIFF).
      3. MP4 ``covr`` tag (AAC, ALAC, M4A).

    Args:
        file_path: Path to the audio file.

    Returns:
        Raw image bytes (any format — typically JPEG or PNG), or ``None``
        if no artwork is embedded or the file cannot be opened.
    """
    audio = None
    try:
        audio = mutagen.File(file_path)
    except Exception as exc:
        logger.warning("mutagen could not open %s for artwork: %s", file_path, exc)
        return None

    if audio is None:
        return None

    # FLAC: pictures is a list of mutagen.flac.Picture objects
    pictures = getattr(audio, "pictures", None)
    if pictures:
        return pictures[0].data

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None

    # ID3 (MP3 etc.): getall() is specific to ID3 tag objects
    if hasattr(tags, "getall"):
        apic_frames = tags.getall("APIC")
        if apic_frames:
            return apic_frames[0].data

    # MP4 (AAC/M4A): covr is a list of MP4Cover (bytes subclass)
    try:
        covr = tags.get("covr")
    except Exception:
        covr = None
    if covr:
        return bytes(covr[0])

    return None


# ---------------------------------------------------------------------------
# Loudness normalization (EBU R128 via ffmpeg loudnorm)
# ---------------------------------------------------------------------------

DEFAULT_TARGET_LUFS = -16.0
"""Default integrated-loudness target (LUFS)."""

DEFAULT_TRUE_PEAK_DBTP = -1.5
"""Default true-peak ceiling (dBTP)."""

# Limits of the ffmpeg loudnorm filter's I and TP options.
MIN_TARGET_LUFS = -70.0
MAX_TARGET_LUFS = -5.0
MIN_TRUE_PEAK_DBTP = -9.0
MAX_TRUE_PEAK_DBTP = 0.0

# loudnorm only needs a loudness-range target in dynamic mode. Using the
# source's own range (never below ffmpeg's default of 7 LU) means the
# fallback limits peaks without also squashing the dynamics. 50 is the
# filter's maximum.
_MIN_TARGET_LRA = 7.0
_MAX_TARGET_LRA = 50.0

# How often to re-encode with a lower level when MP3 encoding pushes the true
# peak over the ceiling. Each retry lowers the output by the overshoot.
_MAX_PEAK_ATTEMPTS = 4

# Extra gain reduction on top of the overshoot, so the retry lands just under
# the ceiling instead of on it. It doubles with each retry: the MP3 encoder's
# overshoot is not a smooth function of level, so a small gain change can move
# the encoded peak by less than the change itself.
_PEAK_RETRY_MARGIN_DB = 0.1

# Safety margin for "capped" mode: the measured values are passed to loudnorm
# rounded to 0.01 dB, and loudnorm's linear mode needs the gain to fit with
# room to spare.
_CAP_MARGIN_DB = 0.1

# Rate at which true peak is measured: loudnorm's internal rate, i.e. at
# least 4x oversampling for every rate up to 48 kHz, as BS.1770 asks.
_TRUE_PEAK_RATE = 192000

# Sample rates the MP3 format supports. Other rates are resampled to 48 kHz.
_MP3_SAMPLE_RATES: frozenset[int] = frozenset(
    {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000}
)

NORMALIZED_SUFFIX = ".mp3"
"""File suffix of every normalized output."""

NORMALIZED_MIME = "audio/mpeg"
"""MIME type of every normalized output."""


class LoudnessError(RuntimeError):
    """ffmpeg is missing, failed, or produced output that misses the target."""


class UnmeasurableLoudnessError(LoudnessError):
    """The audio is silent or too quiet for EBU R128 to measure."""


@dataclass(frozen=True)
class LoudnessTarget:
    """Loudness target for one normalization.

    Attributes:
        integrated_lufs: Integrated loudness to reach (LUFS).
        true_peak_dbtp: Ceiling the true peak must stay at or below (dBTP).
    """

    integrated_lufs: float = DEFAULT_TARGET_LUFS
    true_peak_dbtp: float = DEFAULT_TRUE_PEAK_DBTP

    def matches(self, lufs: float | None, true_peak: float | None) -> bool:
        """Return True if a stored target equals this one.

        Args:
            lufs: Stored integrated-loudness target, or ``None``.
            true_peak: Stored true-peak ceiling, or ``None``.

        Returns:
            True when both values are present and equal to this target
            (to within 0.01 dB, since they round-trip through SQLite REALs).
        """
        if lufs is None or true_peak is None:
            return False
        return math.isclose(lufs, self.integrated_lufs, abs_tol=0.01) and (
            math.isclose(true_peak, self.true_peak_dbtp, abs_tol=0.01)
        )


@dataclass(frozen=True)
class LoudnessConfig:
    """Server-wide loudness normalization settings.

    Attributes:
        enabled: Normalize audio at ingest. When False, ingest stores audio
            unchanged and items stay "not normalized".
        target_lufs: Integrated-loudness target for every audio type
            without its own target.
        true_peak_dbtp: True-peak ceiling for all audio.
        music_target_lufs: Target for music, or ``None`` for ``target_lufs``.
        audiobook_target_lufs: Target for audiobooks, or ``None`` for
            ``target_lufs``.
        allow_limiting: When a plain gain to the target would push the true
            peak over the ceiling, loudnorm may fall back to dynamic mode
            (gain plus a true-peak limiter), which reshapes loud peaks. Set to
            ``False`` to never limit: the gain is held below the target
            instead ("capped"), so quiet, dynamic material ends up a little
            quieter than the target but is never altered.
    """

    enabled: bool = True
    target_lufs: float = DEFAULT_TARGET_LUFS
    true_peak_dbtp: float = DEFAULT_TRUE_PEAK_DBTP
    music_target_lufs: float | None = None
    audiobook_target_lufs: float | None = None
    allow_limiting: bool = True

    def __post_init__(self) -> None:
        """Validate the targets against the ranges ffmpeg accepts.

        Raises:
            ValueError: If a target or the ceiling is out of range.
        """
        for name in ("target_lufs", "music_target_lufs", "audiobook_target_lufs"):
            value = getattr(self, name)
            if value is not None and not (MIN_TARGET_LUFS <= value <= MAX_TARGET_LUFS):
                raise ValueError(
                    f"{name} must be between {MIN_TARGET_LUFS} and "
                    f"{MAX_TARGET_LUFS} LUFS, not {value}"
                )
        if not MIN_TRUE_PEAK_DBTP <= self.true_peak_dbtp <= MAX_TRUE_PEAK_DBTP:
            raise ValueError(
                f"true_peak_dbtp must be between {MIN_TRUE_PEAK_DBTP} and "
                f"{MAX_TRUE_PEAK_DBTP} dBTP, not {self.true_peak_dbtp}"
            )

    def target_for(self, media_type: MediaType) -> LoudnessTarget:
        """Return the loudness target for a media type.

        Args:
            media_type: MUSIC or AUDIOBOOK. Photos get the overall target,
                though they are never normalized.

        Returns:
            The type's own target if set, else the overall one.
        """
        per_type = {
            MediaType.MUSIC: self.music_target_lufs,
            MediaType.AUDIOBOOK: self.audiobook_target_lufs,
        }.get(media_type)
        lufs = per_type if per_type is not None else self.target_lufs
        return LoudnessTarget(integrated_lufs=lufs, true_peak_dbtp=self.true_peak_dbtp)


@dataclass(frozen=True)
class LoudnessMeasurement:
    """EBU R128 statistics of one audio file, as ffmpeg loudnorm reports them.

    Attributes:
        integrated_lufs: Integrated loudness (LUFS).
        true_peak_dbtp: True peak (dBTP).
        lra: Loudness range (LU).
        threshold: Gating threshold (LUFS).
        target_offset: loudnorm's offset for the second pass (LU).
    """

    integrated_lufs: float
    true_peak_dbtp: float
    lra: float
    threshold: float
    target_offset: float


@dataclass(frozen=True)
class NormalizationResult:
    """Outcome of ``normalize_loudness``.

    Attributes:
        source: Measurement of the input file.
        output_lufs: Integrated loudness of the output, as loudnorm's second
            pass reports it.
        output_true_peak_dbtp: True peak of the encoded output file.
        mode: ``"linear"`` (a plain gain), ``"dynamic"`` (gain plus
            true-peak limiting) or ``"capped"`` (a plain gain held below the
            target so that no limiting is needed).
    """

    source: LoudnessMeasurement
    output_lufs: float
    output_true_peak_dbtp: float
    mode: str

    @property
    def gain_db(self) -> float:
        """Loudness change applied: output minus source loudness (dB)."""
        return round(self.output_lufs - self.source.integrated_lufs, 2)


def ffmpeg_available() -> bool:
    """Return True if an ``ffmpeg`` executable is on ``PATH``."""
    return shutil.which("ffmpeg") is not None


def _run_ffmpeg(args: list[str], cancel: threading.Event | None = None) -> str:
    """Run ffmpeg and return its stderr, where loudnorm prints its report.

    Runs under the server's resource limits (``processing.resources``).

    Args:
        args: Arguments after ``ffmpeg -hide_banner -nostdin -nostats``.
        cancel: Set from another thread to kill ffmpeg (see ``run_limited``).

    Returns:
        ffmpeg's stderr output.

    Raises:
        LoudnessError: If ffmpeg is missing or exits non-zero.
        JobCancelledError: If ``cancel`` was set.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", *args]
    try:
        proc = run_limited(cmd, cancel=cancel)
    except FileNotFoundError as exc:
        raise LoudnessError("ffmpeg is not installed or not on PATH") from exc
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise LoudnessError(f"ffmpeg exited with {proc.returncode}: {tail}")
    return proc.stderr


def _parse_loudnorm_report(stderr: str) -> dict[str, str]:
    """Extract loudnorm's JSON report (the last ``{...}`` block) from stderr.

    Args:
        stderr: ffmpeg stderr from a run with ``print_format=json``.

    Returns:
        The report's key/value pairs; loudnorm prints every value as a string.

    Raises:
        LoudnessError: If no parsable report is found.
    """
    start, end = stderr.rfind("{"), stderr.rfind("}")
    if start == -1 or end < start:
        raise LoudnessError("ffmpeg loudnorm printed no loudness report")
    try:
        report = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LoudnessError(f"unreadable loudnorm report: {exc}") from exc
    return {str(k): str(v) for k, v in report.items()}


def _loudnorm_filter(target: LoudnessTarget, lra: float, **extra: str) -> str:
    """Build a loudnorm filter string.

    Args:
        target: Loudness target.
        lra: Loudness-range target (LU).
        **extra: Further loudnorm options, already formatted.

    Returns:
        A filter for ``-af``.
    """
    opts = {
        "I": f"{target.integrated_lufs:.2f}",
        "TP": f"{target.true_peak_dbtp:.2f}",
        "LRA": f"{lra:.2f}",
        **extra,
        "print_format": "json",
    }
    return "loudnorm=" + ":".join(f"{k}={v}" for k, v in opts.items())


def measure_loudness(
    path: Path,
    target: LoudnessTarget | None = None,
    cancel: threading.Event | None = None,
) -> LoudnessMeasurement:
    """Measure a file's EBU R128 loudness (the first loudnorm pass).

    Decodes the whole file once; nothing is written.

    Args:
        path: Audio file to measure.
        target: Target for the second pass, which loudnorm needs to report
            ``target_offset``. Defaults to the default target.
        cancel: Set from another thread to kill ffmpeg.

    Returns:
        The measurement.

    Raises:
        UnmeasurableLoudnessError: If the audio is silent or below the
            -70 LUFS absolute gate.
        LoudnessError: If ffmpeg is missing or fails.
    """
    target = target or LoudnessTarget()
    stderr = _run_ffmpeg(
        [
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            _loudnorm_filter(target, _MIN_TARGET_LRA),
            "-f",
            "null",
            "-",
        ],
        cancel,
    )
    report = _parse_loudnorm_report(stderr)
    try:
        measurement = LoudnessMeasurement(
            integrated_lufs=float(report["input_i"]),
            true_peak_dbtp=float(report["input_tp"]),
            lra=float(report["input_lra"]),
            threshold=float(report["input_thresh"]),
            target_offset=float(report["target_offset"]),
        )
    except (KeyError, ValueError) as exc:
        raise LoudnessError(f"incomplete loudnorm report: {report}") from exc
    values = (
        measurement.integrated_lufs,
        measurement.true_peak_dbtp,
        measurement.target_offset,
    )
    if not all(math.isfinite(v) for v in values) or (
        measurement.integrated_lufs < MIN_TARGET_LUFS
    ):
        raise UnmeasurableLoudnessError(
            f"{path.name} is silent or too quiet to measure "
            f"({report.get('input_i')} LUFS)"
        )
    return measurement


def measure_true_peak(path: Path, cancel: threading.Event | None = None) -> float:
    """Measure a file's true peak: its sample peak after 4x oversampling.

    Cheaper than a full ``measure_loudness`` pass (no loudness gating or
    loudnorm processing) and agrees with loudnorm's ``input_tp``.

    Args:
        path: Audio file to measure.
        cancel: Set from another thread to kill ffmpeg.

    Returns:
        True peak in dBTP; ``-inf`` for digital silence.

    Raises:
        LoudnessError: If ffmpeg is missing or fails, or prints no peak.
    """
    stderr = _run_ffmpeg(
        [
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            # Oversample in floating point: in an integer format the
            # inter-sample peaks above 0 dBFS would be clipped away.
            f"aformat=sample_fmts=dbl,aresample={_TRUE_PEAK_RATE},"
            "aformat=sample_fmts=dbl,"
            "astats=measure_overall=Peak_level:measure_perchannel=none",
            "-f",
            "null",
            "-",
        ],
        cancel,
    )
    match = re.search(r"Peak level dB:\s*(\S+)", stderr)
    if match is None:
        raise LoudnessError("ffmpeg astats printed no peak level")
    try:
        return float(match.group(1))
    except ValueError as exc:
        raise LoudnessError(f"unreadable peak level {match.group(1)!r}") from exc


def _probe_sample_rate(path: Path, cancel: threading.Event | None = None) -> int | None:
    """Read the first audio stream's sample rate with ffprobe.

    For formats mutagen reports no rate for (Opus).

    Args:
        path: Audio file.
        cancel: Set from another thread to kill ffprobe.

    Returns:
        The rate in Hz, or ``None`` if ffprobe is missing or reports none.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = run_limited(cmd, cancel=cancel)
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip().splitlines()
    return int(text[0]) if text and text[0].isdigit() else None


def _output_sample_rate(path: Path, cancel: threading.Event | None = None) -> int:
    """Pick the MP3 sample rate for a source: its own rate if MP3 allows it.

    loudnorm works at 192 kHz internally, so the rate has to be set
    explicitly or the output would be 192 kHz. The rate comes from mutagen,
    or from ffprobe where mutagen reports none (Opus), so that a 48 kHz
    Opus file is not needlessly resampled to 44.1 kHz.

    Args:
        path: Source audio file.
        cancel: Set from another thread to kill ffprobe.

    Returns:
        Sample rate in Hz.
    """
    rate: int | None = None
    try:
        audio = mutagen.File(path)
        rate = getattr(getattr(audio, "info", None), "sample_rate", None)
    except Exception as exc:
        logger.debug("mutagen could not read the sample rate of %s: %s", path, exc)
    if not isinstance(rate, int):
        rate = _probe_sample_rate(path, cancel)
    if isinstance(rate, int) and rate in _MP3_SAMPLE_RATES:
        return rate
    return 48000 if isinstance(rate, int) and rate > 44100 else 44100


def _cap_target(
    measured: LoudnessMeasurement, target: LoudnessTarget
) -> tuple[LoudnessTarget, bool]:
    """Lower the loudness target until a plain gain meets the ceiling.

    Args:
        measured: First-pass measurement of the source.
        target: The wanted target.

    Returns:
        ``(effective target, capped)``: ``target`` itself when the plain gain
        to it already keeps the true peak under the ceiling, else a target
        just low enough for that (``capped`` True).
    """
    headroom = target.true_peak_dbtp - measured.true_peak_dbtp - _CAP_MARGIN_DB
    if target.integrated_lufs - measured.integrated_lufs <= headroom:
        return target, False
    lufs = max(measured.integrated_lufs + headroom, MIN_TARGET_LUFS)
    return replace(target, integrated_lufs=lufs), True


def _encode(
    source: Path,
    dest: Path,
    target: LoudnessTarget,
    measured: LoudnessMeasurement,
    sample_rate: int,
    cancel: threading.Event | None = None,
    trim_db: float = 0.0,
) -> tuple[str, float]:
    """Run the second loudnorm pass and encode to MP3.

    Args:
        source: Input audio file.
        dest: Output MP3 path (overwritten).
        target: Loudness target.
        measured: First-pass measurement of ``source``.
        sample_rate: Output sample rate.
        cancel: Set from another thread to kill ffmpeg.
        trim_db: Plain gain reduction (dB, >= 0) applied after loudnorm, from
            the same decoded source, to bring the encoded peak under the
            ceiling.

    Returns:
        The normalization type loudnorm used (``linear`` or ``dynamic``) and
        the integrated loudness of the output (loudnorm's own measurement
        less ``trim_db``).

    Raises:
        LoudnessError: If ffmpeg fails or its report is incomplete.
    """
    lra = min(max(measured.lra, _MIN_TARGET_LRA), _MAX_TARGET_LRA)
    af = _loudnorm_filter(
        target,
        lra,
        measured_I=f"{measured.integrated_lufs:.2f}",
        measured_TP=f"{measured.true_peak_dbtp:.2f}",
        # loudnorm treats a measured LRA of exactly 0 as "not given" and then
        # refuses linear mode. Steady tones really do measure 0.00 LU, so
        # pass the smallest value it reports instead.
        measured_LRA=f"{max(measured.lra, 0.01):.2f}",
        measured_thresh=f"{measured.threshold:.2f}",
        offset=f"{measured.target_offset:.2f}",
        linear="true",
    )
    if trim_db > 0:
        af += f",volume=-{trim_db:.3f}dB"
    stderr = _run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-af",
            f"{af},aresample={sample_rate}",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            "-f",
            "mp3",
            str(dest),
        ],
        cancel,
    )
    report = _parse_loudnorm_report(stderr)
    try:
        output_lufs = float(report["output_i"])
    except (KeyError, ValueError) as exc:
        raise LoudnessError(f"incomplete loudnorm report: {report}") from exc
    return report.get("normalization_type", "unknown"), output_lufs - trim_db


def normalize_loudness(
    source: Path,
    dest: Path,
    target: LoudnessTarget,
    *,
    allow_limiting: bool = True,
    cancel: threading.Event | None = None,
) -> NormalizationResult:
    """Normalize an audio file to a loudness target and write it as MP3.

    Measures the source, encodes it with loudnorm's second pass, then
    measures the output's true peak. If the peak is over the ceiling (loudnorm's
    limiter is approximate and MP3 encoding overshoots again), it re-encodes
    from the same source with a plain gain reduction of the overshoot plus a
    margin after loudnorm (up to ``_MAX_PEAK_ATTEMPTS`` times), so the stored
    file never exceeds the ceiling. The output loudness then ends a fraction
    of a dB under the target; the result records what was measured. When a
    plain gain to the target does not fit under the ceiling, loudnorm limits
    the peaks (``dynamic`` mode) unless ``allow_limiting`` is False, in which
    case the target is lowered until a plain gain fits (``capped`` mode).

    Synchronous and CPU-bound (three decodes of the file, more on a retry
    and, in dynamic mode, usually an extra encode); call it from a worker
    thread in async code.

    Args:
        source: Input audio file (any format ffmpeg decodes).
        dest: Output path for the MP3.
        target: Loudness target.
        allow_limiting: Whether loudnorm may fall back to limiting.
        cancel: Set from another thread to abandon the work: the running
            ffmpeg is killed and ``JobCancelledError`` raised.

    Returns:
        Source and output measurements and the mode used.

    Raises:
        UnmeasurableLoudnessError: If the source is silent or too quiet.
        LoudnessError: If ffmpeg is missing or fails, or the output still
            exceeds the ceiling after every attempt.
        JobCancelledError: If ``cancel`` was set.
    """
    measured = measure_loudness(source, target, cancel)
    sample_rate = _output_sample_rate(source, cancel)
    trim_db = 0.0
    peaks: list[float] = []
    for attempt in range(1, _MAX_PEAK_ATTEMPTS + 1):
        capped = False
        wanted = target
        if not allow_limiting:
            wanted, capped = _cap_target(measured, target)
        mode, output_lufs = _encode(
            source, dest, wanted, measured, sample_rate, cancel, trim_db
        )
        if capped and mode == "linear":
            mode = "capped"
        peak = measure_true_peak(dest, cancel)
        peaks.append(peak)
        overshoot = peak - target.true_peak_dbtp
        if overshoot <= 0:
            if attempt > 1:
                logger.info(
                    "%s: true peak %s dBTP over the %.2f ceiling; fixed by a "
                    "%.2f dB gain reduction, ending at %.2f LUFS",
                    source.name,
                    _format_peaks(peaks[:-1]),
                    target.true_peak_dbtp,
                    trim_db,
                    output_lufs,
                )
            logger.debug(
                "Normalized %s (%s): %.2f → %.2f LUFS, TP %.2f dBTP",
                source.name,
                mode,
                measured.integrated_lufs,
                output_lufs,
                peak,
            )
            return NormalizationResult(
                source=measured,
                output_lufs=output_lufs,
                output_true_peak_dbtp=peak,
                mode=mode,
            )
        logger.debug(
            "%s: true peak %.2f dBTP over the %.2f ceiling (attempt %d)",
            source.name,
            peak,
            target.true_peak_dbtp,
            attempt,
        )
        # Lower the whole encode by the overshoot (plus a margin) rather than
        # loudnorm's ceiling: its limiter is only approximate and the MP3
        # encoder overshoots again after it, so retargeting does not converge,
        # while a gain change moves the encoded peak by about the same amount.
        trim_db += overshoot + _PEAK_RETRY_MARGIN_DB * 2 ** (attempt - 1)
    logger.info(
        "%s: true peak above the %.2f dBTP ceiling after every attempt: %s dBTP",
        source.name,
        target.true_peak_dbtp,
        _format_peaks(peaks),
    )
    raise LoudnessError(
        f"{source.name}: true peak still above {target.true_peak_dbtp} dBTP "
        f"after {_MAX_PEAK_ATTEMPTS} attempts (peaks {_format_peaks(peaks)} dBTP)"
    )


def _format_peaks(peaks: list[float]) -> str:
    """Format per-attempt true peaks for a log line.

    Args:
        peaks: Peaks in dBTP, one per attempt.

    Returns:
        E.g. ``"-1.17, -0.91"``.
    """
    return ", ".join(f"{p:.2f}" for p in peaks)
