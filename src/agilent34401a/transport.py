"""The Transport seam: the only way the application talks to a Meter."""

from typing import Protocol, runtime_checkable


class Transport(Protocol):
    """A text channel to one Meter. Implemented by the Simulator now, and by VISA Connections later."""

    timeout: float
    """Seconds to wait for a reply before raising `TransportTimeoutError`."""

    def write(self, command: str) -> None:
        """Send a command, appending the line terminator."""

    def read(self) -> str:
        """Return the next reply with its line terminator stripped."""

    def query(self, command: str) -> str:
        """Send a command and return its reply."""

    def clear(self) -> None:
        """Device clear: discard anything the Meter was about to send and abandon the current operation."""

    def close(self) -> None:
        """Release the channel. Closing twice is harmless."""


@runtime_checkable
class RemoteControl(Protocol):
    """A Transport that has to be told to put the Meter in Remote (RS-232 does; GPIB's REN line does it on open).

    Optional: a Transport that needs nothing, such as the Simulator or a plain socket, simply does not have it.
    """

    def go_to_remote(self) -> None:
        """Put the Meter in Remote, so that it accepts commands."""


@runtime_checkable
class LocalControl(Protocol):
    """A Transport that can hand the Meter's front panel back to the user (Local).

    Optional: a Transport that cannot, such as a plain socket, simply does not have it.
    """

    def go_to_local(self) -> None:
        """Return the Meter to Local, leaving its Setup as it is."""


@runtime_checkable
class BusLockout(Protocol):
    """An optional extra for Connections that can send the bus-level local lockout (GPIB).

    RS-232 has no such message, so a Transport without it is locked with `SYST:RWL` instead.
    """

    def set_local_lockout(self, *, locked: bool) -> bool:
        """Lock the front panel's Local key (or release that lock); return False if this Connection cannot."""
