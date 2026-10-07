"""The Simulator: a software Meter that plugs in as a Transport."""

import math
import os
import random
import time
from collections import deque
from collections.abc import Callable, Mapping

from agilent34401a.applied_signal import AppliedSignal
from agilent34401a.errors import TransportError, TransportTimeoutError
from agilent34401a.meter import NPLC_VALUES, Function, Setup, measurement_time

HEWLETT_PACKARD_IDENTITY = "HEWLETT-PACKARD,34401A,0,10-5-2"
AGILENT_IDENTITY = "Agilent Technologies,34401A,MY45000001,11-5-2"

TIME_SCALE_ENV_VAR = "AGILENT34401A_SIM_TIME_SCALE"
"""Overrides the default time scale: 1 is real time, 0 is instant."""

_OVERLOAD_REPLY = "9.90000000E+37"
_OVER_RANGE = 1.2  # most ranges read up to 120 % of their full scale
# These ranges cannot be exceeded: the 1000 V and 750 V limits are safety limits, and 3 A is the fuse.
_NO_OVER_RANGE = frozenset(
    {
        (Function.DC_VOLTAGE, 1000.0),
        (Function.AC_VOLTAGE, 750.0),
        (Function.DC_CURRENT, 3.0),
        (Function.AC_CURRENT, 3.0),
    }
)
_NO_ERROR = '+0,"No error"'
_UNDEFINED_HEADER = '-113,"Undefined header"'
_MISSING_PARAMETER = '-109,"Missing parameter"'
_ILLEGAL_PARAMETER = '-224,"Illegal parameter value"'
_DATA_OUT_OF_RANGE = '-222,"Data out of range"'

# The Applied Signal on each Function's terminals until a test chooses another.
_DEFAULT_SIGNALS = {
    Function.DC_VOLTAGE: 1.0,
    Function.AC_VOLTAGE: 1.0,
    Function.DC_CURRENT: 0.001,
    Function.AC_CURRENT: 0.001,
    Function.RESISTANCE_2W: 1000.0,
    Function.RESISTANCE_4W: 1000.0,
    Function.FREQUENCY: 1000.0,
    Function.PERIOD: 0.001,
    Function.CONTINUITY: 0.5,
    Function.DIODE: 0.6,
    Function.DC_VOLTAGE_RATIO: 1.0,
}

DEMO_SIGNALS: Mapping[Function, AppliedSignal] = {
    Function.DC_VOLTAGE: AppliedSignal(1.0, noise=0.0005, drift=0.0001, sine_amplitude=0.002, sine_frequency_hz=0.2),
    Function.AC_VOLTAGE: AppliedSignal(1.0, noise=0.001, sine_amplitude=0.005, sine_frequency_hz=0.1),
    Function.DC_CURRENT: AppliedSignal(0.001, noise=0.000002, sine_amplitude=0.00001, sine_frequency_hz=0.2),
    Function.RESISTANCE_2W: AppliedSignal(1000.0, noise=0.2, drift=0.01, sine_amplitude=0.5, sine_frequency_hz=0.1),
    Function.RESISTANCE_4W: AppliedSignal(1000.0, noise=0.05, drift=0.01, sine_amplitude=0.2, sine_frequency_hz=0.1),
    Function.FREQUENCY: AppliedSignal(1000.0, noise=0.05, sine_amplitude=0.5, sine_frequency_hz=0.05),
}
"""Applied Signals that wander like a real bench source, for `--simulate` to show the chart something to draw."""

# SCPI lets a header be written in full or abbreviated; the Simulator understands the long forms it is likely to meet.
_LONG_FORMS = {
    "SENSE": "SENS",
    "SYSTEM": "SYST",
    "ERROR": "ERR",
    "FUNCTION": "FUNC",
    "VOLTAGE": "VOLT",
    "CURRENT": "CURR",
    "RESISTANCE": "RES",
    "FRESISTANCE": "FRES",
    "FREQUENCY": "FREQ",
    "PERIOD": "PER",
    "CONTINUITY": "CONT",
    "DIODE": "DIOD",
    "RANGE": "RANG",
    "NPLCYCLES": "NPLC",
    "RATIO": "RAT",
}

# What `FUNC?` answers for each Function.
_FUNCTION_REPLIES = {
    Function.DC_VOLTAGE: '"VOLT"',
    Function.AC_VOLTAGE: '"VOLT:AC"',
    Function.DC_CURRENT: '"CURR"',
    Function.AC_CURRENT: '"CURR:AC"',
    Function.RESISTANCE_2W: '"RES"',
    Function.RESISTANCE_4W: '"FRES"',
    Function.FREQUENCY: '"FREQ"',
    Function.PERIOD: '"PER"',
    Function.CONTINUITY: '"CONT"',
    Function.DIODE: '"DIOD"',
    Function.DC_VOLTAGE_RATIO: '"VOLT:RAT"',
}
# The names `FUNC` accepts, once header long forms are shortened and a trailing DC is implied.
_FUNCTION_NAMES = {
    "VOLT": Function.DC_VOLTAGE,
    "VOLT:DC": Function.DC_VOLTAGE,
    "VOLT:AC": Function.AC_VOLTAGE,
    "CURR": Function.DC_CURRENT,
    "CURR:DC": Function.DC_CURRENT,
    "CURR:AC": Function.AC_CURRENT,
    "RES": Function.RESISTANCE_2W,
    "FRES": Function.RESISTANCE_4W,
    "FREQ": Function.FREQUENCY,
    "PER": Function.PERIOD,
    "CONT": Function.CONTINUITY,
    "DIOD": Function.DIODE,
    "VOLT:RAT": Function.DC_VOLTAGE_RATIO,
    "VOLT:DC:RAT": Function.DC_VOLTAGE_RATIO,
}
# Where each Function's Range (and Integration Time) commands live. Ratio shares DC voltage's settings.
_SETTING_GROUPS = {
    "VOLT:DC": Function.DC_VOLTAGE,
    "VOLT:AC": Function.AC_VOLTAGE,
    "CURR:DC": Function.DC_CURRENT,
    "CURR:AC": Function.AC_CURRENT,
    "RES": Function.RESISTANCE_2W,
    "FRES": Function.RESISTANCE_4W,
    "FREQ:VOLT": Function.FREQUENCY,
    "PER:VOLT": Function.PERIOD,
}
_RANGE_SCALED = (
    Function.DC_VOLTAGE,
    Function.AC_VOLTAGE,
    Function.DC_CURRENT,
    Function.AC_CURRENT,
    Function.RESISTANCE_2W,
    Function.RESISTANCE_4W,
)
_CONTINUITY_LIMIT = 1.2e3
_DIODE_LIMIT = 1.2


class Simulator:
    """One simulated Meter, driven through the same text interface as a real one.

    `signals` sets the Applied Signal per Function: a plain number is a constant value, an `AppliedSignal` adds
    noise, drift or a sine (`dc_voltage` is a shortcut for DC voltage). Drift and the sine follow the Simulator's
    own clock (`signal_time`), which moves on by the time each Reading takes whatever the `time_scale`, so a run is
    repeatable. Noise comes from `random_source`, or from a generator started with `seed`. `time_scale` stretches
    (or, at 0, removes) the time a Reading takes; it defaults to the `AGILENT34401A_SIM_TIME_SCALE` environment
    variable, or real time when that is unset.
    """

    def __init__(  # noqa: PLR0913 - every one is an optional keyword that tests and the server pick from
        self,
        *,
        identity: str = HEWLETT_PACKARD_IDENTITY,
        dc_voltage: float | None = None,
        signals: Mapping[Function, float | AppliedSignal] | None = None,
        time_scale: float | None = None,
        seed: int | None = None,
        random_source: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if time_scale is None:
            time_scale = float(os.environ.get(TIME_SCALE_ENV_VAR, "1"))
        if time_scale < 0:
            message = f"The time scale cannot be negative, got {time_scale}"
            raise ValueError(message)
        self.timeout = 2.0
        self.time_scale = time_scale
        self._identity = identity
        self._signals = {function: AppliedSignal(value) for function, value in _DEFAULT_SIGNALS.items()}
        for function, signal in (signals or {}).items():
            self.set_signal(function, signal)
        if dc_voltage is not None:
            self.set_signal(Function.DC_VOLTAGE, dc_voltage)
        self._random = random_source if random_source is not None else random.Random(seed)  # nosec B311  # noqa: S311
        self._signal_time = 0.0
        self._sleep = sleep
        self._closed = False
        self._errors: deque[str] = deque()
        # Each pending reply carries the real-time seconds the Meter needs before it can send it.
        self._replies: deque[tuple[str, float]] = deque()
        self._reset()

    @property
    def signal_time(self) -> float:
        """Simulated seconds since the Simulator started, which is what drift and the sine component follow."""
        return self._signal_time

    def set_signal(self, function: Function, signal: float | AppliedSignal) -> None:
        """Change the Applied Signal on `function`'s terminals; a plain number is a constant value."""
        self._signals[function] = signal if isinstance(signal, AppliedSignal) else AppliedSignal(signal)

    def _reset(self) -> None:
        self._function = Function.DC_VOLTAGE
        self._ranges: dict[Function, float | None] = dict.fromkeys(_SETTING_GROUPS.values())
        self._nplc: dict[Function, float] = {
            group: Setup.default(group).nplc or 0 for group in _SETTING_GROUPS.values() if group.has_integration_time
        }

    def write(self, command: str) -> None:
        self._require_open()
        header, _, argument = command.strip().partition(" ")
        header = header.upper()
        argument = argument.strip()
        if header.startswith("*"):
            self._common_command(header)
            return
        query = header.endswith("?")
        path = _mnemonics(header.removesuffix("?"))
        key = ":".join(path)
        if key == "SYST:ERR" and query:
            self._reply(self._errors.popleft() if self._errors else _NO_ERROR)
        elif key == "READ" and query:
            setup = self._setup()
            self._reply(self._measure(setup), measurement_time(setup))
        elif key == "FUNC":
            self._function_command(query=query, argument=argument)
        elif not self._setting_command(path, query=query, argument=argument):
            self._errors.append(_UNDEFINED_HEADER)

    def _common_command(self, header: str) -> None:
        match header:
            case "*IDN?":
                self._reply(self._identity)
            case "*CLS":
                self._errors.clear()
            case "*RST":
                self._reset()
            case _:
                self._errors.append(_UNDEFINED_HEADER)

    def _function_command(self, *, query: bool, argument: str) -> None:
        if query:
            self._reply(_FUNCTION_REPLIES[self._function])
            return
        if not argument:
            self._errors.append(_MISSING_PARAMETER)
            return
        name = ":".join(_mnemonics(argument.strip("\"'").upper()))
        function = _FUNCTION_NAMES.get(name)
        if function is None:
            self._errors.append(_ILLEGAL_PARAMETER)
        else:
            self._function = function

    def _setting_command(self, path: tuple[str, ...], *, query: bool, argument: str) -> bool:
        """Handle `<group>:RANG`, `<group>:RANG:AUTO` and `<group>:NPLC`; False when the header is not one."""
        if path[-2:] == ("RANG", "AUTO"):
            group, setter, getter = path[:-2], self._set_autorange, self._get_autorange
        elif path[-1:] == ("RANG",):
            group, setter, getter = path[:-1], self._set_range, self._get_range
        elif path[-1:] == ("NPLC",):
            group, setter, getter = path[:-1], self._set_nplc, self._get_nplc
        else:
            return False
        function = _SETTING_GROUPS.get(":".join(group))
        if function is None or (path[-1] == "NPLC" and not function.has_integration_time):
            return False
        if query:
            self._reply(getter(function))
        elif not argument:
            self._errors.append(_MISSING_PARAMETER)
        else:
            setter(function, argument.upper())
        return True

    def _set_range(self, function: Function, word: str) -> None:
        ranges = function.ranges
        if word == "MIN":
            self._ranges[function] = ranges[0]
        elif word == "MAX":
            self._ranges[function] = ranges[-1]
        elif word == "DEF":
            self._ranges[function] = None
        else:
            value = _number(word)
            if value is None:
                self._errors.append(_ILLEGAL_PARAMETER)
                return
            # A value between two ranges selects the next one up.
            fitting = [candidate for candidate in ranges if candidate >= abs(value)]
            if not fitting:
                self._errors.append(_DATA_OUT_OF_RANGE)
                return
            self._ranges[function] = fitting[0]

    def _get_range(self, function: Function) -> str:
        fixed = self._ranges[function]
        return _number_reply(fixed if fixed is not None else self._autorange(function))

    def _set_autorange(self, function: Function, word: str) -> None:
        if word in ("ON", "1"):
            self._ranges[function] = None
        elif word in ("OFF", "0"):
            # Switching Autorange off holds whichever range it had settled on.
            if self._ranges[function] is None:
                self._ranges[function] = self._autorange(function)
        else:
            self._errors.append(_ILLEGAL_PARAMETER)

    def _get_autorange(self, function: Function) -> str:
        return "1" if self._ranges[function] is None else "0"

    def _set_nplc(self, function: Function, word: str) -> None:
        shortcuts = {"MIN": NPLC_VALUES[0], "MAX": NPLC_VALUES[-1], "DEF": 10}
        value = shortcuts.get(word, _number(word))
        if value is None or value not in NPLC_VALUES:
            self._errors.append(_ILLEGAL_PARAMETER)
        else:
            self._nplc[function] = value

    def _get_nplc(self, function: Function) -> str:
        return _number_reply(self._nplc[function])

    def _setup(self) -> Setup:
        """Return the Setup the Meter is in right now, as the Meter model describes it."""
        function = self._function
        group = Function.DC_VOLTAGE if function is Function.DC_VOLTAGE_RATIO else function
        setup = Setup.default(function)
        if function.ranges:
            setup = setup.with_range(self._ranges.get(group))
        if function.has_integration_time:
            setup = setup.with_nplc(self._nplc[group])
        return setup

    def _autorange(self, function: Function, signal: float | None = None) -> float:
        """Return the lowest range of `function` that holds `signal`, or the highest when none does.

        Without a `signal` it uses the largest the Applied Signal reaches without noise or drift.
        """
        signal = self._signals[function].peak if signal is None else abs(signal)
        return next(
            (candidate for candidate in function.ranges if signal <= _limit(function, candidate)),
            function.ranges[-1],
        )

    def _measure(self, setup: Setup) -> str:
        function = setup.function
        signal = self._signals[function].at(self._signal_time, self._random)
        self._signal_time += measurement_time(setup)
        if function in _RANGE_SCALED:
            range_in_use = setup.range or self._autorange(function, signal)
            if abs(signal) > _limit(function, range_in_use):
                return _overload(signal)
            # The last displayed digit is a millionth of the range at 6½ digits, a hundred-thousandth at 5½.
            step = range_in_use / 10 ** (setup.resolution.significant_digits - 1)
            value = round(signal / step) * step
        else:
            limits = {Function.CONTINUITY: _CONTINUITY_LIMIT, Function.DIODE: _DIODE_LIMIT}
            if abs(signal) > limits.get(function, math.inf):
                return _overload(signal)
            value = float(f"{signal:.{setup.resolution.significant_digits - 1}e}")
        return _number_reply(value)

    @property
    def has_reply(self) -> bool:
        """Whether a reply is waiting to be read, which is how a server knows a line was a query."""
        return bool(self._replies)

    def _reply(self, text: str, duration: float = 0.0) -> None:
        self._replies.append((text, duration))

    def read(self) -> str:
        self._require_open()
        if not self._replies:
            message = "The Simulator has nothing to send"
            raise TransportTimeoutError(message)
        reply, duration = self._replies.popleft()
        duration *= self.time_scale
        if duration > self.timeout:
            message = f"The Simulator needs {duration:g} s to reply but the timeout is {self.timeout:g} s"
            raise TransportTimeoutError(message)
        if duration > 0:
            self._sleep(duration)
        return reply

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self._require_open()
        self._replies.clear()

    def close(self) -> None:
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            message = "The Simulator is closed"
            raise TransportError(message)


def _limit(function: Function, range_value: float) -> float:
    """Return the largest magnitude a range reads before it overloads."""
    return range_value if (function, range_value) in _NO_OVER_RANGE else range_value * _OVER_RANGE


def _mnemonics(header: str) -> tuple[str, ...]:
    """Split a SCPI header into short-form mnemonics, dropping a leading colon and the optional SENSe."""
    parts = [_LONG_FORMS.get(part, part) for part in header.strip(":").split(":")]
    return tuple(parts[1:] if parts[0] == "SENS" and len(parts) > 1 else parts)


def _number(word: str) -> float | None:
    try:
        value = float(word)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _number_reply(value: float) -> str:
    return f"{value + 0.0:+.8E}"


def _overload(signal: float) -> str:
    return f"{'-' if signal < 0 else '+'}{_OVERLOAD_REPLY}"
