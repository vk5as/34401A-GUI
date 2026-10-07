"""The Meter model: Functions, Readings, and turning replies into Readings and Readings into text."""

import math
from dataclasses import dataclass
from enum import Enum

from agilent34401a.errors import MalformedReplyError

# Engineering prefixes by power of ten, smallest to largest.
_PREFIXES = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}
_MIN_PREFIX_EXPONENT = min(_PREFIXES)
_MAX_PREFIX_EXPONENT = max(_PREFIXES)
_PREFIX_STEP = 3

# The Meter reports Overload as +/-9.9E+37; anything at or beyond this magnitude is an Overload.
_OVERLOAD_MAGNITUDE = 9.9e37


class Function(Enum):
    """A quantity the Meter measures. Only DC voltage exists so far; the rest arrive with their issues."""

    DC_VOLTAGE = "VOLT:DC"  # the SCPI mnemonic, which keeps every Function distinct (units repeat)

    @property
    def unit(self) -> str:
        return _UNITS[self]


_UNITS = {Function.DC_VOLTAGE: "V"}


@dataclass(frozen=True)
class Reading:
    """One measured value returned by the Meter."""

    value: float
    function: Function
    raw: str

    @property
    def is_overload(self) -> bool:
        return abs(self.value) >= _OVERLOAD_MAGNITUDE


def parse_reading(raw: str, function: Function) -> Reading:
    """Turn the Meter's reply into a Reading, keeping the Raw Reading untouched."""
    try:
        # float() also accepts Python-only spellings such as "1_0" that a Meter never sends.
        value = math.nan if "_" in raw else float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        message = f"Meter replied {raw!r}, which is not a Reading"
        raise MalformedReplyError(message)
    return Reading(value=value, function=function, raw=raw)


def format_reading(reading: Reading, resolution_digits: float = 6.5) -> str:
    """Show a Reading with an engineering prefix and its unit, or OVLD for an Overload.

    `resolution_digits` is the Meter's Resolution (4.5, 5.5 or 6.5); the half digit is the leading 0 or 1,
    so a 6½-digit Reading shows seven significant digits.
    """
    if reading.is_overload:
        return "OVLD"
    significant = int(resolution_digits) + 1
    value = reading.value + 0.0  # -0.0 would otherwise print as "-0.000000"
    exponent = 0 if value == 0 else math.floor(math.log10(abs(value)))
    prefix_exponent = _clamp(math.floor(exponent / _PREFIX_STEP) * _PREFIX_STEP)
    text = _scaled_text(value, prefix_exponent, significant)
    if abs(float(text)) >= 10**_PREFIX_STEP and prefix_exponent < _MAX_PREFIX_EXPONENT:
        # Rounding carried into the next power of a thousand (999.99999 mV -> 1.000000 V).
        prefix_exponent += _PREFIX_STEP
        text = _scaled_text(value, prefix_exponent, significant)
    return f"{text} {_PREFIXES[prefix_exponent]}{reading.function.unit}"


def _clamp(prefix_exponent: int) -> int:
    return max(_MIN_PREFIX_EXPONENT, min(_MAX_PREFIX_EXPONENT, prefix_exponent))


def _scaled_text(value: float, prefix_exponent: int, significant: int) -> str:
    scaled = value / 10**prefix_exponent
    integer_digits = 1 if scaled == 0 else max(1, math.floor(math.log10(abs(scaled))) + 1)
    return f"{scaled:.{max(significant - integer_digits, 0)}f}"
