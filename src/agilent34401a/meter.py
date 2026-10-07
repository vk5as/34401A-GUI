"""The Meter model: Functions, Setups, Readings, and turning replies into Readings and Readings into text."""

import math
from dataclasses import dataclass, replace
from enum import Enum

from agilent34401a.errors import InvalidSetupError, MalformedReplyError

# Engineering prefixes by power of ten, smallest to largest.
_PREFIXES = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}
_MIN_PREFIX_EXPONENT = min(_PREFIXES)
_MAX_PREFIX_EXPONENT = max(_PREFIXES)
_PREFIX_STEP = 3

# The Meter reports Overload as +/-9.9E+37; anything at or beyond this magnitude is an Overload.
_OVERLOAD_MAGNITUDE = 9.9e37

_LINE_FREQUENCY_HZ = 50
_AUTOZERO_FACTOR = 2  # with Autozero on, every Reading is an offset measurement plus the signal measurement


class Resolution(Enum):
    """The number of displayed digits. The half digit is the leading 0 or 1."""

    FOUR_HALF = 4.5
    FIVE_HALF = 5.5
    SIX_HALF = 6.5

    @property
    def significant_digits(self) -> int:
        return int(self.value) + 1

    @property
    def label(self) -> str:
        return f"{int(self.value)}½ digits"


NPLC_VALUES = (0.02, 0.2, 1, 10, 100)
"""The Integration Times, in power-line cycles, that the Meter accepts."""

_RESOLUTION_FOR_NPLC = {
    0.02: Resolution.FOUR_HALF,
    0.2: Resolution.FIVE_HALF,
    1: Resolution.FIVE_HALF,
    10: Resolution.SIX_HALF,
    100: Resolution.SIX_HALF,
}
# The Integration Time the Meter settles on when only a Resolution is asked for.
_NPLC_FOR_RESOLUTION = {Resolution.FOUR_HALF: 0.02, Resolution.FIVE_HALF: 1, Resolution.SIX_HALF: 10}
_DEFAULT_NPLC = 10
# How long a frequency or period measurement counts for, by Resolution.
_GATE_TIME_S = {Resolution.FOUR_HALF: 0.01, Resolution.FIVE_HALF: 0.1, Resolution.SIX_HALF: 1.0}
_AC_READING_S = 1.0  # the 20 Hz AC Filter, the Meter's default, needs about a second to settle
_FIXED_FUNCTION_READING_S = 0.05

_DC_VOLTAGE_RANGES = (0.1, 1.0, 10.0, 100.0, 1000.0)
_AC_VOLTAGE_RANGES = (0.1, 1.0, 10.0, 100.0, 750.0)
_RESISTANCE_RANGES = (100.0, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8)


@dataclass(frozen=True)
class _FunctionSpec:
    label: str
    unit: str
    ranges: tuple[float, ...]
    range_unit: str
    has_integration_time: bool = False
    fixed_resolution: Resolution | None = None
    uses_prefixes: bool = True


class Function(Enum):
    """A quantity the Meter measures. The value is its SCPI name, as sent with `FUNC`."""

    DC_VOLTAGE = "VOLT:DC"
    AC_VOLTAGE = "VOLT:AC"
    DC_CURRENT = "CURR:DC"
    AC_CURRENT = "CURR:AC"
    RESISTANCE_2W = "RES"
    RESISTANCE_4W = "FRES"
    FREQUENCY = "FREQ"
    PERIOD = "PER"
    CONTINUITY = "CONT"
    DIODE = "DIOD"
    DC_VOLTAGE_RATIO = "VOLT:DC:RAT"

    @property
    def _spec(self) -> _FunctionSpec:
        return _SPECS[self]

    @property
    def label(self) -> str:
        return self._spec.label

    @property
    def unit(self) -> str:
        return self._spec.unit

    @property
    def ranges(self) -> tuple[float, ...]:
        """The Ranges the Meter offers for this Function; empty when the Range is fixed."""
        return self._spec.ranges

    @property
    def range_unit(self) -> str:
        """The unit a Range is expressed in, which is the input's: frequency is ranged in volts."""
        return self._spec.range_unit

    @property
    def has_integration_time(self) -> bool:
        return self._spec.has_integration_time

    @property
    def fixed_resolution(self) -> Resolution | None:
        """The Resolution this Function always measures at, or None when it can be chosen."""
        return self._spec.fixed_resolution

    @property
    def uses_prefixes(self) -> bool:
        return self._spec.uses_prefixes


# Frequency and period are fixed at 5½ digits until Gate Time can be chosen.
_SPECS = {
    Function.DC_VOLTAGE: _FunctionSpec("DC V", "V", _DC_VOLTAGE_RANGES, "V", has_integration_time=True),
    Function.AC_VOLTAGE: _FunctionSpec("AC V", "V", _AC_VOLTAGE_RANGES, "V", fixed_resolution=Resolution.SIX_HALF),
    Function.DC_CURRENT: _FunctionSpec("DC I", "A", (0.01, 0.1, 1.0, 3.0), "A", has_integration_time=True),
    Function.AC_CURRENT: _FunctionSpec("AC I", "A", (1.0, 3.0), "A", fixed_resolution=Resolution.SIX_HALF),
    Function.RESISTANCE_2W: _FunctionSpec("2-wire Ω", "Ω", _RESISTANCE_RANGES, "Ω", has_integration_time=True),
    Function.RESISTANCE_4W: _FunctionSpec("4-wire Ω", "Ω", _RESISTANCE_RANGES, "Ω", has_integration_time=True),
    Function.FREQUENCY: _FunctionSpec(
        "Frequency", "Hz", _AC_VOLTAGE_RANGES, "V", fixed_resolution=Resolution.FIVE_HALF
    ),
    Function.PERIOD: _FunctionSpec("Period", "s", _AC_VOLTAGE_RANGES, "V", fixed_resolution=Resolution.FIVE_HALF),
    Function.CONTINUITY: _FunctionSpec("Continuity", "Ω", (), "Ω", fixed_resolution=Resolution.FIVE_HALF),
    Function.DIODE: _FunctionSpec("Diode", "V", (), "V", fixed_resolution=Resolution.FIVE_HALF),
    Function.DC_VOLTAGE_RATIO: _FunctionSpec(
        "DC V ratio", "", _DC_VOLTAGE_RANGES, "V", has_integration_time=True, uses_prefixes=False
    ),
}


@dataclass(frozen=True)
class Setup:
    """The settings that decide what a Reading means. Invalid combinations cannot be built.

    `range` is None for Autorange. `nplc` is the Integration Time for the Functions that have one, and the
    Resolution always agrees with it.
    """

    function: Function
    range: float | None
    resolution: Resolution
    nplc: float | None

    def __post_init__(self) -> None:
        function = self.function
        if self.range is not None and self.range not in function.ranges:
            offered = ", ".join(format_range(function, value) for value in function.ranges) or "no Range choice"
            message = f"{function.label} has {offered}, not a Range of {self.range:g}"
            raise InvalidSetupError(message)
        if function.has_integration_time:
            if self.nplc not in NPLC_VALUES:
                message = f"{function.label} has an Integration Time of {NPLC_VALUES} NPLC, not {self.nplc}"
                raise InvalidSetupError(message)
        elif self.nplc is not None:
            message = f"{function.label} has no Integration Time"
            raise InvalidSetupError(message)
        expected = function.fixed_resolution or (None if self.nplc is None else _RESOLUTION_FOR_NPLC[self.nplc])
        if self.resolution is not expected:
            message = f"Resolution of {function.label} is {expected and expected.label}, not {self.resolution.label}"
            raise InvalidSetupError(message)

    @classmethod
    def default(cls, function: Function) -> "Setup":
        """Return the Setup the Meter has for `function` after a reset: Autorange, and 10 NPLC where that applies."""
        if function.has_integration_time:
            return cls(function, None, _RESOLUTION_FOR_NPLC[_DEFAULT_NPLC], _DEFAULT_NPLC)
        return cls(function, None, function.fixed_resolution or Resolution.SIX_HALF, None)

    def with_range(self, range_value: float | None) -> "Setup":
        return replace(self, range=range_value)

    def with_resolution(self, resolution: Resolution) -> "Setup":
        """Choose a Resolution, which also sets the Integration Time that usually goes with it."""
        if self.function.has_integration_time:
            return replace(self, resolution=resolution, nplc=_NPLC_FOR_RESOLUTION[resolution])
        return replace(self, resolution=resolution)

    def with_nplc(self, nplc: float) -> "Setup":
        """Choose an Integration Time, which also decides the Resolution."""
        return replace(self, nplc=nplc, resolution=_RESOLUTION_FOR_NPLC.get(nplc, self.resolution))


def describe_setup(setup: Setup) -> str:
    """One line for the readout: Function, Range, Resolution and Integration Time."""
    parts = [setup.function.label]
    if setup.function.ranges:
        parts.append("Autorange" if setup.range is None else f"{format_range(setup.function, setup.range)} range")
    parts.append(setup.resolution.label)
    if setup.nplc is not None:
        parts.append(f"{setup.nplc:g} NPLC")
    return " · ".join(parts)


def measurement_time(setup: Setup) -> float:
    """Seconds the Meter needs for one Reading in this Setup, with Autozero on and the Range already settled."""
    function = setup.function
    if function.has_integration_time:
        seconds = (setup.nplc or _DEFAULT_NPLC) / _LINE_FREQUENCY_HZ * _AUTOZERO_FACTOR
        # A ratio measures the sense input and the reference input.
        return seconds * 2 if function is Function.DC_VOLTAGE_RATIO else seconds
    if function in (Function.AC_VOLTAGE, Function.AC_CURRENT):
        return _AC_READING_S
    if function in (Function.FREQUENCY, Function.PERIOD):
        return _GATE_TIME_S[setup.resolution]
    return _FIXED_FUNCTION_READING_S


_MIN_TIMEOUT_S = 2.0
_TIMEOUT_MARGIN = 1.5
_TIMEOUT_EXTRA_S = 1.0
_AUTORANGE_STEPS = 5  # Autorange may measure at several Ranges before it settles


def reading_timeout(setup: Setup) -> float:
    """Seconds to wait for a Reading in this Setup before giving up (ADR-0002)."""
    steps = _AUTORANGE_STEPS if setup.range is None and setup.function.ranges else 1
    return max(_MIN_TIMEOUT_S, measurement_time(setup) * steps * _TIMEOUT_MARGIN + _TIMEOUT_EXTRA_S)


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


def format_reading(reading: Reading, resolution: Resolution = Resolution.SIX_HALF) -> str:
    """Show a Reading with an engineering prefix and its unit, or OVLD for an Overload.

    A 6½-digit Reading shows seven significant digits, because the half digit is the leading 0 or 1.
    """
    if reading.is_overload:
        return "OVLD"
    significant = resolution.significant_digits
    value = reading.value + 0.0  # -0.0 would otherwise print as "-0.000000"
    if not reading.function.uses_prefixes:
        return _scaled_text(value, 0, significant)
    prefix_exponent = _prefix_exponent(value)
    text = _scaled_text(value, prefix_exponent, significant)
    if abs(float(text)) >= 10**_PREFIX_STEP and prefix_exponent < _MAX_PREFIX_EXPONENT:
        # Rounding carried into the next power of a thousand (999.99999 mV -> 1.000000 V).
        prefix_exponent += _PREFIX_STEP
        text = _scaled_text(value, prefix_exponent, significant)
    return f"{text} {_PREFIXES[prefix_exponent]}{reading.function.unit}"


def format_range(function: Function, value: float) -> str:
    """Name a Range the way the Meter's front panel does: 100 mV, 10 kΩ, 3 A."""
    prefix_exponent = _prefix_exponent(value)
    scaled = round(value / 10**prefix_exponent, 9)
    return f"{scaled:g} {_PREFIXES[prefix_exponent]}{function.range_unit}"


def _prefix_exponent(value: float) -> int:
    exponent = 0 if value == 0 else math.floor(math.log10(abs(value)))
    return max(_MIN_PREFIX_EXPONENT, min(_MAX_PREFIX_EXPONENT, math.floor(exponent / _PREFIX_STEP) * _PREFIX_STEP))


def _scaled_text(value: float, prefix_exponent: int, significant: int) -> str:
    scaled = value / 10**prefix_exponent
    integer_digits = 1 if scaled == 0 else max(1, math.floor(math.log10(abs(scaled))) + 1)
    return f"{scaled:.{max(significant - integer_digits, 0)}f}"
