"""The SCPI driver: typed operations on a Meter, built on a Transport."""

import math
import re
from dataclasses import dataclass

from agilent34401a.errors import MalformedReplyError, UnrecognisedIdentityError
from agilent34401a.meter import NPLC_VALUES, Function, Reading, Setup, format_range, parse_reading
from agilent34401a.transport import Transport

# Early firmware reports HEWLETT-PACKARD, later firmware Agilent Technologies; both are the same Meter.
_MANUFACTURERS = frozenset({"hewlett-packard", "agilent technologies"})
_MODEL = "34401a"
_IDENTITY_FIELDS = 4

# The Meter's error queue holds at most 20 entries, so draining never needs more reads than that.
_ERROR_QUEUE_SIZE = 20
_ERROR_ENTRY = re.compile(r'\s*([+-]?\d+)\s*,\s*"(.*)"\s*')

# The SCPI header each Function's Range and Integration Time commands start with. DC voltage ratio shares DC
# voltage's, and frequency and period are ranged by their input voltage.
_SETTING_PREFIX = {
    Function.DC_VOLTAGE: "VOLT:DC",
    Function.AC_VOLTAGE: "VOLT:AC",
    Function.DC_CURRENT: "CURR:DC",
    Function.AC_CURRENT: "CURR:AC",
    Function.RESISTANCE_2W: "RES",
    Function.RESISTANCE_4W: "FRES",
    Function.FREQUENCY: "FREQ:VOLT",
    Function.PERIOD: "PER:VOLT",
    Function.DC_VOLTAGE_RATIO: "VOLT:DC",
}
# What `FUNC?` may answer; the Meter says "VOLT" for DC voltage, and the long forms are accepted too.
_FUNCTION_ANSWERS = {
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


@dataclass(frozen=True)
class Identity:
    """What the Meter answered to `*IDN?`."""

    manufacturer: str
    model: str
    serial: str
    firmware: str
    raw: str


@dataclass(frozen=True)
class QueuedError:
    """One entry from the Meter's error queue."""

    code: int
    message: str


class Driver:
    """Talks SCPI to a 34401A over a Transport. It knows nothing about threads or the UI.

    The driver remembers the Setup it last applied or read back, because that decides which Function a Reading
    belongs to. A new driver assumes a freshly reset Meter; call `read_setup` to learn what it is really doing.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._setup = Setup.default(Function.DC_VOLTAGE)

    @property
    def setup(self) -> Setup:
        return self._setup

    def identify(self) -> Identity:
        """Ask the Meter who it is, rejecting anything that is not a 34401A."""
        raw = self._transport.query("*IDN?")
        fields = [field.strip() for field in raw.split(",")]
        if (
            len(fields) != _IDENTITY_FIELDS
            or fields[0].casefold() not in _MANUFACTURERS
            or fields[1].casefold() != _MODEL
        ):
            message = f"Expected an Agilent/HP 34401A but the device identified as {raw.strip()!r}"
            raise UnrecognisedIdentityError(message)
        manufacturer, model, serial, firmware = fields
        return Identity(manufacturer=manufacturer, model=model, serial=serial, firmware=firmware, raw=raw)

    def read(self) -> Reading:
        """Take one Reading in the Function of the current Setup."""
        return parse_reading(self._transport.query("READ?"), self._setup.function)

    def apply(self, setup: Setup) -> list[QueuedError]:
        """Send a Setup to the Meter, then drain its error queue (ADR-0005) and return what it complained about.

        Settings go in a fixed order: Function, then Range, then Integration Time, which also sets Resolution.
        A non-empty result means the Meter may not be in `setup`; `read_setup` says what it is in.
        """
        function = setup.function
        write = self._transport.write
        write(f'FUNC "{function.value}"')
        if function.ranges:
            prefix = _SETTING_PREFIX[function]
            write(f"{prefix}:RANG:AUTO ON" if setup.range is None else f"{prefix}:RANG {setup.range:g}")
        if setup.nplc is not None:
            write(f"{_SETTING_PREFIX[function]}:NPLC {setup.nplc:g}")
        self._setup = setup
        return self.drain_errors()

    def select_function(self, function: Function) -> list[QueuedError]:
        """Switch the Meter to `function`, leaving that Function's own Range and Integration Time as they were.

        The Meter remembers its settings per Function, so switching must not rewrite them (ADR-0004). Call
        `read_setup` afterwards to learn what they are.
        """
        self._transport.write(f'FUNC "{function.value}"')
        self._setup = Setup.default(function)  # a stand-in until read_setup, so Readings carry the right Function
        return self.drain_errors()

    def read_setup(self) -> Setup:
        """Ask the Meter what it is measuring, without changing anything (ADR-0004)."""
        answer = self._transport.query("FUNC?")
        function = _FUNCTION_ANSWERS.get(answer.strip().strip('"').upper())
        if function is None:
            message = f"Meter replied {answer!r} to FUNC?, which is not a Function"
            raise MalformedReplyError(message)
        setup = Setup.default(function)
        if function.ranges:
            setup = setup.with_range(self._read_range(function))
        if function.has_integration_time:
            setup = setup.with_nplc(self._read_nplc(function))
        self._setup = setup
        return setup

    def drain_errors(self) -> list[QueuedError]:
        """Read the Meter's error queue until it is empty."""
        errors: list[QueuedError] = []
        for _ in range(_ERROR_QUEUE_SIZE):
            reply = self._transport.query("SYST:ERR?")
            match = _ERROR_ENTRY.fullmatch(reply)
            if match is None:
                message = f"Meter replied {reply!r} to SYST:ERR?, which is not an error entry"
                raise MalformedReplyError(message)
            code = int(match.group(1))
            if code == 0:
                break
            errors.append(QueuedError(code, match.group(2)))
        return errors

    def _read_range(self, function: Function) -> float | None:
        prefix = _SETTING_PREFIX[function]
        auto = self._transport.query(f"{prefix}:RANG:AUTO?").strip().upper()
        if auto in ("1", "ON"):
            return None
        if auto not in ("0", "OFF"):
            message = f"Meter replied {auto!r} to {prefix}:RANG:AUTO?, which is not 0 or 1"
            raise MalformedReplyError(message)
        value = self._read_number(f"{prefix}:RANG?")
        for candidate in function.ranges:
            if math.isclose(value, candidate, rel_tol=1e-6):
                return candidate
        offered = ", ".join(format_range(function, candidate) for candidate in function.ranges)
        message = f"Meter reported a Range of {value:g}, but {function.label} has {offered}"
        raise MalformedReplyError(message)

    def _read_nplc(self, function: Function) -> float:
        value = self._read_number(f"{_SETTING_PREFIX[function]}:NPLC?")
        for candidate in NPLC_VALUES:
            if math.isclose(value, candidate, rel_tol=1e-6):
                return candidate
        message = f"Meter reported an Integration Time of {value:g} NPLC, which it does not have"
        raise MalformedReplyError(message)

    def _read_number(self, query: str) -> float:
        reply = self._transport.query(query)
        try:
            value = float(reply)
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            message = f"Meter replied {reply!r} to {query}, which is not a number"
            raise MalformedReplyError(message)
        return value
