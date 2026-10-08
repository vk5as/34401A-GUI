"""The typed boundary around pyvisa (ADR-0007): nothing else in the package imports it."""

import contextlib
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

import pyvisa
from pyvisa import constants
from pyvisa.errors import VisaIOError
from pyvisa.resources import MessageBasedResource, SerialInstrument

from agilent34401a.backend import BackendStatus
from agilent34401a.errors import BackendUnavailableError, TransportError, TransportTimeoutError
from agilent34401a.serial_config import FlowControl, Parity, SerialSettings

_DEFAULT_TIMEOUT_S = 2.0
_MS_PER_S = 1000
_TERMINATION = "\n"
_BREAK_S = 0.05
_REMOTE = "SYST:REM"
_LOCAL = "SYST:LOC"
_VENDOR_HINT = "Install Keysight IO Libraries Suite (or NI-VISA) to use this Backend."


class _Resource(Protocol):
    """The part of a pyvisa message-based resource this module uses."""

    timeout: float

    def write(self, message: str) -> int: ...

    def read(self) -> str: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...


class _SerialResource(_Resource, Protocol):
    """The part of a pyvisa serial resource that `apply_serial_settings` sets."""

    baud_rate: int
    data_bits: int
    parity: constants.Parity
    stop_bits: constants.StopBits
    flow_control: constants.ControlFlow
    read_termination: str | None
    write_termination: str

    def set_visa_attribute(self, name: constants.ResourceAttribute, state: object) -> object: ...


_PARITY = {Parity.NONE: constants.Parity.none, Parity.EVEN: constants.Parity.even, Parity.ODD: constants.Parity.odd}
_FLOW_CONTROL = {
    FlowControl.NONE: constants.ControlFlow.none,
    FlowControl.XON_XOFF: constants.ControlFlow.xon_xoff,
    FlowControl.RTS_CTS: constants.ControlFlow.rts_cts,
    FlowControl.DTR_DSR: constants.ControlFlow.dtr_dsr,
}


_STOP_BITS = {1: constants.StopBits.one, 2: constants.StopBits.two}


def _line(*, asserted: bool) -> constants.LineState:
    return constants.LineState.asserted if asserted else constants.LineState.unasserted


def apply_serial_settings(resource: _SerialResource, settings: SerialSettings) -> None:
    """Configure an open serial `resource`: baud rate, Framing, Flow Control, terminators and manual DTR/RTS.

    The Meter ends every reply with CR LF, so replies are read up to LF and the CR is stripped by the Transport;
    `settings.terminator` is what goes after each command.
    """
    resource.baud_rate = settings.baud
    resource.data_bits = settings.framing.data_bits
    resource.parity = _PARITY[settings.framing.parity]
    resource.stop_bits = _STOP_BITS[settings.framing.stop_bits]
    resource.flow_control = _FLOW_CONTROL[settings.flow_control]
    resource.read_termination = _TERMINATION
    resource.write_termination = settings.terminator.text
    if settings.dtr is not None:
        resource.set_visa_attribute(constants.ResourceAttribute.asrl_dtr_state, _line(asserted=settings.dtr))
    if settings.rts is not None:
        resource.set_visa_attribute(constants.ResourceAttribute.asrl_rts_state, _line(asserted=settings.rts))


class _Manager(Protocol):
    def close(self) -> None: ...


@contextmanager
def _visa_errors() -> Iterator[None]:
    """Turn pyvisa's exceptions into the package's own."""
    try:
        yield
    except VisaIOError as error:
        if error.error_code == constants.StatusCode.error_timeout:
            message = f"Timed out waiting for the Meter ({error.description})"
            raise TransportTimeoutError(message) from error
        raise TransportError(str(error)) from error
    except TimeoutError as error:
        message = f"Timed out waiting for the Meter ({error})"
        raise TransportTimeoutError(message) from error
    except OSError as error:  # pyvisa-py's socket sessions let connection errors through unwrapped
        message = f"The Connection to the Meter failed: {error}"
        raise TransportError(message) from error


class VisaTransport:
    """A Transport over one open pyvisa resource. It owns the resource and its manager."""

    def __init__(
        self,
        resource: _Resource,
        manager: _Manager,
        *,
        gpib: bool = False,
        serial: bool = False,
        device_clear: bool = True,
    ) -> None:
        self._resource = resource
        self._manager = manager
        self._gpib = gpib
        self._serial = serial
        self.supports_device_clear = device_clear
        """Whether `clear` really abandons what the Meter is doing; a raw socket has no device clear to send."""
        self._closed = False
        self.timeout = _DEFAULT_TIMEOUT_S

    @property
    def timeout(self) -> float:
        """Seconds to wait for a reply; pyvisa counts milliseconds."""
        return float(self._resource.timeout) / _MS_PER_S

    @timeout.setter
    def timeout(self, seconds: float) -> None:
        self._resource.timeout = round(seconds * _MS_PER_S)

    def write(self, command: str) -> None:
        self._require_open()
        with _visa_errors():
            self._resource.write(command)

    def read(self) -> str:
        self._require_open()
        with _visa_errors():
            return self._resource.read().rstrip("\r\n")

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        """Device clear: flush the buffers (GPIB: the bus message). RS-232 also sends a serial break."""
        self._require_open()
        with _visa_errors():
            self._resource.clear()
            if self._serial:
                self._send_break()

    def _send_break(self) -> None:
        set_attribute = getattr(self._resource, "set_visa_attribute", None)
        if not callable(set_attribute):
            return
        set_attribute(constants.ResourceAttribute.asrl_break_state, constants.LineState.asserted)
        try:
            time.sleep(_BREAK_S)
        finally:
            set_attribute(constants.ResourceAttribute.asrl_break_state, constants.LineState.unasserted)

    def go_to_remote(self) -> None:
        """Put an RS-232 Meter in Remote with `SYST:REM`; GPIB's REN line does that when the port opens."""
        self._require_open()
        if self._serial:
            self.write(_REMOTE)

    def go_to_local(self) -> None:
        """Return the Meter to Local: GPIB addresses it with the REN line's go-to-local, RS-232 sends `SYST:LOC`.

        Other buses have no way to, so nothing.
        """
        self._require_open()
        if self._serial:
            self.write(_LOCAL)
            return
        control_ren = getattr(self._resource, "control_ren", None)
        if self._gpib and callable(control_ren):
            with _visa_errors():
                control_ren(constants.RENLineOperation.address_gtl)

    def set_local_lockout(self, *, locked: bool) -> bool:
        """Send the GPIB local lockout message, or release it; False on any other bus, which has no such message.

        Releasing sends GTL with REN deasserted, then asserts REN again so the next command finds the Meter in Remote.
        """
        control = getattr(self._resource, "gpib_control_ren", None)
        if not self._gpib or not callable(control):
            return False
        self._require_open()
        operations = (
            [constants.RENLineOperation.asrt_llo]
            if locked
            else [constants.RENLineOperation.deassert_gtl, constants.RENLineOperation.asrt]
        )
        with _visa_errors():
            for operation in operations:
                control(operation)
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(VisaIOError, OSError):
            self._resource.close()
        with contextlib.suppress(VisaIOError, OSError):
            self._manager.close()

    def _require_open(self) -> None:
        if self._closed:
            message = "The Connection is closed"
            raise TransportError(message)


def check_library(library: str) -> BackendStatus:
    """Report whether pyvisa can load the VISA implementation called `library` (`@ivi` or `@py`)."""
    try:
        manager = pyvisa.ResourceManager(library)
    except Exception as error:  # noqa: BLE001 - pyvisa raises OSError, ValueError or ImportError depending on the cause
        causes = [line.strip().rstrip(":").strip() for line in str(error).splitlines()]
        reason = "; ".join(cause for cause in causes if cause) or type(error).__name__
        return BackendStatus(available=False, reason=f"{reason}. {_VENDOR_HINT}" if library == "@ivi" else reason)
    manager.close()
    return BackendStatus(available=True)


def open_visa_transport(library: str, resource_name: str) -> VisaTransport:
    """Open `resource_name` through the VISA implementation `library` as a Transport.

    pyvisa-py connects TCP sockets lazily, so a refused connection shows on the first command, not here.
    """
    return _open(library, resource_name, gpib=resource_name.upper().startswith("GPIB"))


def open_serial_transport(library: str, settings: SerialSettings) -> VisaTransport:
    """Open the RS-232 port `settings` names through the VISA implementation `library`, configured as asked."""
    return _open(library, settings.resource_name, serial=settings)


def _open(
    library: str, resource_name: str, *, gpib: bool = False, serial: SerialSettings | None = None
) -> VisaTransport:
    try:
        manager = pyvisa.ResourceManager(library)
    except Exception as error:
        message = f"Could not load the VISA library {library}: {error}"
        raise BackendUnavailableError(message) from error
    resource = None
    try:
        resource = manager.open_resource(resource_name)
        if not isinstance(resource, MessageBasedResource):
            message = f"{resource_name} does not take text commands"
            raise TransportError(message)  # noqa: TRY301 - cleaned up by the handler below
        if serial is None:
            resource.read_termination = _TERMINATION
            resource.write_termination = _TERMINATION
        else:
            _configure_serial(resource, resource_name, serial)
    except TransportError:
        _release(resource, manager)
        raise
    except Exception as error:  # pyvisa and pyvisa-py raise VisaIOError, ValueError, OSError and bare Exception here
        _release(resource, manager)
        message = f"Could not open {resource_name}: {error}"
        raise TransportError(message) from error
    return VisaTransport(
        resource, manager, gpib=gpib, serial=serial is not None, device_clear=can_device_clear(resource_name)
    )


def can_device_clear(resource_name: str) -> bool:
    """Whether a device clear can reach the Meter through `resource_name`: not through a raw socket."""
    return not resource_name.upper().endswith("::SOCKET")


def _configure_serial(resource: MessageBasedResource, resource_name: str, settings: SerialSettings) -> None:
    if not isinstance(resource, SerialInstrument):
        message = f"{resource_name} is not a serial port"
        raise TransportError(message)
    apply_serial_settings(resource, settings)


def list_resources(library: str) -> list[str]:
    """Return the names of the resources the VISA implementation `library` can see right now (a Scan)."""
    try:
        manager = pyvisa.ResourceManager(library)
    except Exception as error:
        message = f"Could not load the VISA library {library}: {error}"
        raise BackendUnavailableError(message) from error
    try:
        with _visa_errors(), warnings.catch_warnings():
            # pyvisa-py warns that its TCP/IP discovery sees only the default interface unless psutil is installed;
            # a Meter is reached by GPIB or serial, so that is not worth a warning on every Scan.
            warnings.simplefilter("ignore", UserWarning)
            return list(manager.list_resources())
    finally:
        with contextlib.suppress(Exception):
            manager.close()


def _release(resource: object, manager: _Manager) -> None:
    """Close what a failed open left behind, ignoring any further trouble."""
    close = getattr(resource, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()
    with contextlib.suppress(Exception):
        manager.close()
