"""The Simulator: a software Meter that plugs in as a Transport."""

import os
import time
from collections import deque
from collections.abc import Callable

from agilent34401a.errors import TransportError, TransportTimeoutError

HEWLETT_PACKARD_IDENTITY = "HEWLETT-PACKARD,34401A,0,10-5-2"
AGILENT_IDENTITY = "Agilent Technologies,34401A,MY45000001,11-5-2"

TIME_SCALE_ENV_VAR = "AGILENT34401A_SIM_TIME_SCALE"
"""Overrides the default time scale: 1 is real time, 0 is instant."""

_LINE_FREQUENCY_HZ = 50
_OVERLOAD_REPLY = "9.90000000E+37"
_DC_VOLTAGE_FULL_SCALE = 120.0  # the highest DC voltage Range (100 V) with its 20 % over-range
_NO_ERROR = '+0,"No error"'
_UNDEFINED_HEADER = '-113,"Undefined header"'

# Until Integration Time and Autozero can be set, the Simulator measures like a freshly reset Meter.
_DEFAULT_NPLC = 10
_AUTOZERO_FACTOR = 2


class Simulator:
    """One simulated Meter, driven through the same text interface as a real one.

    `time_scale` stretches (or, at 0, removes) the time a Reading takes; it defaults to the
    `AGILENT34401A_SIM_TIME_SCALE` environment variable, or real time when that is unset.
    """

    def __init__(
        self,
        *,
        identity: str = HEWLETT_PACKARD_IDENTITY,
        dc_voltage: float = 1.0,
        time_scale: float | None = None,
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
        self._dc_voltage = dc_voltage
        self._sleep = sleep
        self._closed = False
        self._errors: deque[str] = deque()
        # Each pending reply carries the real-time seconds the Meter needs before it can send it.
        self._replies: deque[tuple[str, float]] = deque()

    def write(self, command: str) -> None:
        self._require_open()
        match command.strip().upper():
            case "*IDN?":
                self._replies.append((self._identity, 0.0))
            case "READ?":
                self._replies.append(
                    (self._measure_dc_voltage(), _DEFAULT_NPLC / _LINE_FREQUENCY_HZ * _AUTOZERO_FACTOR)
                )
            case "SYST:ERR?":
                self._replies.append((self._errors.popleft() if self._errors else _NO_ERROR, 0.0))
            case "*CLS":
                self._errors.clear()
            case "*RST":
                pass
            case _:
                self._errors.append(_UNDEFINED_HEADER)

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

    def _measure_dc_voltage(self) -> str:
        if abs(self._dc_voltage) > _DC_VOLTAGE_FULL_SCALE:
            return f"{'-' if self._dc_voltage < 0 else '+'}{_OVERLOAD_REPLY}"
        return f"{self._dc_voltage:+.8E}"
