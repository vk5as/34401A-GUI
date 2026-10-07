"""The typed boundary around pyvisa (ADR-0007): nothing else in the package imports it."""

import contextlib
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

import pyvisa
from pyvisa import constants
from pyvisa.errors import VisaIOError
from pyvisa.resources import MessageBasedResource

from agilent34401a.backend import BackendStatus
from agilent34401a.errors import BackendUnavailableError, TransportError, TransportTimeoutError

_DEFAULT_TIMEOUT_S = 2.0
_MS_PER_S = 1000
_TERMINATION = "\n"
_VENDOR_HINT = "Install Keysight IO Libraries Suite (or NI-VISA) to use this Backend."


class _Resource(Protocol):
    """The part of a pyvisa message-based resource this module uses."""

    timeout: float

    def write(self, message: str) -> int: ...

    def read(self) -> str: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...


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

    def __init__(self, resource: _Resource, manager: _Manager, *, gpib: bool = False) -> None:
        self._resource = resource
        self._manager = manager
        self._gpib = gpib
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
        self._require_open()
        with _visa_errors():
            self._resource.clear()

    def go_to_local(self) -> None:
        """Address a GPIB Meter to Local (the REN line's go-to-local); other buses have no REN line, so nothing.

        RS-232 returns to Local with a command instead; that arrives with the RS-232 Connection (issue #7).
        """
        self._require_open()
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
        resource.read_termination = _TERMINATION
        resource.write_termination = _TERMINATION
    except TransportError:
        _release(resource, manager)
        raise
    except Exception as error:  # pyvisa and pyvisa-py raise VisaIOError, ValueError, OSError and bare Exception here
        _release(resource, manager)
        message = f"Could not open {resource_name}: {error}"
        raise TransportError(message) from error
    return VisaTransport(resource, manager, gpib=resource_name.upper().startswith("GPIB"))


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
