"""Per-profile settings delivered to devices.

``ProfileSettings`` is stored per profile on the server and sent to every
device linked to that profile inside the sync manifest. The device persists
the last copy it received, so the settings apply offline and on boot before
any sync.

Compatibility rules (both directions must keep working):

- Every field has a default, so a manifest from an older server that lacks
  a field still validates.
- Unknown fields are ignored (``extra="ignore"``), so an older device keeps
  working when a newer server adds a field.
- ``version`` is bumped only for a change an older device must not
  misread (a field whose meaning changes). Adding a field does not bump it.

Later per-profile settings belong here too, rather than in a mechanism of
their own.
"""

from datetime import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILE_SETTINGS_VERSION = 1
"""Current ``ProfileSettings.version``."""


class BedtimeMode(StrEnum):
    """What the device does during bedtime."""

    OFF = "off"
    """No bedtime restrictions, even if a schedule is set."""
    AUDIOBOOKS_ONLY = "audiobooks_only"
    """Only audiobooks can be browsed and played."""
    SLEEP_SCREEN = "sleep_screen"
    """A dim sleep screen with no playback."""


class Weekday(StrEnum):
    """Day of the week, keyed the same way on server and device."""

    MONDAY = "mon"
    TUESDAY = "tue"
    WEDNESDAY = "wed"
    THURSDAY = "thu"
    FRIDAY = "fri"
    SATURDAY = "sat"
    SUNDAY = "sun"

    @classmethod
    def from_index(cls, index: int) -> "Weekday":
        """Return the weekday for a ``date.weekday()`` index.

        Args:
            index: 0 for Monday through 6 for Sunday.

        Returns:
            The matching ``Weekday``.
        """
        return list(cls)[index % 7]


class BedtimeWindow(BaseModel):
    """Bedtime for one evening, in the device's local time.

    The window starts at ``bedtime`` on its weekday and ends at the next
    ``wake``: the following morning when ``wake`` is not after ``bedtime``
    (the usual overnight case), or the same day when it is (a nap).
    """

    model_config = ConfigDict(extra="ignore")

    bedtime: time = Field(description="Local time bedtime starts, e.g. 20:00.")
    wake: time = Field(description="Local time bedtime ends, e.g. 07:00.")

    @model_validator(mode="after")
    def _distinct_times(self) -> "BedtimeWindow":
        # Bedtime is a wall-clock time in the device's own zone. An offset
        # would make the device's time comparisons raise, so it is rejected.
        if self.bedtime.tzinfo is not None or self.wake.tzinfo is not None:
            raise ValueError(
                "bedtime and wake are local times: give HH:MM without a timezone offset"
            )
        if self.bedtime == self.wake:
            raise ValueError("bedtime and wake must differ")
        return self


class ProfileSettings(BaseModel):
    """Settings for one child's profile, applied on each of their devices.

    Attributes:
        version: Settings format version (see the module docstring).
        max_volume: Loudest the player may play, in percent. Applied as
            digital attenuation before the OS volume and any hardware volume
            dial, so it caps the loudest possible output.
        volume_buttons: Whether the in-app volume up/down buttons are on, for
            hardware without a physical volume control. ``None`` (the default)
            leaves it to the device: its input profile turns them on for
            hardware with no volume dial (a keyboard) and off for the rest
            (the GPi Case 2). ``True`` and ``False`` are a parent's explicit
            choice and win over the input profile. ``None`` is left out of the
            serialised form, so a device that predates the ``None`` value
            (its field is a plain ``bool``) never sees a ``null``.
        ui_sounds: Whether button presses play a short UI sound. On by
            default; off means silence (and the music is not ducked). A manifest
            from an older server lacks it, which means on.
        bedtime_mode: What happens during bedtime.
        bedtime_schedule: Bedtime per weekday. A weekday that is absent has
            no bedtime that evening.
        language: Language of the child's device screens (``"en"``, ``"es"``).
            None means "never chosen": a device then shows Spanish, which is
            what every device showed before this setting existed. Not
            validated here so an older device still accepts a newer server's
            language; the server checks it against ``SUPPORTED_LANGUAGES``
            when it is set.
        theme: Id of the theme the device shows (see ``themes.py``), or None
            to let the device use its own choice (the default theme until
            someone picks another on the device). Not validated against the
            known themes here: a device that does not know the id falls back
            to the default theme, and the server checks it when it is set.
    """

    model_config = ConfigDict(extra="ignore")

    version: int = PROFILE_SETTINGS_VERSION
    max_volume: int = Field(default=100, ge=0, le=100)
    volume_buttons: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    ui_sounds: bool = True
    bedtime_mode: BedtimeMode = BedtimeMode.OFF
    bedtime_schedule: dict[Weekday, BedtimeWindow] = Field(default_factory=dict)
    language: str | None = Field(default=None, max_length=35)
    theme: str | None = Field(default=None, max_length=40)
