"""RS-232 settings: the serial port, baud rate, Framing, Flow Control, line terminator and manual DTR/RTS lines.

Pure data with validation; opening a port is `visa.py`'s business.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

BAUD_RATES = (300, 600, 1200, 2400, 4800, 9600)
"""The baud rates the 34401A offers, slowest first."""

_DATA_BITS = (7, 8)
_STOP_BITS = (1, 2)
_COM_PORT = re.compile(r"COM(\d+)", re.IGNORECASE)
_RESOURCE_SUFFIX = "::INSTR"


class Parity(Enum):
    """Whether each character carries a parity bit, and which kind."""

    NONE = "none"
    EVEN = "even"
    ODD = "odd"

    @property
    def letter(self) -> str:
        return self.value[0].upper()

    @property
    def label(self) -> str:
        return self.value.capitalize()


class FlowControl(Enum):
    """The RS-232 handshaking method."""

    NONE = "none"
    XON_XOFF = "xonxoff"
    RTS_CTS = "rtscts"
    DTR_DSR = "dtrdsr"

    @property
    def label(self) -> str:
        return {
            FlowControl.NONE: "None",
            FlowControl.XON_XOFF: "XON/XOFF",
            FlowControl.RTS_CTS: "RTS/CTS",
            FlowControl.DTR_DSR: "DTR/DSR",
        }[self]


class Terminator(Enum):
    """The line ending sent after every command."""

    LF = "lf"
    CR = "cr"
    CRLF = "crlf"

    @property
    def text(self) -> str:
        return {Terminator.LF: "\n", Terminator.CR: "\r", Terminator.CRLF: "\r\n"}[self]

    @property
    def label(self) -> str:
        return {Terminator.LF: "LF", Terminator.CR: "CR", Terminator.CRLF: "CR+LF"}[self]


@dataclass(frozen=True)
class Framing:
    """The RS-232 character format: data bits, parity and stop bits together."""

    data_bits: int = 8
    parity: Parity = Parity.NONE
    stop_bits: int = 1

    def __post_init__(self) -> None:
        if self.data_bits not in _DATA_BITS:
            message = f"A Framing has 7 or 8 data bits, got {self.data_bits}"
            raise ValueError(message)
        if self.stop_bits not in _STOP_BITS:
            message = f"A Framing has 1 or 2 stop bits, got {self.stop_bits}"
            raise ValueError(message)

    @property
    def label(self) -> str:
        """The usual short name, such as 8N1 or 7E1."""
        return f"{self.data_bits}{self.parity.letter}{self.stop_bits}"

    @classmethod
    def from_label(cls, label: str) -> "Framing":
        """Read a Framing from its short name (8N1, 7E1, 7O1, ...), in either case."""
        match = re.fullmatch(r"([78])([NEO])([12])", label.strip().upper())
        if match is None:
            message = f"{label!r} is not a Framing (such as 8N1, 7E1 or 7O1)"
            raise ValueError(message)
        parity = next(parity for parity in Parity if parity.letter == match.group(2))
        return cls(int(match.group(1)), parity, int(match.group(3)))


METER_FRAMINGS = (Framing(8, Parity.NONE, 1), Framing(7, Parity.EVEN, 1), Framing(7, Parity.ODD, 1))
"""The three Framings the 34401A's I/O menu offers, its factory setting (8N1) first."""


@dataclass(frozen=True)
class SerialSettings:
    """Everything needed to open the Meter's RS-232 port.

    `dtr` and `rts` are manual overrides for unusual cables: None leaves the line to the driver and Flow Control,
    True or False holds it asserted or unasserted.
    """

    port: str
    baud: int = 9600
    framing: Framing = field(default_factory=Framing)
    flow_control: FlowControl = FlowControl.NONE
    terminator: Terminator = Terminator.LF
    dtr: bool | None = None
    rts: bool | None = None

    def __post_init__(self) -> None:
        if not self.port.strip():
            message = "The serial port cannot be blank"
            raise ValueError(message)
        if self.baud not in BAUD_RATES:
            rates = ", ".join(str(rate) for rate in BAUD_RATES)
            message = f"The Meter's baud rate is one of {rates}, got {self.baud}"
            raise ValueError(message)

    @property
    def resource_name(self) -> str:
        """The VISA resource for the port: `COM3` becomes `ASRL3::INSTR`, `/dev/ttyUSB0` becomes `ASRL/dev/ttyUSB0::INSTR`."""
        port = self.port.strip()
        if port.upper().startswith("ASRL"):
            return port if port.upper().endswith(_RESOURCE_SUFFIX) else f"{port}{_RESOURCE_SUFFIX}"
        com = _COM_PORT.fullmatch(port)
        if com is not None:
            return f"ASRL{com.group(1)}{_RESOURCE_SUFFIX}"
        return f"ASRL{port}{_RESOURCE_SUFFIX}"

    def to_json(self) -> dict[str, Any]:
        """Return the settings as plain JSON data, for the settings file."""
        return {
            "port": self.port,
            "baud": self.baud,
            "framing": self.framing.label,
            "flow_control": self.flow_control.value,
            "terminator": self.terminator.value,
            "dtr": self.dtr,
            "rts": self.rts,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "SerialSettings":
        """Read settings written by `to_json`; raise `ValueError`, `KeyError` or `TypeError` if they do not fit."""
        dtr, rts = data["dtr"], data["rts"]
        if not all(line is None or isinstance(line, bool) for line in (dtr, rts)):
            message = "DTR and RTS are true, false or null"
            raise TypeError(message)
        if not isinstance(data["port"], str) or not isinstance(data["baud"], int):
            message = "The port is text and the baud rate a whole number"
            raise TypeError(message)
        return cls(
            port=data["port"],
            baud=data["baud"],
            framing=Framing.from_label(data["framing"]),
            flow_control=FlowControl(data["flow_control"]),
            terminator=Terminator(data["terminator"]),
            dtr=dtr,
            rts=rts,
        )

    def describe(self) -> str:
        """Return the settings in one line for a status bar or a message."""
        return f"{self.port.strip()}, {self.baud} baud, {self.framing.label}, {self.flow_control.label}"
