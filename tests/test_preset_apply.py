"""Telling which settings of a Preset the Meter did not take, by comparing what was asked for with what it reports."""

import pytest

from agilent34401a.math_operations import MathOperation, MathSettings
from agilent34401a.meter import (
    AcFilter,
    Autozero,
    Function,
    GateTime,
    InputImpedance,
    Setup,
)
from agilent34401a.preset_apply import rejected_settings
from agilent34401a.trigger import TriggerSettings, TriggerSource

DC_VOLTS = Setup.default(Function.DC_VOLTAGE)
AC_VOLTS = Setup.default(Function.AC_VOLTAGE)


def texts(wanted: Setup, actual: Setup) -> list[str]:
    return [str(rejection) for rejection in rejected_settings(wanted, actual)]


def test_a_meter_that_took_everything_rejected_nothing():
    wanted = DC_VOLTS.with_range(10.0).with_nplc(1).with_math(MathSettings(operation=MathOperation.NULL, null_offset=2))

    assert rejected_settings(wanted, wanted) == ()


def test_a_function_the_meter_did_not_switch_to_is_the_only_thing_reported():
    wanted = AC_VOLTS.with_ac_filter(AcFilter.SLOW)

    assert texts(wanted, DC_VOLTS) == ["Function: asked for AC V, the Meter is in DC V"]


@pytest.mark.parametrize(
    ("wanted", "actual", "expected"),
    [
        (DC_VOLTS.with_range(10.0), DC_VOLTS.with_range(100.0), "Range: asked for 10 V, the Meter has 100 V"),
        (DC_VOLTS.with_range(10.0), DC_VOLTS, "Range: asked for 10 V, the Meter has Autorange"),
        (DC_VOLTS, DC_VOLTS.with_range(1.0), "Range: asked for Autorange, the Meter has 1 V"),
        (
            DC_VOLTS.with_nplc(100),
            DC_VOLTS.with_nplc(10),
            "Integration Time: asked for 100 NPLC, the Meter has 10 NPLC",
        ),
        (
            AC_VOLTS.with_ac_filter(AcFilter.FAST),
            AC_VOLTS.with_ac_filter(AcFilter.SLOW),
            "AC Filter: asked for 200 Hz, the Meter has 3 Hz",
        ),
        (
            DC_VOLTS.with_autozero(Autozero.OFF),
            DC_VOLTS.with_autozero(Autozero.ON),
            "Autozero: asked for Off, the Meter has On",
        ),
        (
            DC_VOLTS.with_input_impedance(InputImpedance.HIGH_IMPEDANCE),
            DC_VOLTS,
            "Input Impedance: asked for >10 GΩ, the Meter has 10 MΩ",
        ),
    ],
)
def test_a_setting_the_meter_kept_different_is_named_with_both_values(wanted, actual, expected):
    assert texts(wanted, actual) == [expected]


def test_a_gate_time_the_meter_did_not_take_is_named_with_the_resolution_that_goes_with_it():
    wanted = Setup.default(Function.FREQUENCY).with_gate_time(GateTime.ONE_SECOND)

    assert texts(wanted, Setup.default(Function.FREQUENCY)) == [
        "Resolution: asked for 6½ digits, the Meter has 5½ digits",
        "Gate Time: asked for 1 s, the Meter has 100 ms",
    ]


def test_a_resolution_the_meter_did_not_take_is_named():
    wanted = DC_VOLTS.with_nplc(1)
    actual = DC_VOLTS.with_nplc(10)

    assert texts(wanted, actual) == [
        "Resolution: asked for 5½ digits, the Meter has 6½ digits",
        "Integration Time: asked for 1 NPLC, the Meter has 10 NPLC",
    ]


def test_autozero_once_is_an_action_so_the_meter_reporting_it_off_is_not_a_rejection():
    assert rejected_settings(DC_VOLTS.with_autozero(Autozero.ONCE), DC_VOLTS.with_autozero(Autozero.OFF)) == ()
    assert texts(DC_VOLTS.with_autozero(Autozero.ONCE), DC_VOLTS.with_autozero(Autozero.ON)) == [
        "Autozero: asked for Once, the Meter has On"
    ]


def test_each_trigger_setting_is_compared():
    wanted = DC_VOLTS.with_trigger(TriggerSettings(TriggerSource.BUS, delay=0.5, sample_count=10, trigger_count=None))

    assert texts(wanted, DC_VOLTS) == [
        "Trigger Source: asked for Bus, the Meter has Immediate",
        "Trigger Delay: asked for 0.5 s, the Meter has automatic",
        "Sample Count: asked for 10, the Meter has 1",
        "Trigger Count: asked for infinite, the Meter has 1",
    ]


def test_a_trigger_delay_the_meter_rounded_to_what_it_was_sent_is_not_a_rejection():
    wanted = DC_VOLTS.with_trigger(TriggerSettings(delay=0.123456789))
    actual = DC_VOLTS.with_trigger(TriggerSettings(delay=0.123457))

    assert rejected_settings(wanted, actual) == ()
    assert texts(wanted, DC_VOLTS.with_trigger(TriggerSettings(delay=0.13))) != []


def test_each_math_setting_is_compared():
    wanted = DC_VOLTS.with_math(
        MathSettings(MathOperation.LIMIT_TEST, null_offset=0.5, db_reference=-3.0, limit_lower=-1.0, limit_upper=2.0)
    )

    assert texts(wanted, DC_VOLTS) == [
        "Math Operation: asked for Limit Test, the Meter has none",
        "Null offset: asked for 0.5, the Meter has 0",
        "dB reference: asked for -3 dBm, the Meter has 0 dBm",
        "Limit Test lower bound: asked for -1, the Meter has 0",
        "Limit Test upper bound: asked for 2, the Meter has 0",
    ]


def test_math_is_not_compared_for_a_function_that_has_none():
    wanted = Setup.default(Function.CONTINUITY).with_math(MathSettings(null_offset=3.0))

    assert rejected_settings(wanted, Setup.default(Function.CONTINUITY)) == ()


def test_rejections_come_in_the_order_the_settings_are_sent():
    wanted = (
        DC_VOLTS.with_range(10.0)
        .with_nplc(1)
        .with_autozero(Autozero.OFF)
        .with_trigger(TriggerSettings(sample_count=3))
        .with_math(MathSettings(MathOperation.NULL))
    )

    settings = [rejection.setting for rejection in rejected_settings(wanted, DC_VOLTS)]

    assert settings == [
        "Range",
        "Resolution",
        "Integration Time",
        "Autozero",
        "Sample Count",
        "Math Operation",
    ]
