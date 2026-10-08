"""The Simulator's Math unit: the Meter's CALCulate subsystem and the Questionable Data status that reports a Limit Test.

The Simulator owns one `MathUnit`. It hands it the `CALC:*` and `STAT:QUES:*` commands, lets it process every Reading,
and asks it for its share of the status byte. The Meter does one Math Operation at a time (`CALC:FUNC` chooses it,
`CALC:STAT` turns it on or off), and, as modelled here, turns it off when the measuring Function changes or on a reset.
Nothing here is specific to the Transport.
"""

import math
from collections.abc import Callable
from functools import partial

from agilent34401a.math_operations import (
    DEFAULT_DBM_RESISTANCE,
    LIMIT_HIGH_BIT,
    LIMIT_LOW_BIT,
    MAX_DB_REFERENCE,
    MAX_DBM_RESISTANCE,
    MIN_DBM_RESISTANCE,
    MathOperation,
)
from agilent34401a.meter import Function

_OVERLOAD_MAGNITUDE = 9.9e37
_MILLIWATT = 1e-3
_DECIBEL_LIMIT = 200.0
_QUESTIONABLE_SUMMARY_BIT = 8  # bit 3 of the status byte: the Questionable Data register has an enabled bit set
_MAX_STATUS_REGISTER = 32767

_MISSING_PARAMETER = '-109,"Missing parameter"'
_ILLEGAL_PARAMETER = '-224,"Illegal parameter value"'
_DATA_OUT_OF_RANGE = '-222,"Data out of range"'
_SETTINGS_CONFLICT = '-221,"Settings conflict"'

# What CALC:FUNC accepts, with the long forms; the Meter answers with the short one.
_OPERATIONS = {
    "NULL": MathOperation.NULL,
    "DB": MathOperation.DB,
    "DBM": MathOperation.DBM,
    "AVER": MathOperation.STATISTICS,
    "AVERAGE": MathOperation.STATISTICS,
    "LIM": MathOperation.LIMIT_TEST,
    "LIMIT": MathOperation.LIMIT_TEST,
}
_STATISTICS_QUERIES = ("CALC:AVER:MIN", "CALC:AVER:MAX", "CALC:AVER:AVER", "CALC:AVER:COUN")


def _number(word: str) -> float | None:
    try:
        value = float(word)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _number_reply(value: float) -> str:
    return f"{value + 0.0:+.8E}"


def _applies(operation: MathOperation, function: Function) -> bool:
    return function.has_math and (function.has_decibels or not operation.in_decibels)


class MathUnit:
    """The state of the Meter's Math: which Operation, whether it is on, its settings, Statistics and status."""

    def __init__(self, reply: Callable[[str], None], error: Callable[[str], None]) -> None:
        self._reply = reply
        self._error = error
        self._questionable_enable = 0  # the Meter's enable registers survive a reset
        self._questionable_event = 0
        self.reset()

    def reset(self) -> None:
        """Go back to how a reset Meter has its Math: off, with the settings at their defaults."""
        self._operation = MathOperation.NULL
        self._on = False
        self._null_offset: float | None = None  # None: the first Reading taken with Null on becomes the offset
        self._db_reference = 0.0
        self._dbm_resistance = DEFAULT_DBM_RESISTANCE
        self._limit_lower = 0.0
        self._limit_upper = 0.0
        self._questionable_condition = 0
        self._clear_statistics()

    def function_changed(self) -> None:
        """Note that the measuring Function changed, which turns the Math Operation off."""
        self._turn_off()

    def clear_status(self) -> None:
        """`*CLS`: forget the latched Limit Test failures."""
        self._questionable_event = 0

    @property
    def status_byte_bits(self) -> int:
        """The Questionable Data summary bit of the status byte, set when an enabled failure is latched."""
        return _QUESTIONABLE_SUMMARY_BIT if self._questionable_event & self._questionable_enable else 0

    def handles(self, key: str) -> bool:
        return key.startswith(("CALC", "STAT:QUES"))

    def command(self, key: str, *, query: bool, argument: str, function: Function) -> bool:
        """Do the command `key` names; return False when it is not one the Math unit has."""
        word = argument.upper()
        handlers: dict[str, Callable[[], None]] = {
            "CALC:FUNC": lambda: self._function_command(query=query, word=word, function=function),
            "CALC:STAT": lambda: self._state_command(query=query, word=word, function=function),
            "CALC:NULL:OFFS": lambda: self._null_offset_command(query=query, word=word),
            "CALC:DB:REF": lambda: self._db_reference_command(query=query, word=word),
            "CALC:DBM:REF": lambda: self._dbm_reference_command(query=query, word=word),
            "CALC:LIM:LOW": lambda: self._limit_command("_limit_lower", query=query, word=word),
            "CALC:LIM:UPP": lambda: self._limit_command("_limit_upper", query=query, word=word),
            "STAT:QUES:ENAB": lambda: self._enable_command(query=query, word=word),
        }
        queries: dict[str, Callable[[], str]] = {
            "STAT:QUES:COND": lambda: f"+{self._questionable_condition}",
            "STAT:QUES:EVEN": self._take_questionable_events,
            **{statistic: partial(self._statistics_reply, statistic) for statistic in _STATISTICS_QUERIES},
        }
        if key in handlers:
            handlers[key]()
        elif query and key in queries:
            self._reply(queries[key]())
        else:
            return False
        return True

    def _take_questionable_events(self) -> str:
        events, self._questionable_event = self._questionable_event, 0
        return f"+{events}"

    def process(self, value: float) -> float:
        """Apply the Operation in effect to a Reading the Meter measured, and return what it sends."""
        if not self._on:
            return value
        overload = abs(value) >= _OVERLOAD_MAGNITUDE
        match self._operation:
            case MathOperation.NULL if not overload:
                if self._null_offset is None:
                    self._null_offset = value
                return value - self._null_offset
            case MathOperation.DBM if not overload:
                return self._decibels(value)
            case MathOperation.DB if not overload:
                return max(-_DECIBEL_LIMIT, min(_DECIBEL_LIMIT, self._decibels(value) - self._db_reference))
            case MathOperation.STATISTICS if not overload:
                self._add_to_statistics(value)
            case MathOperation.LIMIT_TEST:
                self._test_limits(value)
        return value

    def _decibels(self, volts: float) -> float:
        power = volts**2 / self._dbm_resistance
        dbm = 10 * math.log10(power / _MILLIWATT) if power > 0 else -_DECIBEL_LIMIT
        return max(-_DECIBEL_LIMIT, min(_DECIBEL_LIMIT, dbm))

    def _test_limits(self, value: float) -> None:
        bits = 0
        if value > self._limit_upper:
            bits = LIMIT_HIGH_BIT
        elif value < self._limit_lower:
            bits = LIMIT_LOW_BIT
        self._questionable_condition = bits
        self._questionable_event |= bits

    def _turn_off(self) -> None:
        self._on = False
        self._questionable_condition = 0

    def _clear_statistics(self) -> None:
        self._count = 0
        self._minimum = 0.0
        self._maximum = 0.0
        self._sum = 0.0

    def _add_to_statistics(self, value: float) -> None:
        if self._count == 0:
            self._minimum = self._maximum = value
        else:
            self._minimum, self._maximum = min(self._minimum, value), max(self._maximum, value)
        self._sum += value
        self._count += 1

    def _statistics_reply(self, key: str) -> str:
        average = self._sum / self._count if self._count else 0.0
        values = {
            "CALC:AVER:MIN": self._minimum,
            "CALC:AVER:MAX": self._maximum,
            "CALC:AVER:AVER": average,
            "CALC:AVER:COUN": float(self._count),
        }
        return _number_reply(values[key])

    def _function_command(self, *, query: bool, word: str, function: Function) -> None:
        if query:
            self._reply(self._operation.value)
        elif not word:
            self._error(_MISSING_PARAMETER)
        elif (operation := _OPERATIONS.get(word)) is None:
            self._error(_ILLEGAL_PARAMETER)
        elif self._on and not _applies(operation, function):
            self._error(_SETTINGS_CONFLICT)
        else:
            if self._on and operation is not self._operation:
                self._questionable_condition = 0
                if operation is MathOperation.STATISTICS:
                    self._clear_statistics()
            self._operation = operation

    def _state_command(self, *, query: bool, word: str, function: Function) -> None:
        if query:
            self._reply("1" if self._on else "0")
        elif word in ("ON", "1"):
            if self._on:
                return
            if not _applies(self._operation, function):
                self._error(_SETTINGS_CONFLICT)
                return
            self._on = True
            self._clear_statistics()
        elif word in ("OFF", "0"):
            self._turn_off()
        else:
            self._error(_ILLEGAL_PARAMETER)

    def _null_offset_command(self, *, query: bool, word: str) -> None:
        if query:
            self._reply(_number_reply(self._null_offset or 0.0))
        elif (value := _number(word)) is None:
            self._error(_MISSING_PARAMETER if not word else _ILLEGAL_PARAMETER)
        else:
            self._null_offset = value

    def _db_reference_command(self, *, query: bool, word: str) -> None:
        if query:
            self._reply(_number_reply(self._db_reference))
            return
        value = self._choose(word, minimum=-MAX_DB_REFERENCE, maximum=MAX_DB_REFERENCE, default=0.0)
        if value is not None:
            self._db_reference = value

    def _dbm_reference_command(self, *, query: bool, word: str) -> None:
        if query:
            self._reply(_number_reply(self._dbm_resistance))
            return
        value = self._choose(
            word, minimum=MIN_DBM_RESISTANCE, maximum=MAX_DBM_RESISTANCE, default=DEFAULT_DBM_RESISTANCE
        )
        if value is not None:
            self._dbm_resistance = value

    def _choose(self, word: str, *, minimum: float, maximum: float, default: float) -> float | None:
        """Pick a setting from a number or MIN, MAX or DEF, reporting what is wrong when it cannot be."""
        if not word:
            self._error(_MISSING_PARAMETER)
            return None
        value = {"MIN": minimum, "MAX": maximum, "DEF": default}.get(word, _number(word))
        if value is None:
            self._error(_ILLEGAL_PARAMETER)
        elif not minimum <= value <= maximum:
            self._error(_DATA_OUT_OF_RANGE)
        else:
            return value
        return None

    def _limit_command(self, attribute: str, *, query: bool, word: str) -> None:
        if query:
            self._reply(_number_reply(getattr(self, attribute)))
        elif (value := _number(word)) is None:
            self._error(_MISSING_PARAMETER if not word else _ILLEGAL_PARAMETER)
        else:
            setattr(self, attribute, value)

    def _enable_command(self, *, query: bool, word: str) -> None:
        if query:
            self._reply(f"+{self._questionable_enable}")
            return
        value = _number(word)
        if value is None:
            self._error(_ILLEGAL_PARAMETER)
        elif not 0 <= value <= _MAX_STATUS_REGISTER:
            self._error(_DATA_OUT_OF_RANGE)
        else:
            self._questionable_enable = int(value)
