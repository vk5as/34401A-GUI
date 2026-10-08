"""A Preset is a named Setup: what it stores, and that every Setup survives being written and read back."""

import itertools
import json

from agilent34401a.math_operations import MathOperation, MathSettings
from agilent34401a.meter import (
    NPLC_VALUES,
    AcFilter,
    Autozero,
    Function,
    GateTime,
    InputImpedance,
    Resolution,
    Setup,
)
from agilent34401a.preset import FORMAT_VERSION, setup_from_json, setup_to_json
from agilent34401a.trigger import TriggerSettings, TriggerSource

TRIGGERS = [
    TriggerSettings(),
    TriggerSettings(TriggerSource.BUS, delay=0.25, sample_count=10, trigger_count=None),
    TriggerSettings(TriggerSource.EXTERNAL, delay=3600.0, sample_count=50000, trigger_count=50000),
    TriggerSettings(TriggerSource.IMMEDIATE, delay=0.0, sample_count=2, trigger_count=3),
]


def math_variants(function: Function) -> list[MathSettings]:
    """No Operation, and each Operation the Function can do, with settings that are not the defaults."""
    settings = MathSettings(
        null_offset=-0.000123456789,
        db_reference=-12.5,
        dbm_reference_resistance=8000.0,
        limit_lower=-1e-9,
        limit_upper=2.5e6,
    )
    variants = [MathSettings(), settings]
    if function.has_math:
        for operation in MathOperation:
            if operation.in_decibels and not function.has_decibels:
                continue
            variants.append(settings.with_operation(operation))
    return variants


def sense_options(function: Function) -> list[Setup]:
    """Every combination of Range, Integration Time or Gate Time, and sense options the Function has."""
    base = Setup.default(function)
    setups = []
    for range_value in [None, *function.ranges]:
        ranged = base.with_range(range_value)
        if function.has_integration_time:
            variants = [ranged.with_nplc(nplc) for nplc in NPLC_VALUES]
        elif function.has_gate_time:
            variants = [ranged.with_gate_time(gate_time) for gate_time in GateTime]
        else:
            variants = [ranged]
        for variant in variants:
            filters = list(AcFilter) if function.has_ac_filter else [None]
            zeros = list(Autozero) if function.has_autozero else [None]
            impedances = list(InputImpedance) if function.has_input_impedance else [None]
            for ac_filter, autozero, impedance in itertools.product(filters, zeros, impedances):
                setup = variant
                if ac_filter is not None:
                    setup = setup.with_ac_filter(ac_filter)
                if autozero is not None:
                    setup = setup.with_autozero(autozero)
                if impedance is not None:
                    setup = setup.with_input_impedance(impedance)
                setups.append(setup)
    return setups


def every_setup() -> list[Setup]:
    setups = []
    for function in Function:
        for setup in sense_options(function):
            for trigger, math_settings in itertools.product(TRIGGERS, math_variants(function)):
                setups.append(setup.with_trigger(trigger).with_math(math_settings))
    return setups


EVERY_SETUP = every_setup()


def test_the_format_has_a_version():
    assert FORMAT_VERSION == 1


def test_every_setup_survives_being_written_and_read_back():
    assert len(EVERY_SETUP) > 5000  # all 11 Functions, with their Ranges, options, triggers and Math Operations
    for setup in EVERY_SETUP:
        written = json.loads(json.dumps(setup_to_json(setup)))  # through real JSON text, as a file would
        assert setup_from_json(written) == setup


def test_a_setup_is_written_as_the_documented_json():
    setup = (
        Setup.default(Function.DC_VOLTAGE)
        .with_range(10.0)
        .with_nplc(1)
        .with_autozero(Autozero.OFF)
        .with_input_impedance(InputImpedance.HIGH_IMPEDANCE)
        .with_trigger(TriggerSettings(TriggerSource.BUS, delay=None, sample_count=5, trigger_count=None))
        .with_math(MathSettings(operation=MathOperation.DBM, db_reference=-3.0, dbm_reference_resistance=50.0))
    )

    assert setup_to_json(setup) == {
        "function": "VOLT:DC",
        "range": 10.0,
        "resolution": 5.5,
        "nplc": 1,
        "ac_filter": None,
        "gate_time": None,
        "autozero": "OFF",
        "input_impedance": "high_impedance",
        "trigger": {"source": "BUS", "delay": None, "sample_count": 5, "trigger_count": None},
        "math": {
            "operation": "DBM",
            "null_offset": 0.0,
            "db_reference": -3.0,
            "dbm_reference_resistance": 50.0,
            "limit_lower": 0.0,
            "limit_upper": 0.0,
        },
    }


def test_the_options_of_other_functions_are_written_as_null():
    written = setup_to_json(Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.FAST))

    assert written["ac_filter"] == 200
    assert written["gate_time"] is None
    assert written["nplc"] is None
    assert written["autozero"] is None
    assert written["input_impedance"] is None
    assert written["resolution"] == Resolution.SIX_HALF.value
