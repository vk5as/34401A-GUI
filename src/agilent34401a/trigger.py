"""The trigger model: what starts a Reading, how long the Meter waits first, and how many Readings it takes.

These are the Meter's trigger settings, part of a `Setup`. They are plain values with no I/O, so a Preset can store
them as they are. `source` is the Trigger Source, `delay` the Trigger Delay in seconds (None for automatic),
`sample_count` the Sample Count and `trigger_count` the Trigger Count (None for infinite).
"""

import math
from dataclasses import dataclass
from enum import Enum

from agilent34401a.errors import BurstTooLargeError, InvalidSetupError

READING_MEMORY_SIZE = 512
"""How many Readings the Meter's Reading Memory holds."""
MAX_COUNT = 50000
"""The largest Sample Count or Trigger Count the Meter accepts."""
MAX_DELAY_S = 3600.0
"""The longest fixed Trigger Delay the Meter accepts, in seconds."""


class TriggerSource(Enum):
    """What starts a Reading. The value is the SCPI name, as sent with `TRIG:SOUR`."""

    IMMEDIATE = "IMM"
    BUS = "BUS"
    EXTERNAL = "EXT"

    @property
    def label(self) -> str:
        return {
            TriggerSource.IMMEDIATE: "Immediate",
            TriggerSource.BUS: "Bus",
            TriggerSource.EXTERNAL: "External",
        }[self]


@dataclass(frozen=True)
class TriggerSettings:
    """The Trigger Source, Trigger Delay, Sample Count and Trigger Count. Values the Meter would refuse cannot be built."""

    source: TriggerSource = TriggerSource.IMMEDIATE
    delay: float | None = None
    sample_count: int = 1
    trigger_count: int | None = 1

    def __post_init__(self) -> None:
        if self.delay is not None and not (math.isfinite(self.delay) and 0 <= self.delay <= MAX_DELAY_S):
            message = f"Trigger Delay is automatic or 0 to {MAX_DELAY_S:g} s, not {self.delay:g} s"
            raise InvalidSetupError(message)
        if not 1 <= self.sample_count <= MAX_COUNT:
            message = f"Sample Count is 1 to {MAX_COUNT}, not {self.sample_count}"
            raise InvalidSetupError(message)
        if self.trigger_count is not None and not 1 <= self.trigger_count <= MAX_COUNT:
            message = f"Trigger Count is 1 to {MAX_COUNT} or infinite, not {self.trigger_count}"
            raise InvalidSetupError(message)

    @property
    def readings(self) -> int | None:
        """How many Readings these settings take in all, or None when the Trigger Count is infinite."""
        return None if self.trigger_count is None else self.sample_count * self.trigger_count

    @property
    def is_single_immediate(self) -> bool:
        """One Reading per request, triggered at once: what Continuous and Single need (the delay does not matter)."""
        return self.source is TriggerSource.IMMEDIATE and self.sample_count == 1 and self.trigger_count == 1

    def check_fits_reading_memory(self) -> None:
        """Raise `BurstTooLargeError` unless a Burst with these settings fits in Reading Memory."""
        readings = self.readings
        if readings is None:
            message = f"A Burst needs a Trigger Count, because Reading Memory holds only {READING_MEMORY_SIZE} Readings"
            raise BurstTooLargeError(message)
        if readings > READING_MEMORY_SIZE:
            message = (
                f"A Sample Count of {self.sample_count} and a Trigger Count of {self.trigger_count} make {readings} Readings, "
                f"but Reading Memory holds {READING_MEMORY_SIZE}"
            )
            raise BurstTooLargeError(message)
