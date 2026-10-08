"""The Meter's Math Operations: what a Setup holds for them, what a Reading says of them, and the Meter's Statistics.

The Meter does one Math Operation at a time. `MathSettings` holds which one is active (`operation`, None for none) and
the settings of every Operation, active or not, so that choosing another Operation and coming back keeps what was
typed in. Which Functions an Operation applies to is the Setup's business, because it needs the Function.

No I/O and no Function knowledge lives here; `meter.py` builds on it.
"""

import math
from dataclasses import dataclass, replace
from enum import Enum

from agilent34401a.errors import InvalidSetupError

MIN_DBM_RESISTANCE = 50.0
MAX_DBM_RESISTANCE = 8000.0
DEFAULT_DBM_RESISTANCE = 600.0
MAX_DB_REFERENCE = 200.0
"""The dB reference is in dBm, and so is every dB or dBm result, to within this many either side of zero."""

# The Questionable Data register bits the Meter sets when a Reading fails a Limit Test (bit 11 low, bit 12 high).
LIMIT_LOW_BIT = 1 << 11
LIMIT_HIGH_BIT = 1 << 12


class MathOperation(Enum):
    """A calculation the Meter applies to Readings. The value is its SCPI name, as sent with `CALC:FUNC`."""

    NULL = "NULL"
    DB = "DB"
    DBM = "DBM"
    STATISTICS = "AVER"
    LIMIT_TEST = "LIM"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def csv_name(self) -> str:
        """How the CSV `math_mode` column names it."""
        return _CSV_NAMES[self]

    @property
    def changes_the_reading(self) -> bool:
        """Whether the Reading the Meter sends is the Operation's result rather than the measured value."""
        return self in (MathOperation.NULL, MathOperation.DB, MathOperation.DBM)

    @property
    def in_decibels(self) -> bool:
        """Whether the Reading the Meter sends is in dB or dBm rather than in the unit of the Function."""
        return self in (MathOperation.DB, MathOperation.DBM)


_LABELS = {
    MathOperation.NULL: "Null",
    MathOperation.DB: "dB",
    MathOperation.DBM: "dBm",
    MathOperation.STATISTICS: "Statistics",
    MathOperation.LIMIT_TEST: "Limit Test",
}
_CSV_NAMES = {
    MathOperation.NULL: "NULL",
    MathOperation.DB: "DB",
    MathOperation.DBM: "DBM",
    MathOperation.STATISTICS: "STATS",
    MathOperation.LIMIT_TEST: "LIMIT",
}


class LimitResult(Enum):
    """How a Reading did in a Limit Test. The value is the text the readout and the CSV show."""

    PASS = "PASS"  # nosec B105  # noqa: S105 - the word, not a password
    LOW = "LO"
    HIGH = "HI"

    @property
    def label(self) -> str:
        return self.value

    @property
    def failed(self) -> bool:
        return self is not LimitResult.PASS


def limit_result_from_status(questionable_events: int) -> LimitResult:
    """Read a Limit Test result off the Meter's Questionable Data event register, as the Meter sets it."""
    if questionable_events & LIMIT_HIGH_BIT:
        return LimitResult.HIGH
    if questionable_events & LIMIT_LOW_BIT:
        return LimitResult.LOW
    return LimitResult.PASS


def offset_that_nulls(value: float, *, nulled_by: float | None) -> float:
    """Return the Null offset that makes a Reading of `value` read zero.

    `nulled_by` is the offset the Reading was already nulled by, or None if it was not nulled: a nulled Reading is the
    measured value minus that offset, so the offset to capture is the Reading plus it.
    """
    return value + (nulled_by or 0.0)


@dataclass(frozen=True)
class MathSettings:
    """The Math part of a Setup. Invalid values cannot be built.

    `operation` is the one Math Operation in effect, or None. `null_offset` is in the unit of the Function and is
    subtracted from each Reading by Null. `db_reference` is the dBm that dB is relative to. `dbm_reference_resistance`
    is the load in ohms that dBm assumes, 50 to 8000. `limit_lower` and `limit_upper` bound a Limit Test, in the unit
    of the Function, with the lower never above the upper.
    """

    operation: MathOperation | None = None
    null_offset: float = 0.0
    db_reference: float = 0.0
    dbm_reference_resistance: float = DEFAULT_DBM_RESISTANCE
    limit_lower: float = 0.0
    limit_upper: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.null_offset):
            message = f"A Null offset must be a number, not {self.null_offset}"
            raise InvalidSetupError(message)
        if not abs(self.db_reference) <= MAX_DB_REFERENCE:  # also false for NaN
            message = f"The dB reference must be within {MAX_DB_REFERENCE:g} dBm of zero, not {self.db_reference}"
            raise InvalidSetupError(message)
        if not MIN_DBM_RESISTANCE <= self.dbm_reference_resistance <= MAX_DBM_RESISTANCE:
            message = (
                f"The dBm Reference Resistance is {MIN_DBM_RESISTANCE:g} Ω to {MAX_DBM_RESISTANCE:g} Ω, "
                f"not {self.dbm_reference_resistance}"
            )
            raise InvalidSetupError(message)
        if not (math.isfinite(self.limit_lower) and math.isfinite(self.limit_upper)):
            message = "The Limit Test bounds must be numbers"
            raise InvalidSetupError(message)
        if self.limit_lower > self.limit_upper:
            message = f"The Limit Test lower bound {self.limit_lower:g} is above the upper bound {self.limit_upper:g}"
            raise InvalidSetupError(message)

    def with_operation(self, operation: MathOperation | None) -> "MathSettings":
        """Make `operation` the one in effect (or none), which turns any other off."""
        return replace(self, operation=operation)


@dataclass(frozen=True)
class MeterStatistics:
    """The Meter's own running minimum, maximum and average of the Readings since Statistics was turned on.

    Not to be confused with the History statistics, which the application works out from the Readings it kept.
    """

    minimum: float
    maximum: float
    average: float
    count: int
