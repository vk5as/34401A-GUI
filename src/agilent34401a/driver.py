"""The SCPI driver: typed operations on a Meter, built on a Transport."""

from dataclasses import dataclass

from agilent34401a.errors import UnrecognisedIdentityError
from agilent34401a.meter import Function, Reading, parse_reading
from agilent34401a.transport import Transport

# Early firmware reports HEWLETT-PACKARD, later firmware Agilent Technologies; both are the same Meter.
_MANUFACTURERS = frozenset({"hewlett-packard", "agilent technologies"})
_MODEL = "34401a"
_IDENTITY_FIELDS = 4


@dataclass(frozen=True)
class Identity:
    """What the Meter answered to `*IDN?`."""

    manufacturer: str
    model: str
    serial: str
    firmware: str
    raw: str


class Driver:
    """Talks SCPI to a 34401A over a Transport. It knows nothing about threads or the UI."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def identify(self) -> Identity:
        """Ask the Meter who it is, rejecting anything that is not a 34401A."""
        raw = self._transport.query("*IDN?")
        fields = [field.strip() for field in raw.split(",")]
        if (
            len(fields) != _IDENTITY_FIELDS
            or fields[0].casefold() not in _MANUFACTURERS
            or fields[1].casefold() != _MODEL
        ):
            message = f"Expected an Agilent/HP 34401A but the device identified as {raw.strip()!r}"
            raise UnrecognisedIdentityError(message)
        manufacturer, model, serial, firmware = fields
        return Identity(manufacturer=manufacturer, model=model, serial=serial, firmware=firmware, raw=raw)

    def read(self) -> Reading:
        """Take one Reading.

        Selecting a Function arrives with a later issue, so for now this is DC voltage, the
        Function a freshly reset Meter measures.
        """
        return parse_reading(self._transport.query("READ?"), Function.DC_VOLTAGE)
