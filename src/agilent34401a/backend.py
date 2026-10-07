"""VISA Backends: which implementations exist, whether each loads here, and which one Auto picks (ADR-0001)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

from agilent34401a.errors import BackendUnavailableError


class Backend(Enum):
    """Which VISA implementation carries a Connection."""

    AUTO = "auto"
    VENDOR = "ivi"
    PYVISA_PY = "py"

    @property
    def library(self) -> str:
        """The name pyvisa knows this Backend by. Auto has none until it has been resolved to another."""
        if self is Backend.AUTO:
            message = "Auto is not a VISA library; resolve it to a concrete Backend first"
            raise ValueError(message)
        return "@ivi" if self is Backend.VENDOR else "@py"

    @property
    def label(self) -> str:
        return {
            Backend.AUTO: "Auto",
            Backend.VENDOR: "Keysight/NI VISA",
            Backend.PYVISA_PY: "pyvisa-py",
        }[self]


CONCRETE_BACKENDS = (Backend.VENDOR, Backend.PYVISA_PY)
"""The Backends Auto chooses between, in order of preference."""


@dataclass(frozen=True)
class BackendStatus:
    """Whether a Backend loads on this machine, and if not, why."""

    available: bool
    reason: str = ""


CheckLibrary = Callable[[str], BackendStatus]
"""Reports whether pyvisa can load the VISA library with the given name (`@ivi` or `@py`)."""


def detect_backends(check: CheckLibrary, requested: Backend = Backend.AUTO) -> dict[Backend, BackendStatus]:
    """Check the Backends a request needs: all of them for Auto, only the one asked for otherwise."""
    needed = CONCRETE_BACKENDS if requested is Backend.AUTO else (requested,)
    return {backend: check(backend.library) for backend in needed}


def resolve_backend(requested: Backend, statuses: Mapping[Backend, BackendStatus]) -> Backend:
    """Pick the Backend to use. Auto prefers vendor VISA and falls back to pyvisa-py; an explicit choice never falls back."""
    if requested is Backend.AUTO:
        for candidate in CONCRETE_BACKENDS:
            if statuses[candidate].available:
                return candidate
        reasons = "; ".join(f"{backend.label}: {statuses[backend].reason}" for backend in CONCRETE_BACKENDS)
        message = f"No VISA Backend is available. {reasons}"
        raise BackendUnavailableError(message)
    status = statuses[requested]
    if not status.available:
        message = f"{requested.label} is not available: {status.reason}"
        raise BackendUnavailableError(message)
    return requested
