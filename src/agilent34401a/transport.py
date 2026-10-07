"""The Transport seam: the only way the application talks to a Meter."""

from typing import Protocol


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
