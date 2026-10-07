"""The Connection opener: turns connection settings into a Transport, choosing the VISA Backend (ADR-0001)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agilent34401a.backend import Backend, BackendStatus, detect_backends, resolve_backend
from agilent34401a.transport import Transport

_DEFAULT_GPIB_ADDRESS = 22
_MAX_GPIB_ADDRESS = 30


@dataclass(frozen=True)
class ConnectionSettings:
    """Everything needed to open a Connection to a Meter over GPIB, or over any raw VISA resource."""

    backend: Backend = Backend.AUTO
    resource: str | None = None
    gpib_board: int = 0
    gpib_address: int = _DEFAULT_GPIB_ADDRESS

    def __post_init__(self) -> None:
        if self.resource is not None and not self.resource.strip():
            message = "The VISA resource string cannot be blank"
            raise ValueError(message)
        if self.gpib_board < 0:
            message = f"The GPIB board index cannot be negative, got {self.gpib_board}"
            raise ValueError(message)
        if not 0 <= self.gpib_address <= _MAX_GPIB_ADDRESS:
            message = f"A GPIB address is 0 to {_MAX_GPIB_ADDRESS}, got {self.gpib_address}"
            raise ValueError(message)

    @property
    def resource_name(self) -> str:
        """The VISA resource to open: the raw string if one was given, otherwise the GPIB board and address."""
        if self.resource is not None:
            return self.resource
        return f"GPIB{self.gpib_board}::{self.gpib_address}::INSTR"


DetectBackends = Callable[[Backend], Mapping[Backend, BackendStatus]]
OpenVisa = Callable[[str, str], Transport]


def open_transport(
    settings: ConnectionSettings,
    *,
    detect: DetectBackends | None = None,
    open_visa: OpenVisa | None = None,
) -> Transport:
    """Open a Connection to the Meter described by `settings`.

    `detect` and `open_visa` are the seams tests replace to avoid needing VISA installed; they default to pyvisa.
    """
    if detect is None or open_visa is None:
        # Imported here so that nothing needs pyvisa until a real Connection is wanted.
        from agilent34401a import visa  # noqa: PLC0415

        detect = detect or (lambda requested: detect_backends(visa.check_library, requested))
        open_visa = open_visa or visa.open_visa_transport
    backend = resolve_backend(settings.backend, detect(settings.backend))
    return open_visa(backend.library, settings.resource_name)
