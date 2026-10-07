"""The SCPI driver: typed operations on a Meter, built on a Transport."""

import math
import re
from dataclasses import dataclass

from agilent34401a.errors import CalibrationBlockedError, MalformedReplyError, UnrecognisedIdentityError
from agilent34401a.meter import NPLC_VALUES, Function, Reading, Setup, format_range, parse_reading
from agilent34401a.raw_scpi import analyse
from agilent34401a.transport import BusLockout, Transport

SELF_TEST_TIMEOUT_S = 30.0
"""The 34401A's self-test takes about ten seconds, so `*TST?` is given three times that to reply."""
DISPLAY_TEXT_LIMIT = 12
"""The most characters the Meter's display shows in a message."""
_ERROR_QUEUE_STATUS_BIT = 4  # bit 2 of the status byte: the error queue is not empty
_QUOTED = 2  # a quoted string is at least its two quotes

# Early firmware reports HEWLETT-PACKARD, later firmware Agilent Technologies; both are the same Meter.
_MANUFACTURERS = frozenset({"hewlett-packard", "agilent technologies"})
_MODEL = "34401a"
_IDENTITY_FIELDS = 4

# The Meter's error queue holds at most 20 entries, so draining never needs more reads than that.
_ERROR_QUEUE_SIZE = 20
_LINE_BREAK = re.compile(r"[\r\n]")
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


@dataclass(frozen=True)
class SystemInfo:
    """What the System tab shows besides the Meter's identity. The calibration items are read-only (ADR-0006)."""

    scpi_version: str
    calibration_count: int
    calibration_message: str
    beeper_enabled: bool
    display_on: bool
    display_text: str


@dataclass(frozen=True)
class RawResult:
    """What came of a raw command: the Meter's reply to it (None if it was not a query) and what its error queue held."""

    reply: str | None
    errors: tuple[QueuedError, ...]
    changes_meter: bool
    """The command was not a pure query, so the Meter's Setup may no longer be the one the driver remembers."""


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

    def send_raw(self, command: str, *, allow_calibration: bool = False) -> RawResult:
        """Send `command` as typed, which may be several commands joined with `;`, and return the Meter's reply.

        Calibration writes are refused with `CalibrationBlockedError`, before anything is sent, unless
        `allow_calibration` (ADR-0006). A command that is not a pure query is followed by a check of the error
        queue (ADR-0005). The driver does not re-read the Setup; the caller does that when `changes_meter` is set.
        """
        if _LINE_BREAK.search(command):
            message = "A raw command must be one line"
            raise ValueError(message)
        analysis = analyse(command)
        if analysis.is_empty:
            message = "Nothing to send"
            raise ValueError(message)
        if analysis.writes_calibration and not allow_calibration:
            message = "This would change the Meter's calibration, so it was not sent"
            raise CalibrationBlockedError(message)
        command = command.strip()
        reply: str | None = None
        if analysis.has_reply:
            reply = self._transport.query(command)
        else:
            self._transport.write(command)
        errors = tuple(self.drain_errors()) if analysis.changes_meter else ()
        return RawResult(reply, errors, analysis.changes_meter)

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

    def read_system(self) -> SystemInfo:
        """Read the SCPI version, calibration count and message, beeper and display state. Changes nothing."""
        query = self._transport.query
        return SystemInfo(
            scpi_version=query("SYST:VERS?").strip(),
            calibration_count=int(self._read_number("CAL:COUN?")),
            calibration_message=_unquote(query("CAL:STR?")),
            beeper_enabled=self._read_flag("SYST:BEEP:STAT?"),
            display_on=self._read_flag("DISP?"),
            display_text=_unquote(query("DISP:TEXT?")),
        )

    def reset(self) -> list[QueuedError]:
        """Send `*RST`. This is the only place the application ever resets the Meter (ADR-0004).

        The driver then expects a freshly reset Meter; call `read_setup` to learn what it is in.
        """
        self._transport.write("*RST")
        self._setup = Setup.default(Function.DC_VOLTAGE)
        return self.drain_errors()

    def self_test(self) -> bool:
        """Run the Meter's self-test (about ten seconds) and return whether it passed.

        The Transport's timeout is raised for the test and put back afterwards, even when the test fails.
        """
        previous = self._transport.timeout
        self._transport.timeout = max(previous, SELF_TEST_TIMEOUT_S)
        try:
            reply = self._transport.query("*TST?")
        finally:
            self._transport.timeout = previous
        result = reply.strip().lstrip("+")
        if result not in ("0", "1"):
            message = f"Meter replied {reply!r} to *TST?, which is not 0 or 1"
            raise MalformedReplyError(message)
        return result == "0"

    def status_byte(self) -> int:
        """Read the Meter's status byte."""
        return int(self._read_number("*STB?"))

    def errors_if_flagged(self) -> list[QueuedError]:
        """Drain the error queue only if the status byte says it is not empty (ADR-0005), else return nothing."""
        if self.status_byte() & _ERROR_QUEUE_STATUS_BIT:
            return self.drain_errors()
        return []

    def beep(self) -> list[QueuedError]:
        """Sound the beeper once."""
        self._transport.write("SYST:BEEP")
        return self.drain_errors()

    def set_beeper(self, *, enabled: bool) -> list[QueuedError]:
        """Turn the beeper that sounds on errors and limit hits on or off."""
        self._transport.write(f"SYST:BEEP:STAT {'ON' if enabled else 'OFF'}")
        return self.drain_errors()

    def set_display_text(self, text: str | None) -> list[QueuedError]:
        """Show `text` on the Meter's display, or give the display back to the Meter when `text` is None."""
        if text is None:
            self._transport.write("DISP:TEXT:CLE")
        else:
            if len(text) > DISPLAY_TEXT_LIMIT:
                message = f"The display shows at most {DISPLAY_TEXT_LIMIT} characters, got {len(text)}"
                raise ValueError(message)
            if not text.isascii() or not text.isprintable():
                message = "The display can only show printable ASCII characters"
                raise ValueError(message)
            self._transport.write(f'DISP:TEXT "{text.replace(chr(34), chr(34) * 2)}"')
        return self.drain_errors()

    def set_display(self, *, on: bool) -> list[QueuedError]:
        """Turn the Meter's display on or off; Readings are still taken while it is off."""
        self._transport.write(f"DISP {'ON' if on else 'OFF'}")
        return self.drain_errors()

    def lock_front_panel(self) -> list[QueuedError]:
        """Start a Lockout: Remote with the front panel's Local key disabled."""
        return self._set_lockout(locked=True)

    def unlock_front_panel(self) -> list[QueuedError]:
        """End a Lockout, leaving the Meter in Remote with its Local key working again."""
        return self._set_lockout(locked=False)

    def _set_lockout(self, *, locked: bool) -> list[QueuedError]:
        transport = self._transport
        if not (isinstance(transport, BusLockout) and transport.set_local_lockout(locked=locked)):
            if locked:
                transport.write("SYST:RWL")
            else:
                transport.write("SYST:LOC")
                transport.write("SYST:REM")
        return self.drain_errors()

    def _read_flag(self, query: str) -> bool:
        reply = self._transport.query(query).strip().upper()
        if reply not in ("0", "1", "ON", "OFF"):
            message = f"Meter replied {reply!r} to {query}, which is not 0 or 1"
            raise MalformedReplyError(message)
        return reply in ("1", "ON")

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


def _unquote(reply: str) -> str:
    """Strip the double quotes the Meter puts around a string reply, and undouble any quote inside it."""
    text = reply.strip()
    if len(text) >= _QUOTED and text[0] == text[-1] == '"':
        text = text[1:-1].replace('""', '"')
    return text
