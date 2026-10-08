"""Math Operations in a Setup: only one at a time, validated, and part of what a Preset stores."""

import math

import pytest

from agilent34401a.errors import InvalidSetupError
from agilent34401a.math_operations import LimitResult, MathOperation, MathSettings
from agilent34401a.meter import Function, Reading, Setup, describe_setup

VOLTAGE_FUNCTIONS = [Function.DC_VOLTAGE, Function.AC_VOLTAGE]
NO_MATH_FUNCTIONS = [Function.CONTINUITY, Function.DIODE]
DECIBEL_OPERATIONS = [MathOperation.DB, MathOperation.DBM]
ANY_FUNCTION_OPERATIONS = [MathOperation.NULL, MathOperation.STATISTICS, MathOperation.LIMIT_TEST]


def test_the_five_math_operations_are_the_ones_the_meter_has():
    assert [(operation.label, operation.value) for operation in MathOperation] == [
        ("Null", "NULL"),
        ("dB", "DB"),
        ("dBm", "DBM"),
        ("Statistics", "AVER"),
        ("Limit Test", "LIM"),
    ]


def test_a_new_setup_has_no_math_operation():
    assert Setup.default(Function.DC_VOLTAGE).math.operation is None


def test_math_settings_start_where_a_reset_meter_has_them():
    math_settings = MathSettings()

    assert math_settings.null_offset == 0.0
    assert math_settings.db_reference == 0.0
    assert math_settings.dbm_reference_resistance == 600.0
    assert (math_settings.limit_lower, math_settings.limit_upper) == (0.0, 0.0)


def test_turning_one_math_operation_on_turns_the_other_off():
    setup = Setup.default(Function.DC_VOLTAGE).with_math(MathSettings().with_operation(MathOperation.NULL))

    switched = setup.with_math(setup.math.with_operation(MathOperation.STATISTICS))

    assert switched.math.operation is MathOperation.STATISTICS


def test_a_math_operation_can_be_turned_off_without_forgetting_its_settings():
    active = MathSettings(operation=MathOperation.NULL, null_offset=0.25)

    off = active.with_operation(None)

    assert off.operation is None
    assert off.null_offset == 0.25


@pytest.mark.parametrize("function", list(Function))
@pytest.mark.parametrize("operation", ANY_FUNCTION_OPERATIONS)
def test_null_statistics_and_limit_test_apply_to_every_function_that_measures(function, operation):
    if function in NO_MATH_FUNCTIONS:
        with pytest.raises(InvalidSetupError, match="no Math Operation"):
            Setup.default(function).with_math(MathSettings(operation=operation))
    else:
        setup = Setup.default(function).with_math(MathSettings(operation=operation))
        assert setup.math.operation is operation


@pytest.mark.parametrize("function", list(Function))
@pytest.mark.parametrize("operation", DECIBEL_OPERATIONS)
def test_db_and_dbm_apply_to_voltage_only(function, operation):
    if function in VOLTAGE_FUNCTIONS:
        assert Setup.default(function).with_math(MathSettings(operation=operation)).math.operation is operation
    else:
        with pytest.raises(InvalidSetupError):
            Setup.default(function).with_math(MathSettings(operation=operation))


def test_the_settings_of_an_operation_that_is_off_are_still_checked_but_do_not_depend_on_the_function():
    setup = Setup.default(Function.DIODE).with_math(MathSettings(null_offset=1.0))

    assert setup.math.operation is None


@pytest.mark.parametrize("resistance", [50, 600, 8000])
def test_dbm_reference_resistance_accepts_50_ohms_to_8_kilohms(resistance):
    assert MathSettings(dbm_reference_resistance=resistance).dbm_reference_resistance == resistance


@pytest.mark.parametrize("resistance", [49.9, 8000.1, 0, -600, math.nan, math.inf])
def test_dbm_reference_resistance_outside_50_ohms_to_8_kilohms_is_rejected(resistance):
    with pytest.raises(InvalidSetupError, match="dBm Reference Resistance"):
        MathSettings(dbm_reference_resistance=resistance)


@pytest.mark.parametrize("reference", [-200.0, 0.0, 200.0])
def test_db_reference_accepts_minus_200_to_plus_200_dbm(reference):
    assert MathSettings(db_reference=reference).db_reference == reference


@pytest.mark.parametrize("reference", [-200.1, 200.1, math.nan])
def test_db_reference_outside_200_dbm_is_rejected(reference):
    with pytest.raises(InvalidSetupError, match="dB reference"):
        MathSettings(db_reference=reference)


@pytest.mark.parametrize("offset", [math.nan, math.inf, -math.inf])
def test_a_null_offset_must_be_a_number(offset):
    with pytest.raises(InvalidSetupError, match="Null offset"):
        MathSettings(null_offset=offset)


def test_limits_may_be_equal_and_may_be_negative():
    assert MathSettings(limit_lower=-2.0, limit_upper=-1.0).limit_upper == -1.0
    assert MathSettings(limit_lower=1.0, limit_upper=1.0).limit_lower == 1.0


@pytest.mark.parametrize(("lower", "upper"), [(2.0, 1.0), (math.nan, 1.0), (0.0, math.inf)])
def test_limits_must_be_numbers_with_the_lower_not_above_the_upper(lower, upper):
    with pytest.raises(InvalidSetupError, match="Limit Test"):
        MathSettings(limit_lower=lower, limit_upper=upper)


def test_the_setup_describes_the_math_operation_in_effect():
    base = Setup.default(Function.DC_VOLTAGE)

    assert "Null" not in describe_setup(base)
    assert describe_setup(base.with_math(MathSettings(operation=MathOperation.NULL, null_offset=0.5))).endswith(
        "Null 500 mV"
    )
    assert describe_setup(base.with_math(MathSettings(operation=MathOperation.DBM))).endswith("dBm into 600 Ω")
    assert describe_setup(base.with_math(MathSettings(operation=MathOperation.DB, db_reference=-3))).endswith(
        "dB relative to -3 dBm"
    )
    assert describe_setup(base.with_math(MathSettings(operation=MathOperation.STATISTICS))).endswith("Statistics")
    limit = MathSettings(operation=MathOperation.LIMIT_TEST, limit_lower=-1, limit_upper=2.5)
    assert describe_setup(base.with_math(limit)).endswith("Limit Test -1 V to 2.5 V")


def test_a_reading_in_db_or_dbm_has_that_unit_and_no_prefix():
    plain = Reading(1.5, Function.DC_VOLTAGE, "+1.5E+00")

    assert plain.unit == "V"
    assert Reading(1.5, Function.DC_VOLTAGE, "x", math=MathOperation.DBM).unit == "dBm"
    assert Reading(1.5, Function.AC_VOLTAGE, "x", math=MathOperation.DB).unit == "dB"
    assert Reading(1.5, Function.DC_VOLTAGE, "x", math=MathOperation.NULL).unit == "V"


def test_a_reading_has_no_limit_result_unless_a_limit_test_gave_one():
    assert Reading(1.5, Function.DC_VOLTAGE, "x").limit is None
    assert Reading(1.5, Function.DC_VOLTAGE, "x", limit=LimitResult.HIGH).limit is LimitResult.HIGH
    assert [result.label for result in LimitResult] == ["PASS", "LO", "HI"]
