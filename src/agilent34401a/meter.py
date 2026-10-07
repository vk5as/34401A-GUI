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


class GateTime(Enum):
    """How long a frequency or period measurement counts for. It decides the Resolution of those Functions."""

    TEN_MILLISECONDS = 0.01
    HUNDRED_MILLISECONDS = 0.1
    ONE_SECOND = 1.0

    @property
    def seconds(self) -> float:
        return self.value

    @property
    def label(self) -> str:
        return "1 s" if self is GateTime.ONE_SECOND else f"{round(self.value * 1000)} ms"

    @property
    def resolution(self) -> Resolution:
        return _RESOLUTION_FOR_GATE_TIME[self]


_RESOLUTION_FOR_GATE_TIME = {
    GateTime.TEN_MILLISECONDS: Resolution.FOUR_HALF,
    GateTime.HUNDRED_MILLISECONDS: Resolution.FIVE_HALF,
    GateTime.ONE_SECOND: Resolution.SIX_HALF,
}
_GATE_TIME_FOR_RESOLUTION = {resolution: gate_time for gate_time, resolution in _RESOLUTION_FOR_GATE_TIME.items()}


class AcFilter(Enum):
    """The lowest signal frequency the AC detector is tuned for. The lower it is, the longer a Reading settles."""

    SLOW = 3
    MEDIUM = 20
    FAST = 200

    @property
    def hertz(self) -> int:
        return self.value

    @property
    def label(self) -> str:
        return f"{self.value} Hz"

    @property
    def settling_seconds(self) -> float:
        return _AC_SETTLING_S[self]


_AC_SETTLING_S = {AcFilter.SLOW: 7.0, AcFilter.MEDIUM: 1.0, AcFilter.FAST: 0.1}


class Autozero(Enum):
    """Whether the Meter takes an offset measurement alongside each Reading.

    `ONCE` takes a single offset measurement and then leaves Autozero off, so the Meter never reports it back.
    """

    ON = "ON"
    OFF = "OFF"
    ONCE = "ONCE"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class InputImpedance(Enum):
    """What DC voltage inputs on the lower Ranges present to the circuit."""

    TEN_MEGOHM = "10 MΩ"
    HIGH_IMPEDANCE = ">10 GΩ"

    @property
    def label(self) -> str:
        return self.value


class Terminals(Enum):
    """Which set of input jacks is active. The Meter's front panel switch decides; it can only be read."""

    FRONT = "FRON"
    REAR = "REAR"

    @property
    def label(self) -> str:
        return "Front" if self is Terminals.FRONT else "Rear"


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
_DEFAULT_AC_FILTER = AcFilter.MEDIUM
_DEFAULT_GATE_TIME = GateTime.HUNDRED_MILLISECONDS
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
    has_ac_filter: bool = False
    has_gate_time: bool = False
    has_input_impedance: bool = False


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

    @property
    def has_ac_filter(self) -> bool:
        return self._spec.has_ac_filter

    @property
    def has_gate_time(self) -> bool:
        """Frequency and period count over a Gate Time, which decides their Resolution."""
        return self._spec.has_gate_time

    @property
    def has_autozero(self) -> bool:
        """Autozero applies to the Functions that integrate."""
        return self._spec.has_integration_time

    @property
    def has_input_impedance(self) -> bool:
        return self._spec.has_input_impedance


_SPECS = {
    Function.DC_VOLTAGE: _FunctionSpec(
        "DC V", "V", _DC_VOLTAGE_RANGES, "V", has_integration_time=True, has_input_impedance=True
    ),
    Function.AC_VOLTAGE: _FunctionSpec(
        "AC V", "V", _AC_VOLTAGE_RANGES, "V", fixed_resolution=Resolution.SIX_HALF, has_ac_filter=True
    ),
    Function.DC_CURRENT: _FunctionSpec("DC I", "A", (0.01, 0.1, 1.0, 3.0), "A", has_integration_time=True),
    Function.AC_CURRENT: _FunctionSpec(
        "AC I", "A", (1.0, 3.0), "A", fixed_resolution=Resolution.SIX_HALF, has_ac_filter=True
    ),
    Function.RESISTANCE_2W: _FunctionSpec("2-wire Ω", "Ω", _RESISTANCE_RANGES, "Ω", has_integration_time=True),
    Function.RESISTANCE_4W: _FunctionSpec("4-wire Ω", "Ω", _RESISTANCE_RANGES, "Ω", has_integration_time=True),
    Function.FREQUENCY: _FunctionSpec("Frequency", "Hz", _AC_VOLTAGE_RANGES, "V", has_gate_time=True),
    Function.PERIOD: _FunctionSpec("Period", "s", _AC_VOLTAGE_RANGES, "V", has_gate_time=True),
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
    Resolution always agrees with it. The same goes for `gate_time` of frequency and period. Each sense option is
    None for the Functions it does not apply to and set for the ones it does: `ac_filter` for AC Functions,
    `gate_time` for frequency and period, `autozero` for the Functions that integrate, and `input_impedance` for DC
    voltage.
    """

    function: Function
    range: float | None
    resolution: Resolution
    nplc: float | None
    ac_filter: AcFilter | None = None
    gate_time: GateTime | None = None
    autozero: Autozero | None = None
    input_impedance: InputImpedance | None = None

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
        self._require_sense_options()
        expected = function.fixed_resolution
        if expected is None:
            expected = self.gate_time.resolution if self.gate_time else _RESOLUTION_FOR_NPLC.get(self.nplc or 0)
        if self.resolution is not expected:
            message = f"Resolution of {function.label} is {expected and expected.label}, not {self.resolution.label}"
            raise InvalidSetupError(message)

    def _require_sense_options(self) -> None:
        function = self.function
        options = (
            ("AC Filter", function.has_ac_filter, self.ac_filter),
            ("Gate Time", function.has_gate_time, self.gate_time),
            ("Autozero", function.has_autozero, self.autozero),
            ("Input Impedance", function.has_input_impedance, self.input_impedance),
        )
        for name, applies, value in options:
            if applies and value is None:
                message = f"{function.label} needs its {name}"
                raise InvalidSetupError(message)
            if not applies and value is not None:
                message = f"{function.label} has no {name}"
                raise InvalidSetupError(message)

    @classmethod
    def default(cls, function: Function) -> "Setup":
        """Return the Setup the Meter has for `function` after a reset: Autorange, and 10 NPLC where that applies."""
        gate_time = _DEFAULT_GATE_TIME if function.has_gate_time else None
        if function.has_integration_time:
            resolution = _RESOLUTION_FOR_NPLC[_DEFAULT_NPLC]
        else:
            resolution = gate_time.resolution if gate_time else function.fixed_resolution or Resolution.SIX_HALF
        return cls(
            function,
            None,
            resolution,
            _DEFAULT_NPLC if function.has_integration_time else None,
            ac_filter=_DEFAULT_AC_FILTER if function.has_ac_filter else None,
            gate_time=gate_time,
            autozero=Autozero.ON if function.has_autozero else None,
            input_impedance=InputImpedance.TEN_MEGOHM if function.has_input_impedance else None,
        )

    def with_range(self, range_value: float | None) -> "Setup":
        return replace(self, range=range_value)

    def with_resolution(self, resolution: Resolution) -> "Setup":
        """Choose a Resolution, which also sets the Integration Time that usually goes with it."""
        if self.function.has_integration_time:
            return replace(self, resolution=resolution, nplc=_NPLC_FOR_RESOLUTION[resolution])
        if self.function.has_gate_time:
            return replace(self, resolution=resolution, gate_time=_GATE_TIME_FOR_RESOLUTION[resolution])
        return replace(self, resolution=resolution)

    def with_nplc(self, nplc: float) -> "Setup":
        """Choose an Integration Time, which also decides the Resolution."""
        return replace(self, nplc=nplc, resolution=_RESOLUTION_FOR_NPLC.get(nplc, self.resolution))

    def with_gate_time(self, gate_time: GateTime) -> "Setup":
        """Choose a Gate Time, which also decides the Resolution."""
        return replace(self, gate_time=gate_time, resolution=gate_time.resolution)

    def with_ac_filter(self, ac_filter: AcFilter) -> "Setup":
        return replace(self, ac_filter=ac_filter)

    def with_autozero(self, autozero: Autozero) -> "Setup":
        return replace(self, autozero=autozero)

    def with_input_impedance(self, input_impedance: InputImpedance) -> "Setup":
        return replace(self, input_impedance=input_impedance)


def describe_setup(setup: Setup) -> str:
    """One line for the readout: Function, Range, Resolution and Integration Time."""
    parts = [setup.function.label]
    if setup.function.ranges:
        parts.append("Autorange" if setup.range is None else f"{format_range(setup.function, setup.range)} range")
    parts.append(setup.resolution.label)
    if setup.nplc is not None:
        parts.append(f"{setup.nplc:g} NPLC")
    if setup.ac_filter is not None:
        parts.append(f"{setup.ac_filter.label} filter")
    if setup.gate_time is not None:
        parts.append(f"{setup.gate_time.label} gate")
    # The options a reset Meter has are left out; only a departure from them is worth the room.
    if setup.autozero not in (None, Autozero.ON):
        parts.append(f"Autozero {setup.autozero.label.lower()}")
    if setup.input_impedance not in (None, InputImpedance.TEN_MEGOHM):
        parts.append(f"{setup.input_impedance.label} input")
    return " · ".join(parts)


def measurement_time(setup: Setup) -> float:
    """Seconds the Meter needs for one Reading in this Setup, with the Range already settled."""
    function = setup.function
    if function.has_integration_time:
        seconds = (setup.nplc or _DEFAULT_NPLC) / _LINE_FREQUENCY_HZ
        if setup.autozero is Autozero.ON:
            seconds *= _AUTOZERO_FACTOR
        # A ratio measures the sense input and the reference input.
        return seconds * 2 if function is Function.DC_VOLTAGE_RATIO else seconds
    if setup.ac_filter is not None:
        return setup.ac_filter.settling_seconds
    if setup.gate_time is not None:
        return setup.gate_time.seconds
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
