"""Reading a raw SCPI command well enough to keep calibration writes away from the Meter (ADR-0006).

A raw command is not a transparent pass-through. This module splits what the user typed into the commands the
Meter would see, works out which SCPI path each one addresses, and says whether any of them could change the
Meter's calibration. It has no I/O. It is deliberately paranoid: anything in the calibration subsystem that is not
one of a few known read-only queries counts as a calibration write, however it is spelled.

The Meter's parser works like this, and so does this one:

- `;` ends a command, and a line break ends the whole message. A command with a leading colon starts at the root; one without it is
  relative to the previous command's path minus that command's last mnemonic, so `CAL:COUN?;SEC:STAT OFF,1` is a
  calibration write.
- Mnemonics are case-insensitive and have a short and a long form (`CAL` and `CALibration`).
- Every control character and the space are white space.
- A query's header ends in `?`. Common commands (`*RST`) do not change the path.

Quotes are not tracked: splitting at a `;` inside a quoted string only ever makes the check stricter.
"""

import re
from dataclasses import dataclass

# The Meter's white space is every control character and the space (IEEE 488.2), not just what str.strip removes.
_WHITE_SPACE = "".join(chr(code) for code in range(33))
_LINE_BREAKS = re.compile(r"[\r\n]")
_WORDS = re.compile(r"[A-Za-z]+")
_HEADER = re.compile(r"[A-Za-z0-9_:*]*\??")

_CALIBRATION_LONG_FORM = "CALIBRATION"
_CALIBRATION_SHORTEST = 3  # CAL; CALC is a different subsystem (CALCulate), and no prefix of CALIBRATION is CALC

# Queries that only read the calibration state, as (short form, long form) per mnemonic. Nothing else under
# CALibration is let through, including `CAL?`, which performs a calibration.
_READ_ONLY_QUERIES = (
    (("CAL", "CALIBRATION"), ("COUN", "COUNT")),
    (("CAL", "CALIBRATION"), ("STR", "STRING")),
    (("CAL", "CALIBRATION"), ("SEC", "SECURE"), ("STAT", "STATE")),
    (("CAL", "CALIBRATION"), ("VAL", "VALUE")),
)


@dataclass(frozen=True)
class RawCommand:
    """What a raw command would do to the Meter, as far as the application needs to know."""

    is_empty: bool
    """Nothing but white space and separators."""
    has_reply: bool
    """At least one command in it is a query, so the Meter will answer."""
    changes_meter: bool
    """At least one command in it is not a query, so the Meter's Setup may be different afterwards."""
    writes_calibration: bool
    """At least one command is in the calibration subsystem and is not a known read-only query."""


@dataclass(frozen=True)
class _Part:
    path: tuple[str, ...]
    is_query: bool
    has_arguments: bool
    is_common: bool
    unreadable: str = ""
    """The text of a command with no header the Meter could read, kept so it can still be searched."""


def analyse(command: str) -> RawCommand:
    """Work out what `command`, which may hold several commands separated by `;`, would do to the Meter."""
    parts = _parts(command)
    return RawCommand(
        is_empty=not parts,
        has_reply=any(part.is_query for part in parts),
        changes_meter=any(not part.is_query for part in parts),
        writes_calibration=any(_writes_calibration(part) for part in parts),
    )


def _parts(command: str) -> list[_Part]:
    parts: list[_Part] = []
    for message in _LINE_BREAKS.split(command):
        previous: tuple[str, ...] = ()  # a line break ends the message, so the next command starts from the root
        for text in message.split(";"):
            part, previous = _part(text, previous)
            if part is not None:
                parts.append(part)
    return parts


def _part(text: str, previous: tuple[str, ...]) -> tuple[_Part | None, tuple[str, ...]]:
    """Read one command; `previous` is the path of the one before it, and the path to carry on from comes back."""
    text = text.lstrip(_WHITE_SPACE)
    absolute = text.startswith(":")
    if absolute:
        text = text[1:].lstrip(_WHITE_SPACE)
    header_match = _HEADER.match(text)
    header = header_match.group() if header_match else ""
    arguments = text[len(header) :].strip(_WHITE_SPACE)
    if not header and not arguments:
        return None, previous
    if not header:
        return _Part((), is_query=False, has_arguments=True, is_common=False, unreadable=text), previous
    is_query = header.endswith("?")
    header = header.removesuffix("?")
    if header.startswith("*"):
        return _Part((header,), is_query, bool(arguments), is_common=True), previous
    mnemonics = tuple(header.split(":"))
    path = mnemonics if absolute or not previous else (*previous[:-1], *mnemonics)
    return _Part(path, is_query, bool(arguments), is_common=False), path


def _writes_calibration(part: _Part) -> bool:
    if part.unreadable:
        # The Meter will not understand this, but there is no reason to rely on that when it looks like calibration.
        return any(_is_calibration(word) for word in _WORDS.findall(part.unreadable))
    if part.is_common:
        return part.path[0].upper().startswith("*CAL")
    if not any(_is_calibration(mnemonic) for mnemonic in part.path):
        return False
    return not (part.is_query and not part.has_arguments and _is_read_only_query(part.path))


def _is_calibration(mnemonic: str) -> bool:
    """Whether `mnemonic` could be CAL or CALibration, however abbreviated, in any case, with a numeric suffix."""
    name = mnemonic.upper().rstrip("0123456789")
    return len(name) >= _CALIBRATION_SHORTEST and _CALIBRATION_LONG_FORM.startswith(name)


def _is_read_only_query(path: tuple[str, ...]) -> bool:
    return any(
        len(path) == len(forms) and all(mnemonic.upper() in names for mnemonic, names in zip(path, forms, strict=True))
        for forms in _READ_ONLY_QUERIES
    )
