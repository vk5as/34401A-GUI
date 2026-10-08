"""A simulated RS-232 line to the Simulator: the Meter answers only when the port is set up the way it is.

A real serial port cannot be opened in a test and the Simulator is in-process (or reached over TCP), so this is the
seam for RS-232. `SimulatedSerialMeter.open` has the shape of the connection opener's `open_serial` and returns a
Transport. What the Meter does depends on how the port was set up:

- the wrong baud rate, or the wrong Flow Control, and it hears nothing: the Transport times out;
- the right baud rate but another Framing, and what comes back is garbage;
- the right settings, and it is the Simulator.

Probe is tested against it, and so is anything else that needs an RS-232 Connection without a cable.
"""

from collections import deque

from agilent34401a.errors import TransportError, TransportTimeoutError
from agilent34401a.serial_config import FlowControl, Framing, SerialSettings
from agilent34401a.sim import Simulator

_GARBAGE = "\x00\x7f\x1f\x80"  # what a UART that is listening with the wrong Framing makes of the Meter's reply


class SimulatedSerialMeter:
    """One Simulator behind an RS-232 line that works at one baud rate, Framing and Flow Control."""

    def __init__(
        self,
        simulator: Simulator | None = None,
        *,
        baud: int = 9600,
        framing: Framing | None = None,
        flow_control: FlowControl = FlowControl.NONE,
        missing_ports: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self.simulator = simulator if simulator is not None else Simulator()
        self.baud = baud
        self.framing = framing if framing is not None else Framing()
        self.flow_control = flow_control
        self.missing_ports = set(missing_ports)
        self.opened: list[SerialSettings] = []
        """Every port opened so far, in order, as the settings it was opened with."""

    def open(self, settings: SerialSettings) -> "SimulatedSerialPort":
        """Open the line the way a real port would be: a port that does not exist cannot be."""
        self.opened.append(settings)
        if settings.port in self.missing_ports:
            message = f"Could not open {settings.port}: no such serial port"
            raise TransportError(message)
        return SimulatedSerialPort(self, settings)


class SimulatedSerialPort:
    """A Transport over a simulated RS-232 line; see the module docstring for what the Meter hears and says."""

    def __init__(self, line: SimulatedSerialMeter, settings: SerialSettings) -> None:
        self._line = line
        self._settings = settings
        self._garbage: deque[str] = deque()
        self._closed = False
        self.timeout = line.simulator.timeout

    @property
    def _listening(self) -> bool:
        line, settings = self._line, self._settings
        return settings.baud == line.baud and settings.flow_control is line.flow_control

    @property
    def _understood(self) -> bool:
        return self._listening and self._settings.framing == self._line.framing

    def write(self, command: str) -> None:
        self._require_open()
        if self._understood:
            self._line.simulator.timeout = self.timeout
            self._line.simulator.write(command)
        elif self._listening and command.rstrip().endswith("?"):
            self._garbage.append(_GARBAGE)

    def read(self) -> str:
        self._require_open()
        if self._understood:
            self._line.simulator.timeout = self.timeout
            return self._line.simulator.read()
        if self._garbage:
            return self._garbage.popleft()
        message = f"No reply from the Meter on {self._settings.describe()}"
        raise TransportTimeoutError(message)

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self._require_open()
        self._garbage.clear()
        if self._understood:
            self._line.simulator.clear()

    def go_to_remote(self) -> None:
        self.write("SYST:REM")

    def go_to_local(self) -> None:
        self.write("SYST:LOC")

    def close(self) -> None:
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            message = "The simulated serial port is closed"
            raise TransportError(message)
