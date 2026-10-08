"""The Simulator's trigger state machine and Reading Memory (part of the Meter model, ADR-0003).

`TriggerModel` holds the Trigger Source, Trigger Delay, Sample Count and Trigger Count, and runs a measurement the
way the 34401A does: `INIT` clears Reading Memory and makes the Meter wait for its trigger, each trigger takes
`Sample Count` Readings after the Trigger Delay, and the Meter is idle again once the Trigger Count is used up.
Readings reach Reading Memory as time passes (instantly when the Simulator's time scale is 0), and the oldest are
overwritten once the 512 it holds are full.
"""

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from agilent34401a.burst import trigger_delay_seconds
from agilent34401a.meter import Setup, measurement_time
from agilent34401a.trigger import MAX_COUNT, MAX_DELAY_S, READING_MEMORY_SIZE, TriggerSettings, TriggerSource

_ILLEGAL_PARAMETER = '-224,"Illegal parameter value"'
_DATA_OUT_OF_RANGE = '-222,"Data out of range"'
_MISSING_PARAMETER = '-109,"Missing parameter"'
_TRIGGER_IGNORED = '-211,"Trigger ignored"'
_INIT_IGNORED = '-213,"Init ignored"'
_TRIGGER_DEADLOCK = '-214,"Trigger deadlock"'
_DATA_STALE = '-230,"Data corrupt or stale"'
_INFINITE_REPLY = "+9.90000000E+37"
_OPERATION_COMPLETE = 1  # bit 0 of the standard event status register
_EVENT_SUMMARY_BIT = 32  # bit 5 of the status byte: an enabled standard event has happened
_SOURCES = {"IMM": TriggerSource.IMMEDIATE, "BUS": TriggerSource.BUS, "EXT": TriggerSource.EXTERNAL}
_LONG_WORDS = {"IMMEDIATE": "IMM", "EXTERNAL": "EXT", "MINIMUM": "MIN", "MAXIMUM": "MAX", "INFINITY": "INF"}


@dataclass(frozen=True)
class SimHost:
    """What the trigger model needs from the Simulator that owns it."""

    setup: Callable[[], Setup]
    """The Setup the Meter is in, without the trigger settings."""
    measure: Callable[[], str]
    """Take one Reading now and return the Meter's reply text for it."""
    skip: Callable[[float], None]
    """Let the Applied Signal move on by this many seconds without taking a Reading."""
    error: Callable[[str], None]
    reply: Callable[[str, float], None]
    time_scale: Callable[[], float]
    clock: Callable[[], float] = time.monotonic


def _number_reply(value: float) -> str:
    return f"{value + 0.0:+.8E}"


class TriggerModel:
    """The trigger settings, the state of the measurement they start, and Reading Memory."""

    def __init__(self, host: SimHost) -> None:
        self._host = host
        self.reset()

    def reset(self) -> None:
        """Back to the settings of a reset Meter: immediate, automatic delay, one Reading, idle, memory empty."""
        self.source = TriggerSource.IMMEDIATE
        self.delay_auto = True
        self.delay_s = 0.0
        self.sample_count = 1
        self.trigger_count: int | None = 1
        self.standard_event_enable = 0
        self.standard_events = 0
        self._operation_pending = False
        self._memory: deque[str] = deque(maxlen=READING_MEMORY_SIZE)
        self._active = False
        self._started_at = 0.0
        self._trigger_starts: list[float] = []  # in simulated seconds after INIT, for Bus and External triggers
        self._generated = 0

    @property
    def settings(self) -> TriggerSettings:
        return TriggerSettings(
            self.source, None if self.delay_auto else self.delay_s, self.sample_count, self.trigger_count
        )

    # --- commands ---------------------------------------------------------------------------------------------

    def common(self, header: str, argument: str) -> bool:
        """Handle `*TRG`, `*OPC`, `*OPC?`, `*ESE`, `*ESE?`, `*ESR?`; False when `header` is not one of them."""
        match header:
            case "*TRG":
                self._bus_trigger()
            case "*OPC":
                self._operation_pending = True
                self._sync()
            case "*OPC?":
                self._operation_complete_query()
            case "*ESE":
                value = _number(argument)
                if value is None:
                    self._host.error(_ILLEGAL_PARAMETER)
                else:
                    self.standard_event_enable = int(value) & 0xFF
            case "*ESE?":
                self._host.reply(str(self.standard_event_enable), 0.0)
            case "*ESR?":
                self._sync()
                self._host.reply(str(self.standard_events), 0.0)
                self.standard_events = 0
            case _:
                return False
        return True

    def status_bits(self) -> int:
        """Return the bits of the status byte that come from the trigger model."""
        self._sync()
        return _EVENT_SUMMARY_BIT if self.standard_events & self.standard_event_enable else 0

    def clear_status(self) -> None:
        self.standard_events = 0
        self._operation_pending = False

    def command(self, key: str, *, query: bool, argument: str) -> bool:
        """Handle the trigger commands and queries; False when `key` is not one of them."""
        handlers: dict[str, Callable[[], None]] = {
            "TRIG:SOUR": lambda: self._source_command(query=query, argument=argument),
            "TRIG:DEL": lambda: self._delay_command(query=query, argument=argument),
            "TRIG:DEL:AUTO": lambda: self._delay_auto_command(query=query, argument=argument),
            "SAMP:COUN": lambda: self._count_command("sample_count", query=query, argument=argument, infinite=False),
            "TRIG:COUN": lambda: self._count_command("trigger_count", query=query, argument=argument, infinite=True),
            "INIT": self._init,
            "FETC": lambda: self._fetch(erase=False),
            "R": lambda: self._fetch(erase=True),
            "DATA:POIN": self._points,
            "READ": self._read,
        }
        handler = handlers.get(key)
        if handler is None or (query and key == "INIT") or (not query and key in {"FETC", "R", "DATA:POIN", "READ"}):
            return False
        handler()
        return True

    def external_trigger(self) -> None:
        """Pulse the trigger input, which starts a trigger if the Meter is waiting for an external one."""
        if self.source is TriggerSource.EXTERNAL:
            self._accept_trigger(report=False)

    def abort(self) -> None:
        """Stop the measurement as a device clear does, keeping the Readings already in Reading Memory."""
        self._sync()
        self._active = False
        self._operation_pending = False

    # --- settings ---------------------------------------------------------------------------------------------

    def _word(self, argument: str) -> str:
        word = argument.strip().upper()
        return _LONG_WORDS.get(word, word)

    def _source_command(self, *, query: bool, argument: str) -> None:
        if query:
            self._host.reply(self.source.value, 0.0)
        elif not argument:
            self._host.error(_MISSING_PARAMETER)
        elif (source := _SOURCES.get(self._word(argument))) is None:
            self._host.error(_ILLEGAL_PARAMETER)
        else:
            self.source = source

    def _delay_command(self, *, query: bool, argument: str) -> None:
        if query:
            delay = trigger_delay_seconds(self._setup()) if self.delay_auto else self.delay_s
            self._host.reply(_number_reply(delay), 0.0)
            return
        word = self._word(argument)
        if not word:
            self._host.error(_MISSING_PARAMETER)
            return
        value = {"MIN": 0.0, "MAX": MAX_DELAY_S, "DEF": 0.0}.get(word, _number(word))
        if value is None:
            self._host.error(_ILLEGAL_PARAMETER)
        elif not 0 <= value <= MAX_DELAY_S:
            self._host.error(_DATA_OUT_OF_RANGE)
        else:
            self.delay_s = value
            self.delay_auto = False

    def _delay_auto_command(self, *, query: bool, argument: str) -> None:
        if query:
            self._host.reply("1" if self.delay_auto else "0", 0.0)
        elif self._word(argument) in {"ON", "1"}:
            self.delay_auto = True
        elif self._word(argument) in {"OFF", "0"}:
            self.delay_auto = False
            self.delay_s = trigger_delay_seconds(self._setup())
        else:
            self._host.error(_ILLEGAL_PARAMETER)

    def _count_command(self, attribute: str, *, query: bool, argument: str, infinite: bool) -> None:
        if query:
            value = getattr(self, attribute)
            self._host.reply(_INFINITE_REPLY if value is None else _number_reply(value), 0.0)
            return
        word = self._word(argument)
        if not word:
            self._host.error(_MISSING_PARAMETER)
        elif word == "INF" and infinite:
            setattr(self, attribute, None)
        elif (number := {"MIN": 1.0, "MAX": float(MAX_COUNT), "DEF": 1.0}.get(word, _number(word))) is None:
            self._host.error(_ILLEGAL_PARAMETER)
        elif not 1 <= round(number) <= MAX_COUNT:
            self._host.error(_DATA_OUT_OF_RANGE)
        else:
            setattr(self, attribute, round(number))

    def _setup(self) -> Setup:
        return self._host.setup().with_trigger(self.settings)

    # --- the measurement --------------------------------------------------------------------------------------

    def _timing(self) -> tuple[float, float, float]:
        """Return the Trigger Delay, the time one Reading takes and the time one whole trigger takes."""
        setup = self._setup()
        delay = trigger_delay_seconds(setup)
        reading = measurement_time(setup)
        return delay, reading, delay + self.sample_count * reading

    def _elapsed(self) -> float:
        """Return the simulated seconds since INIT; infinite when the time scale is 0, so that everything is already done."""
        scale = self._host.time_scale()
        return math.inf if scale == 0 else (self._host.clock() - self._started_at) / scale

    def _init(self) -> None:
        self._sync()
        if self._active:
            self._host.error(_INIT_IGNORED)
            return
        self._begin()

    def _begin(self) -> None:
        self._memory.clear()
        self._generated = 0
        self._trigger_starts = []
        self._active = True
        self._started_at = self._host.clock()

    def _bus_trigger(self) -> None:
        self._sync()
        if self.source is TriggerSource.BUS:
            self._accept_trigger(report=True)
        else:
            self._host.error(_TRIGGER_IGNORED)

    def _accept_trigger(self, *, report: bool) -> None:
        """Start the next trigger if the Meter is waiting for one; a Bus trigger that cannot start is an error."""
        self._sync()
        waiting = self._active and self.source is not TriggerSource.IMMEDIATE
        elapsed = self._elapsed()
        _delay, _reading, period = self._timing()
        busy = bool(self._trigger_starts) and elapsed < self._trigger_starts[-1] + period
        used_up = self.trigger_count is not None and len(self._trigger_starts) >= self.trigger_count
        if not waiting or busy or used_up:
            if report:
                self._host.error(_TRIGGER_IGNORED)
            return
        self._trigger_starts.append(0.0 if math.isinf(elapsed) else elapsed)
        self._sync()

    def _readings_done(self, elapsed: float) -> int:
        """How many Readings the measurement has taken after `elapsed` simulated seconds."""
        delay, reading, period = self._timing()
        per_trigger = self.sample_count

        def done_in(trigger_elapsed: float) -> int:
            if trigger_elapsed >= period:
                return per_trigger
            return max(0, min(per_trigger, math.floor((trigger_elapsed - delay) / reading)))

        if self.source is not TriggerSource.IMMEDIATE:
            return sum(done_in(elapsed - start) for start in self._trigger_starts)
        triggers = self.trigger_count
        if math.isinf(elapsed):
            # An infinite Trigger Count never finishes; on an instant Meter it fills Reading Memory and stays there.
            triggers = triggers if triggers is not None else math.ceil(READING_MEMORY_SIZE / per_trigger)
            return triggers * per_trigger
        whole = math.floor(elapsed / period)
        if triggers is not None and whole >= triggers:
            return triggers * per_trigger
        return whole * per_trigger + done_in(elapsed - whole * period)

    def _is_complete(self, done: int) -> bool:
        if self.trigger_count is None:
            return False
        if self.source is TriggerSource.IMMEDIATE:
            return done >= self.trigger_count * self.sample_count
        return len(self._trigger_starts) >= self.trigger_count and done >= self.trigger_count * self.sample_count

    def _sync(self) -> None:
        """Bring Reading Memory and the state up to date with the time that has passed."""
        if not self._active:
            if self._operation_pending:
                self._finish_operation()
            return
        done = self._readings_done(self._elapsed())
        missing = done - self._generated
        if missing > READING_MEMORY_SIZE:  # only the last 512 can still be in memory, so do not take the rest
            skipped = missing - READING_MEMORY_SIZE
            self._host.skip(skipped * self._timing()[1])
            self._generated += skipped
            missing = READING_MEMORY_SIZE
        for _ in range(missing):
            self._memory.append(self._host.measure())
        self._generated = done
        if self._is_complete(done):
            self._active = False
            if self._operation_pending:
                self._finish_operation()

    def _finish_operation(self) -> None:
        self._operation_pending = False
        self.standard_events |= _OPERATION_COMPLETE

    def _remaining(self) -> float | None:
        """Return the simulated seconds until the measurement is complete, or None while it waits for a trigger."""
        if not self._active:
            return 0.0
        if self.trigger_count is None:
            return None
        _delay, _reading, period = self._timing()
        if self.source is TriggerSource.IMMEDIATE:
            return self.trigger_count * period - self._elapsed()
        if len(self._trigger_starts) < self.trigger_count:
            return None
        return self._trigger_starts[-1] + period - self._elapsed()

    def _operation_complete_query(self) -> None:
        self._sync()
        remaining = self._remaining()
        if remaining is not None:
            self._host.reply("1", max(0.0, remaining))

    # --- Reading Memory ---------------------------------------------------------------------------------------

    def _points(self) -> None:
        self._sync()
        self._host.reply(_number_reply(len(self._memory)), 0.0)

    def _fetch(self, *, erase: bool) -> None:
        self._sync()
        if not self._memory:
            self._host.error(_DATA_STALE)
            return
        self._host.reply(",".join(self._memory), 0.0)
        if erase:
            self._memory.clear()

    def _read(self) -> None:
        """`READ?`: start a measurement and send its Readings straight to the output, not leaving them in memory."""
        self._sync()
        if self._active:
            self._host.error(_INIT_IGNORED)
            return
        if self.source is TriggerSource.BUS:
            self._host.error(_TRIGGER_DEADLOCK)  # the Meter would wait for a *TRG that cannot be sent
            return
        if self.source is TriggerSource.EXTERNAL:
            self._begin()  # waits for the trigger input, so there is no reply for the caller to read
            return
        triggers = self.trigger_count if self.trigger_count is not None else 1
        _delay, _reading, period = self._timing()
        readings = [self._host.measure() for _ in range(min(MAX_COUNT, triggers * self.sample_count))]
        self._memory.clear()
        self._host.reply(",".join(readings), triggers * period)


def _number(word: str) -> float | None:
    try:
        value = float(word)
    except ValueError:
        return None
    return value if math.isfinite(value) else None
