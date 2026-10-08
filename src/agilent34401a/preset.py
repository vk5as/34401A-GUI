"""Presets: a Setup written as versioned JSON, and read back.

A Preset is a named Setup. This module is the pure part: the JSON form of a Setup, with no files and no Meter.
`setup_to_json` writes every field of a Setup. `setup_from_json` reads one and is strict about it: anything wrong is a
`PresetError` that names the field.
"""

import math
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal, TypeVar, overload

from agilent34401a.errors import InvalidSetupError, PresetError
from agilent34401a.math_operations import MathOperation, MathSettings
from agilent34401a.meter import AcFilter, Autozero, Function, GateTime, InputImpedance, Resolution, Setup
from agilent34401a.trigger import TriggerSettings, TriggerSource

FORMAT_VERSION = 1
"""The version of the JSON a Preset is written in. Raised only when an older reader could not read the new form."""

_E = TypeVar("_E", bound=Enum)


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
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        message = f"'{path}' must be a finite number{' or null' if nullable else ''}, not {value!r}"
        raise PresetError(message)
    return value


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
