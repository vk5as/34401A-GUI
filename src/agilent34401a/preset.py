"""Presets: a Setup written as versioned JSON, and read back.

A Preset is a named Setup. This module is the pure part: the JSON form of a Setup, with no files and no Meter.
`setup_to_json` writes every field of a Setup. `setup_from_json` reads one and is strict about it: anything wrong is a
`PresetError` that names the field.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, TypeVar, overload

from agilent34401a.errors import InvalidSetupError, PresetError
from agilent34401a.math_operations import MathOperation, MathSettings
from agilent34401a.meter import AcFilter, Autozero, Function, GateTime, InputImpedance, Resolution, Setup
from agilent34401a.trigger import TriggerSettings, TriggerSource

FORMAT_VERSION = 1
"""The version of the JSON a Preset is written in. Raised only when an older reader could not read the new form."""

FORMAT_NAME = "agilent34401a-presets"
"""What a Presets file says it is, so that some other JSON file is recognised as not being one."""
MAX_NAME_LENGTH = 60

_E = TypeVar("_E", bound=Enum)


@dataclass(frozen=True)
class Preset:
    """A named Setup. The name is how the user finds it; names are unique to the case within a store."""

    name: str
    setup: Setup


@dataclass(frozen=True)
class PresetFile:
    """The Presets a file held, and what was wrong with the entries that could not be used."""

    presets: tuple[Preset, ...]
    problems: tuple[str, ...]


def clean_name(name: str) -> str:
    """Return `name` trimmed, or raise `PresetError` if a Preset cannot be called that."""
    name = name.strip()
    if not name:
        message = "A Preset name cannot be empty"
        raise PresetError(message)
    if len(name) > MAX_NAME_LENGTH:
        message = f"A Preset name is at most {MAX_NAME_LENGTH} characters, not {len(name)}"
        raise PresetError(message)
    if any(not character.isprintable() for character in name):
        message = "A Preset name cannot contain line breaks, tabs or other control characters"
        raise PresetError(message)
    return name


def presets_to_json(presets: Iterable[Preset]) -> dict[str, Any]:
    """Return `presets` as the JSON document a Presets file holds."""
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "presets": [{"name": preset.name, "setup": setup_to_json(preset.setup)} for preset in presets],
    }


def presets_from_json(data: object) -> PresetFile:
    """Read a Presets document. A document that is not one, or is of a version this cannot read, is a `PresetError`.

    A Preset in it that is wrong, or whose name an earlier one already has, is left out and described in `problems`,
    so that one bad entry does not cost the user the rest.
    """
    if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
        message = "This is not a Preset file"
        raise PresetError(message)
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        message = f"The format version must be a whole number, not {version!r}"
        raise PresetError(message)
    if version != FORMAT_VERSION:
        message = f"This Preset file is of format version {version}, but this program reads version {FORMAT_VERSION}"
        raise PresetError(message)
    entries = data.get("presets")
    if not isinstance(entries, list):
        message = "'presets' is missing or is not a list"
        raise PresetError(message)
    presets: list[Preset] = []
    problems: list[str] = []
    for index, entry in enumerate(entries, start=1):
        label = f"Preset {index}"
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"].strip():
            label = f"Preset '{entry['name'].strip()}'"
        try:
            preset = _read_entry(entry)
            if any(preset.name.casefold() == earlier.name.casefold() for earlier in presets):
                message = f"There is already a Preset called '{preset.name}', so this one was left out"
                raise PresetError(message)  # noqa: TRY301 - reported as this entry's problem just below
            presets.append(preset)
        except PresetError as error:
            problems.append(f"{label}: {error}")
    return PresetFile(tuple(presets), tuple(problems))


def _read_entry(entry: object) -> Preset:
    fields = _object(entry, "a Preset")
    name = _get(fields, "name")
    if not isinstance(name, str):
        message = f"The Preset name must be text, not {name!r}"
        raise PresetError(message)
    return Preset(clean_name(name), setup_from_json(_get(fields, "setup")))


def setup_to_json(setup: Setup) -> dict[str, Any]:
    """Return `setup` as JSON-ready values, every field of it."""
    trigger, math_settings = setup.trigger, setup.math
    return {
        "function": setup.function.value,
        "range": setup.range,
        "resolution": setup.resolution.value,
        "nplc": setup.nplc,
        "ac_filter": None if setup.ac_filter is None else setup.ac_filter.hertz,
        "gate_time": None if setup.gate_time is None else setup.gate_time.seconds,
        "autozero": None if setup.autozero is None else setup.autozero.value,
        "input_impedance": None if setup.input_impedance is None else setup.input_impedance.name.lower(),
        "trigger": {
            "source": trigger.source.value,
            "delay": trigger.delay,
            "sample_count": trigger.sample_count,
            "trigger_count": trigger.trigger_count,
        },
        "math": {
            "operation": None if math_settings.operation is None else math_settings.operation.value,
            "null_offset": math_settings.null_offset,
            "db_reference": math_settings.db_reference,
            "dbm_reference_resistance": math_settings.dbm_reference_resistance,
            "limit_lower": math_settings.limit_lower,
            "limit_upper": math_settings.limit_upper,
        },
    }


def setup_from_json(data: object) -> Setup:
    """Return the Setup `data` holds, or raise `PresetError` saying which field is missing or wrong.

    Fields a Function does not have (the AC Filter of DC V, say) may be null or left out. Fields this version does
    not know are ignored, so a newer file that only added some stays readable.
    """
    setup = _object(data, "a Setup")
    trigger = _object(_get(setup, "trigger"), "'trigger'")
    math_section = _object(_get(setup, "math"), "'math'")
    impedances: dict[object, InputImpedance] = {member.name.lower(): member for member in InputImpedance}
    operation = _get(math_section, "operation", "math")
    try:
        return Setup(
            function=_choice(_get(setup, "function"), "function", _by_value(Function)),
            range=_real(_get(setup, "range"), "range", nullable=True),
            resolution=_choice(_get(setup, "resolution"), "resolution", _by_value(Resolution)),
            nplc=_real(_get(setup, "nplc", required=False), "nplc", nullable=True),
            ac_filter=_optional_choice(setup, "ac_filter", _by_value(AcFilter)),
            gate_time=_optional_choice(setup, "gate_time", _by_value(GateTime)),
            autozero=_optional_choice(setup, "autozero", _by_value(Autozero)),
            input_impedance=_optional_choice(setup, "input_impedance", impedances),
            trigger=TriggerSettings(
                _choice(_get(trigger, "source", "trigger"), "trigger.source", _by_value(TriggerSource)),
                _real(_get(trigger, "delay", "trigger"), "trigger.delay", nullable=True),
                _whole(_get(trigger, "sample_count", "trigger"), "trigger.sample_count"),
                _whole(_get(trigger, "trigger_count", "trigger"), "trigger.trigger_count", nullable=True),
            ),
            math=MathSettings(
                None if operation is None else _choice(operation, "math.operation", _by_value(MathOperation)),
                _math_number(math_section, "null_offset"),
                _math_number(math_section, "db_reference"),
                _math_number(math_section, "dbm_reference_resistance"),
                _math_number(math_section, "limit_lower"),
                _math_number(math_section, "limit_upper"),
            ),
        )
    except InvalidSetupError as error:
        raise PresetError(str(error)) from error


def _by_value(kind: type[_E]) -> dict[object, _E]:
    return {member.value: member for member in kind}


def _object(data: object, what: str) -> Mapping[str, object]:
    if not isinstance(data, dict):
        kind = "null" if data is None else type(data).__name__
        message = f"{what[0].upper() + what[1:]} must be a JSON object, not {kind}"
        raise PresetError(message)
    return data


def _get(section: Mapping[str, object], key: str, prefix: str = "", *, required: bool = True) -> object:
    if key not in section:
        if required:
            message = f"'{prefix + '.' if prefix else ''}{key}' is missing"
            raise PresetError(message)
        return None
    return section[key]


def _math_number(section: Mapping[str, object], key: str) -> float:
    return _real(_get(section, key, "math"), f"math.{key}")


@overload
def _real(value: object, path: str, *, nullable: Literal[True]) -> float | None: ...
@overload
def _real(value: object, path: str, *, nullable: Literal[False] = False) -> float: ...
def _real(value: object, path: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    number = _as_float(value)
    if number is None:
        message = f"'{path}' must be a finite number{' or null' if nullable else ''}, not {value!r}"
        raise PresetError(message)
    return number


def _as_float(value: object) -> float | None:
    """`value` as a float if it is a finite number, else None; an integer too big for a float is not finite."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


@overload
def _whole(value: object, path: str, *, nullable: Literal[True]) -> int | None: ...
@overload
def _whole(value: object, path: str, *, nullable: Literal[False] = False) -> int: ...
def _whole(value: object, path: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"'{path}' must be a whole number{' or null' if nullable else ''}, not {value!r}"
        raise PresetError(message)
    return value


def _choice(value: object, path: str, choices: Mapping[object, _E]) -> _E:
    # A bool would match 1 and 0 (a Gate Time of 1 s, say), and a list or object is not even hashable.
    if isinstance(value, str | int | float) and not isinstance(value, bool) and value in choices:
        return choices[value]
    message = f"'{path}' must be one of {', '.join(repr(choice) for choice in choices)}, not {value!r}"
    raise PresetError(message)


def _optional_choice(section: Mapping[str, object], key: str, choices: Mapping[object, _E]) -> _E | None:
    value = _get(section, key, required=False)
    return None if value is None else _choice(value, key, choices)
