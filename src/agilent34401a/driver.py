"""The SCPI driver: typed operations on a Meter, built on a Transport."""

import math
import re
from dataclasses import dataclass, replace

from agilent34401a.burst import Burst, BurstProgress
from agilent34401a.errors import (
    BurstRefusedError,
    CalibrationBlockedError,
    InvalidSetupError,
    MalformedReplyError,
    UnrecognisedIdentityError,
)
from agilent34401a.math_operations import (
    MathOperation,
    MathSettings,
    MeterStatistics,
    limit_result_from_status,
    limit_result_of,
)
from agilent34401a.meter import (
    NPLC_VALUES,
    AcFilter,
    Autozero,
    Function,
    GateTime,
    InputImpedance,
    Reading,
    Setup,
    Terminals,
    format_range,
    parse_reading,
)
from agilent34401a.raw_scpi import analyse
from agilent34401a.transport import BusLockout, Transport
from agilent34401a.trigger import TriggerSettings, TriggerSource

SELF_TEST_TIMEOUT_S = 30.0
"""The 34401A's self-test takes about ten seconds, so `*TST?` is given three times that to reply."""
DISPLAY_TEXT_LIMIT = 12
"""The most characters the Meter's display shows in a message."""
_ERROR_QUEUE_STATUS_BIT = 4  # bit 2 of the status byte: the error queue is not empty
_EVENT_SUMMARY_STATUS_BIT = 32  # bit 5 of the status byte: with *ESE 1, operation complete
_QUOTED = 2  # a quoted string is at least its two quotes
_INFINITE_COUNT = 9.9e37  # how the Meter says infinite when asked for its Trigger Count
STORE_LOCATIONS = range(1, 4)
"""The Meter Memory locations a Setup can be stored in (location 0 is the Meter's own power-down state)."""
RECALL_LOCATIONS = range(4)
"""The Meter Memory locations a Setup can be recalled from."""
_FETCH_BASE_TIMEOUT_S = 10.0
_FETCH_TIMEOUT_PER_READING_S = 0.05  # 512 Readings of about 16 characters take several seconds at 9600 baud

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


# What `CALC:FUNC?` may answer, short and long forms.
_MATH_ANSWERS = {
    "NULL": MathOperation.NULL,
    "DB": MathOperation.DB,
    "DBM": MathOperation.DBM,
    "AVER": MathOperation.STATISTICS,
    "AVERAGE": MathOperation.STATISTICS,
    "LIM": MathOperation.LIMIT_TEST,
    "LIMIT": MathOperation.LIMIT_TEST,
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
        self._terminals = Terminals.FRONT

    @property
    def setup(self) -> Setup:
        return self._setup

    @property
    def terminals(self) -> Terminals:
        """The Terminals the Meter last said were active; `read_terminals` asks again."""
        return self._terminals

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
        """Take one Reading in the Function of the current Setup, tagged with its Math Operation and Limit Test result.

        The Meter is first put back to one immediately triggered Reading if a Preset or a Burst left it otherwise,
        because `READ?` would wait for a bus trigger (and fail) or return several Readings. This is what Single and
        Continuous use. The Setup the driver remembers then has those trigger settings, so a caller that shows the Setup
        (the Worker) must report that change: compare `setup.trigger` before and after.

        With a Limit Test running, the Meter's Questionable Data register says how the Reading did, which costs one more
        query. A Reading under Null, dB or dBm is the Operation's result, not the measured value.
        """
        if not self._setup.trigger.is_single_immediate:
            plain = replace(self._setup.trigger, source=TriggerSource.IMMEDIATE, sample_count=1, trigger_count=1)
            self._write_trigger(plain)
            self._setup = self._setup.with_trigger(plain)
        reading = parse_reading(self._transport.query("READ?"), self._setup.function)
        operation = self._setup.math.operation
        if operation is None:
            return reading
        limit = None
        if operation is MathOperation.LIMIT_TEST:
            limit = limit_result_from_status(int(self._read_number("STAT:QUES:EVEN?")))
        return replace(reading, math=operation, limit=limit)

    def apply(self, setup: Setup) -> list[QueuedError]:
        """Send a Setup to the Meter, then drain its error queue (ADR-0005) and return what it complained about.

        Settings go in a fixed order: Function, then Range, then Integration Time or Gate Time (which also set the
        Resolution), then the AC Filter, Autozero and Input Impedance, then the trigger settings if they differ from the
        ones the driver knows the Meter has, and last the Math settings and Operation.
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
        if setup.gate_time is not None:
            write(f"{function.value}:APER {setup.gate_time.seconds:g}")
        if setup.ac_filter is not None:
            write(f"DET:BAND {setup.ac_filter.hertz}")
        if setup.autozero is not None:
            write(f"ZERO:AUTO {setup.autozero.value}")
        if setup.input_impedance is not None:
            write(f"INP:IMP:AUTO {'ON' if setup.input_impedance is InputImpedance.HIGH_IMPEDANCE else 'OFF'}")
        if setup.trigger != self._setup.trigger:  # the Meter's trigger settings are global, not per Function
            self._write_trigger(setup.trigger)
        if function.has_math and self._math_needs_sending(setup):
            self._apply_math(setup.math)
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

    def save_to_meter(self, location: int) -> list[QueuedError]:
        """Store the Meter's whole Setup in its own Meter Memory (`*SAV`), then drain the error queue (ADR-0005).

        `location` is 1 to 3, anything else raises `ValueError` before anything is sent. This overwrites what was
        there, so it is only ever done on the user's request (ADR-0004).
        """
        _check_location(location, STORE_LOCATIONS)
        self._transport.write(f"*SAV {location}")
        return self.drain_errors()

    def recall_from_meter(self, location: int) -> list[QueuedError]:
        """Replace the Meter's Setup with the one in Meter Memory `location` (`*RCL`), then drain the error queue.

        `location` is 0 (the power-down state) to 3, anything else raises `ValueError` before anything is sent. The
        Setup the driver remembers is no longer right afterwards, so the caller must `read_setup` (ADR-0004: read it
        back, never assume it).
        """
        _check_location(location, RECALL_LOCATIONS)
        self._transport.write(f"*RCL {location}")
        errors = self.drain_errors()
        self._setup = Setup.default(self._setup.function)  # a stand-in until read_setup, as after select_function
        return errors

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
        if function.has_gate_time:
            setup = setup.with_gate_time(self._read_gate_time(function))
        if function.has_ac_filter:
            setup = setup.with_ac_filter(self._read_ac_filter())
        if function.has_autozero:
            # The Meter never reports "once": it takes the one offset measurement and leaves Autozero off.
            autozero = Autozero.ON if self._read_flag("ZERO:AUTO?") else Autozero.OFF
            setup = setup.with_autozero(autozero)
        if function.has_input_impedance:
            impedance = InputImpedance.HIGH_IMPEDANCE if self._read_flag("INP:IMP:AUTO?") else InputImpedance.TEN_MEGOHM
            setup = setup.with_input_impedance(impedance)
        setup = setup.with_trigger(self.read_trigger())
        if function.has_math:
            setup = self._with_math_read(setup)
        self._setup = setup
        return setup

    def read_trigger(self) -> TriggerSettings:
        """Ask the Meter for its Trigger Source, Trigger Delay, Sample Count and Trigger Count. Changes nothing."""
        reply = self._transport.query("TRIG:SOUR?").strip().upper()
        source = next((source for source in TriggerSource if source.value == reply), None)
        if source is None:
            message = f"Meter replied {reply!r} to TRIG:SOUR?, which is not IMM, BUS or EXT"
            raise MalformedReplyError(message)
        delay = None if self._read_flag("TRIG:DEL:AUTO?") else self._read_number("TRIG:DEL?")
        samples = self._read_number("SAMP:COUN?")
        triggers = self._read_number("TRIG:COUN?")
        try:
            return TriggerSettings(
                source, delay, round(samples), None if triggers >= _INFINITE_COUNT else round(triggers)
            )
        except InvalidSetupError as error:
            message = f"Meter reported trigger settings it does not have: {error}"
            raise MalformedReplyError(message) from error

    def _write_trigger(self, trigger: TriggerSettings) -> None:
        for command in self._trigger_commands(trigger):
            self._transport.write(command)

    def start_burst(self, trigger: TriggerSettings) -> Burst:
        """Start a Burst: the Meter takes `trigger.readings` Readings into Reading Memory on its own.

        A Burst that does not fit in Reading Memory is refused with `BurstTooLargeError` before anything is sent. If
        the Meter complains (it is already waiting for a trigger, say) the trigger settings it had are put back and
        `BurstRefusedError` says what it complained of. Otherwise the Meter is flagged to report operation complete
        in its status byte, `INIT` has been sent, and the caller waits with `wait_for_burst` (a Burst that cannot
        take long) or `burst_progress` (polling, which can be abandoned), then `fetch_burst` and `finish_burst`.
        """
        trigger.check_fits_reading_memory()
        previous = self._setup.trigger
        self._write_trigger(trigger)
        errors = self.drain_errors()
        if not errors:
            for command in ("*CLS", "*ESE 1", "INIT", "*OPC"):
                self._transport.write(command)
            errors = self.drain_errors()
        if errors:
            self._write_trigger(previous)
            with_text = "; ".join(f"{error.code}: {error.message}" for error in errors)
            self.drain_errors()
            message = f"The Meter would not start the Burst ({with_text})"
            raise BurstRefusedError(message)
        return Burst(trigger, self._setup.with_trigger(trigger), previous)

    def wait_for_burst(self, burst: Burst, *, timeout: float | None = None) -> None:
        """Block until the Meter says the Burst is complete (`*OPC?`), for at most `timeout` seconds if given.

        Only for a Burst that needs no more than one bus trigger; the Transport's timeout is restored afterwards.
        """
        if burst.trigger.source is TriggerSource.BUS and burst.trigger.trigger_count != 1:
            message = "A bus-triggered Burst of several triggers must be polled with burst_progress"
            raise ValueError(message)
        previous = self._transport.timeout
        if timeout is not None:
            self._transport.timeout = timeout
        try:
            if burst.bus_trigger_due(0):
                self._send_bus_trigger(burst)
            self._read_number("*OPC?")
        finally:
            self._transport.timeout = previous

    def burst_progress(self, burst: Burst) -> BurstProgress:
        """Look at the Burst without waiting: send the next bus trigger if the Meter is ready, and report progress."""
        points = int(self._read_number("DATA:POIN?"))
        if burst.bus_trigger_due(points):
            self._send_bus_trigger(burst)
        complete = bool(self.status_byte() & _EVENT_SUMMARY_STATUS_BIT)
        if complete:
            points = int(self._read_number("DATA:POIN?"))  # the last Readings may have arrived since the first look
        return BurstProgress(points, complete=complete)

    def _send_bus_trigger(self, burst: Burst) -> None:
        self._transport.write("*TRG")
        burst.triggers_sent += 1

    def fetch_burst(self, burst: Burst) -> list[Reading]:
        """Read the Burst's Readings out of Reading Memory (they stay there until the next Burst).

        Under a Math Operation the Readings carry it, as those of `read` do. The Meter only says in its Questionable
        Data register that some Reading failed a Limit Test, not which, so each Reading is judged against the bounds
        the way the Meter does; the register is then read once to forget what the Burst latched in it.
        """
        previous = self._transport.timeout
        self._transport.timeout = max(
            previous, _FETCH_BASE_TIMEOUT_S + _FETCH_TIMEOUT_PER_READING_S * burst.expected_readings
        )
        try:
            reply = self._transport.query("FETC?")
        finally:
            self._transport.timeout = previous
        readings = [parse_reading(word.strip(), burst.setup.function) for word in reply.split(",")]
        operation = burst.setup.math.operation
        if operation is None:
            return readings
        settings = burst.setup.math
        if operation is MathOperation.LIMIT_TEST:
            self._transport.query("STAT:QUES:EVEN?")
            return [
                replace(
                    reading,
                    math=operation,
                    limit=limit_result_of(reading.value, settings.limit_lower, settings.limit_upper),
                )
                for reading in readings
            ]
        return [replace(reading, math=operation) for reading in readings]

    def finish_burst(self, burst: Burst) -> list[QueuedError]:
        """Put the trigger settings the Meter had before the Burst back, and return what its error queue held."""
        self._write_trigger(burst.previous)
        return self.drain_errors()

    def _trigger_commands(self, trigger: TriggerSettings) -> list[str]:
        return [
            f"TRIG:SOUR {trigger.source.value}",
            "TRIG:DEL:AUTO ON" if trigger.delay is None else f"TRIG:DEL {trigger.delay:g}",
            f"SAMP:COUN {trigger.sample_count}",
            "TRIG:COUN INF" if trigger.trigger_count is None else f"TRIG:COUN {trigger.trigger_count}",
        ]

    def _math_needs_sending(self, setup: Setup) -> bool:
        """Whether the Meter's Math differs from `setup`'s, as far as the driver knows.

        A change of Function turns the Operation off, so an Operation wanted in another Function must be sent again.
        Leaving the rest alone also keeps Statistics running while other settings are changed.
        """
        known = self._setup
        if setup.math != known.math:
            return True
        return setup.math.operation is not None and setup.function is not known.function

    def _apply_math(self, settings: MathSettings) -> None:
        """Send the settings of every Math Operation, then turn the one in effect on (or all of them off)."""
        write = self._transport.write
        write(f"CALC:NULL:OFFS {settings.null_offset:g}")
        write(f"CALC:DB:REF {settings.db_reference:g}")
        write(f"CALC:DBM:REF {settings.dbm_reference_resistance:g}")
        write(f"CALC:LIM:LOW {settings.limit_lower:g}")
        write(f"CALC:LIM:UPP {settings.limit_upper:g}")
        operation = settings.operation
        if operation is None:
            write("CALC:STAT OFF")
            return
        write(f"CALC:FUNC {operation.value}")
        write("CALC:STAT ON")
        if operation is MathOperation.LIMIT_TEST:
            self._transport.query("STAT:QUES:EVEN?")  # forget failures latched before this Limit Test began

    def _with_math_read(self, setup: Setup) -> Setup:
        """Ask the Meter which Math Operation it is doing and with what settings, and put that in `setup`."""
        operation = None
        if self._read_flag("CALC:STAT?"):
            answer = self._transport.query("CALC:FUNC?").strip().strip('"').upper()
            operation = _MATH_ANSWERS.get(answer)
            if operation is None:
                message = f"Meter replied {answer!r} to CALC:FUNC?, which is not a Math Operation"
                raise MalformedReplyError(message)
        try:
            settings = MathSettings(
                operation=operation,
                null_offset=self._read_number("CALC:NULL:OFFS?"),
                db_reference=self._read_number("CALC:DB:REF?"),
                dbm_reference_resistance=self._read_number("CALC:DBM:REF?"),
                limit_lower=self._read_number("CALC:LIM:LOW?"),
                limit_upper=self._read_number("CALC:LIM:UPP?"),
            )
            return setup.with_math(settings)
        except InvalidSetupError as error:
            message = f"Meter reported Math that {setup.function.label} cannot have: {error}"
            raise MalformedReplyError(message) from error

    def fetch_statistics(self) -> MeterStatistics:
        """Ask the Meter for the minimum, maximum, average and count of the Readings since Statistics was turned on."""
        return MeterStatistics(
            minimum=self._read_number("CALC:AVER:MIN?"),
            maximum=self._read_number("CALC:AVER:MAX?"),
            average=self._read_number("CALC:AVER:AVER?"),
            count=round(self._read_number("CALC:AVER:COUN?")),
        )

    def reset_statistics(self) -> list[QueuedError]:
        """Start the Meter's Statistics again by turning the Operation off and on, and drain its error queue.

        Raises `ValueError` unless Statistics is the Math Operation in effect.
        """
        if self._setup.math.operation is not MathOperation.STATISTICS:
            message = "Only Statistics can be reset, and it is not the Math Operation in effect"
            raise ValueError(message)
        self._transport.write("CALC:STAT OFF")
        self._transport.write("CALC:STAT ON")
        return self.drain_errors()

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

    def read_terminals(self) -> Terminals:
        """Ask the Meter which Terminals are active. Only the switch on its front panel can change that."""
        reply = self._transport.query("ROUT:TERM?").strip().upper()
        for terminals in Terminals:
            if reply == terminals.value:
                self._terminals = terminals
                return terminals
        message = f"Meter replied {reply!r} to ROUT:TERM?, which is not FRON or REAR"
        raise MalformedReplyError(message)

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

    def _read_gate_time(self, function: Function) -> GateTime:
        value = self._read_number(f"{function.value}:APER?")
        for candidate in GateTime:
            if math.isclose(value, candidate.seconds, rel_tol=1e-6):
                return candidate
        message = f"Meter reported a Gate Time of {value:g} s, which it does not have"
        raise MalformedReplyError(message)

    def _read_ac_filter(self) -> AcFilter:
        value = self._read_number("DET:BAND?")
        for candidate in AcFilter:
            if math.isclose(value, candidate.hertz, rel_tol=1e-6):
                return candidate
        message = f"Meter reported an AC Filter of {value:g} Hz, which it does not have"
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


def _check_location(location: int, allowed: range) -> None:
    if location not in allowed:
        message = f"Meter Memory location is {allowed.start} to {allowed[-1]}, not {location}"
        raise ValueError(message)


def _unquote(reply: str) -> str:
    """Strip the double quotes the Meter puts around a string reply, and undouble any quote inside it."""
    text = reply.strip()
    if len(text) >= _QUOTED and text[0] == text[-1] == '"':
        text = text[1:-1].replace('""', '"')
    return text
