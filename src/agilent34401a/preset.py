"""Presets: a Setup written as versioned JSON, and read back.

A Preset is a named Setup. This module is the pure part: the JSON form of a Setup, with no files and no Meter.
`setup_to_json` writes every field of a Setup, `setup_from_json` reads one and is strict about it.
"""

from typing import Any

from agilent34401a.math_operations import MathOperation, MathSettings
from agilent34401a.meter import AcFilter, Autozero, Function, GateTime, InputImpedance, Resolution, Setup
from agilent34401a.trigger import TriggerSettings, TriggerSource

FORMAT_VERSION = 1
"""The version of the JSON a Preset is written in. Raised only when an older reader could not read the new form."""


def setup_to_json(setup: Setup) -> dict[str, Any]:
    """Return `setup` as JSON-ready values, every field of it."""
    trigger, math = setup.trigger, setup.math
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
            "operation": None if math.operation is None else math.operation.value,
            "null_offset": math.null_offset,
            "db_reference": math.db_reference,
            "dbm_reference_resistance": math.dbm_reference_resistance,
            "limit_lower": math.limit_lower,
            "limit_upper": math.limit_upper,
        },
    }


def setup_from_json(data: dict[str, Any]) -> Setup:
    """Return the Setup `data` holds."""
    trigger, math = data["trigger"], data["math"]
    operation = math["operation"]
    ac_filter, gate_time = data["ac_filter"], data["gate_time"]
    autozero, impedance = data["autozero"], data["input_impedance"]
    return Setup(
        function=Function(data["function"]),
        range=data["range"],
        resolution=Resolution(data["resolution"]),
        nplc=data["nplc"],
        ac_filter=None if ac_filter is None else AcFilter(ac_filter),
        gate_time=None if gate_time is None else GateTime(gate_time),
        autozero=None if autozero is None else Autozero(autozero),
        input_impedance=None if impedance is None else InputImpedance[impedance.upper()],
        trigger=TriggerSettings(
            TriggerSource(trigger["source"]), trigger["delay"], trigger["sample_count"], trigger["trigger_count"]
        ),
        math=MathSettings(
            None if operation is None else MathOperation(operation),
            math["null_offset"],
            math["db_reference"],
            math["dbm_reference_resistance"],
            math["limit_lower"],
            math["limit_upper"],
        ),
    )
