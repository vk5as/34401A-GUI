"""Reading a Setup strictly: a bad Preset is a clear error for that Preset, never a crash or a guess."""

from typing import Any

import pytest

from agilent34401a.errors import PresetError
from agilent34401a.meter import Function, Setup
from agilent34401a.preset import setup_from_json, setup_to_json


class HugeInteger(int):
    """An int that pytest names briefly instead of by its four hundred digits."""

    def __repr__(self) -> str:
        return "10**400"


HUGE_INTEGER = HugeInteger(10**400)  # too big for a float, so math.isfinite raises OverflowError on it
DC_VOLTAGE: dict[str, Any] = setup_to_json(Setup.default(Function.DC_VOLTAGE))


def changed(**changes: object) -> dict[str, object]:
    return {**DC_VOLTAGE, **changes}


def test_what_is_not_an_object_is_not_a_setup():
    not_objects: list[object] = [None, [], "VOLT:DC", 7]
    for data in not_objects:
        with pytest.raises(PresetError, match="object"):
            setup_from_json(data)


@pytest.mark.parametrize("field", ["function", "range", "resolution", "trigger", "math"])
def test_a_missing_field_is_named_in_the_error(field):
    data = {name: value for name, value in DC_VOLTAGE.items() if name != field}

    with pytest.raises(PresetError, match=f"'{field}'"):
        setup_from_json(data)


@pytest.mark.parametrize("field", ["nplc", "autozero", "input_impedance"])
def test_a_missing_option_the_function_needs_is_refused(field):
    data = {name: value for name, value in DC_VOLTAGE.items() if name != field}

    with pytest.raises(PresetError, match=r"DC V needs its|Integration Time"):
        setup_from_json(data)


def test_a_missing_option_the_function_does_not_have_is_simply_not_set():
    data = {name: value for name, value in DC_VOLTAGE.items() if name not in ("ac_filter", "gate_time")}

    assert setup_from_json(data) == Setup.default(Function.DC_VOLTAGE)


def test_fields_this_version_does_not_know_are_ignored():
    assert setup_from_json(changed(brightness=3)) == Setup.default(Function.DC_VOLTAGE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("function", "VOLT:XX"),
        ("function", 7),
        ("range", "10"),
        ("range", True),
        ("range", float("nan")),
        ("range", HUGE_INTEGER),
        ("nplc", HugeInteger(-(10**400))),
        ("resolution", 7.5),
        ("resolution", "6.5"),
        ("nplc", "10"),
        ("autozero", "SOMETIMES"),
        ("input_impedance", "1 MΩ"),
        ("ac_filter", "fast"),
        ("gate_time", 0.5),
    ],
)
def test_a_value_of_the_wrong_kind_is_named_in_the_error(field, value):
    with pytest.raises(PresetError, match=f"'{field}'"):
        setup_from_json(changed(**{field: value}))


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("trigger", "source", "TOUCH"),
        ("trigger", "delay", "soon"),
        ("trigger", "sample_count", 1.5),
        ("trigger", "sample_count", True),
        ("trigger", "trigger_count", "inf"),
        ("math", "operation", "FFT"),
        ("math", "null_offset", "0"),
        ("math", "limit_upper", None),
        ("math", "dbm_reference_resistance", float("inf")),
        ("math", "null_offset", HUGE_INTEGER),
        ("trigger", "delay", HUGE_INTEGER),
    ],
)
def test_a_bad_value_inside_the_trigger_or_math_is_named_in_the_error(section, field, value):
    inner = {**DC_VOLTAGE[section], field: value}

    with pytest.raises(PresetError, match=f"'{section}.{field}'"):
        setup_from_json(changed(**{section: inner}))


def test_a_missing_field_inside_the_trigger_or_math_is_named_in_the_error():
    inner = {name: value for name, value in DC_VOLTAGE["trigger"].items() if name != "sample_count"}

    with pytest.raises(PresetError, match=r"'trigger\.sample_count'"):
        setup_from_json(changed(trigger=inner))


def test_a_section_that_is_not_an_object_is_named_in_the_error():
    with pytest.raises(PresetError, match="'trigger'"):
        setup_from_json(changed(trigger=[]))


def test_a_setup_the_meter_could_never_hold_is_refused_with_the_reason():
    with pytest.raises(PresetError, match="not a Range of 5"):
        setup_from_json(changed(range=5.0))
    with pytest.raises(PresetError, match="Sample Count is 1 to 50000, not 0"):
        setup_from_json(changed(trigger={**DC_VOLTAGE["trigger"], "sample_count": 0}))
    with pytest.raises(PresetError, match="Resolution of DC V"):
        setup_from_json(changed(resolution=4.5))
    with pytest.raises(PresetError, match="DC V has no AC Filter"):
        setup_from_json(changed(ac_filter=200))
    with pytest.raises(PresetError, match="lower bound"):
        setup_from_json(changed(math={**DC_VOLTAGE["math"], "limit_lower": 2.0, "limit_upper": 1.0}))
    current = setup_to_json(Setup.default(Function.DC_CURRENT))
    with pytest.raises(PresetError, match="applies to DC V and AC V"):
        setup_from_json({**current, "math": {**current["math"], "operation": "DB"}})
