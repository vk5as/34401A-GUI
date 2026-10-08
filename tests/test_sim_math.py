"""The Simulator's Math Operations: Null, dB, dBm, Statistics and Limit Test, as the Meter's CALC subsystem has them."""

import math

import pytest

from agilent34401a.meter import Function
from agilent34401a.sim import Simulator

NO_ERROR = '+0,"No error"'
OVERLOAD = 9.9e37
LIMIT_LOW_BIT = 2048
LIMIT_HIGH_BIT = 4096
QUESTIONABLE_SUMMARY = 8


def errors(simulator: Simulator) -> list[str]:
    found = []
    while (reply := simulator.query("SYST:ERR?")) != NO_ERROR:
        found.append(reply)
    return found


def number(simulator: Simulator, query: str) -> float:
    return float(simulator.query(query))


def reading(simulator: Simulator) -> float:
    return number(simulator, "READ?")


def with_math(operation: str, *, volts: float = 1.0, function: Function = Function.DC_VOLTAGE) -> Simulator:
    simulator = Simulator(signals={function: volts})
    simulator.write(f'FUNC "{function.value}"')
    simulator.write(f"CALC:FUNC {operation}")
    simulator.write("CALC:STAT ON")
    return simulator


def test_a_reset_meter_has_no_math_operation_on():
    simulator = Simulator()

    assert simulator.query("CALC:STAT?") == "0"
    assert simulator.query("CALC:FUNC?") == "NULL"
    assert number(simulator, "CALC:NULL:OFFS?") == 0
    assert number(simulator, "CALC:DB:REF?") == 0
    assert number(simulator, "CALC:DBM:REF?") == 600
    assert (number(simulator, "CALC:LIM:LOW?"), number(simulator, "CALC:LIM:UPP?")) == (0, 0)
    assert errors(simulator) == []


@pytest.mark.parametrize(
    ("sent", "reported"),
    [
        ("NULL", "NULL"),
        ("DB", "DB"),
        ("DBM", "DBM"),
        ("AVER", "AVER"),
        ("AVERAGE", "AVER"),
        ("LIM", "LIM"),
        ("LIMIT", "LIM"),
    ],
)
def test_calc_func_selects_the_math_operation(sent, reported):
    simulator = Simulator()

    simulator.write(f"CALC:FUNC {sent}")

    assert simulator.query("CALC:FUNC?") == reported
    assert errors(simulator) == []


def test_the_long_form_of_the_calculate_headers_is_understood():
    simulator = Simulator()
    simulator.write("CALCULATE:FUNCTION LIMIT")
    simulator.write("CALCULATE:LIMIT:LOWER -2")
    simulator.write("CALCULATE:STATE ON")

    assert simulator.query("CALCULATE:STATE?") == "1"
    assert number(simulator, "CALC:LIM:LOW?") == -2
    assert errors(simulator) == []


def test_calc_func_refuses_what_is_not_a_math_operation():
    simulator = Simulator()

    simulator.write("CALC:FUNC SQUARE")
    simulator.write("CALC:FUNC")

    assert errors(simulator) == ['-224,"Illegal parameter value"', '-109,"Missing parameter"']
    assert simulator.query("CALC:FUNC?") == "NULL"


def test_calc_state_takes_on_and_off_and_nothing_else():
    simulator = Simulator()
    simulator.write("CALC:STAT 1")
    assert simulator.query("CALC:STAT?") == "1"
    simulator.write("CALC:STAT OFF")
    assert simulator.query("CALC:STAT?") == "0"

    simulator.write("CALC:STAT MAYBE")

    assert errors(simulator) == ['-224,"Illegal parameter value"']


def test_choosing_another_math_operation_while_one_is_on_replaces_it():
    simulator = with_math("NULL")

    simulator.write("CALC:FUNC DBM")

    assert simulator.query("CALC:STAT?") == "1"
    assert simulator.query("CALC:FUNC?") == "DBM"


# --- Null


def test_null_subtracts_the_offset_from_each_reading():
    simulator = with_math("NULL", volts=1.0)
    simulator.write("CALC:NULL:OFFS 0.25")

    assert reading(simulator) == pytest.approx(0.75)


def test_null_without_an_offset_takes_the_first_reading_as_the_offset():
    simulator = with_math("NULL", volts=1.0)

    assert reading(simulator) == 0
    simulator.set_signal(Function.DC_VOLTAGE, 1.5)
    assert reading(simulator) == pytest.approx(0.5)
    assert number(simulator, "CALC:NULL:OFFS?") == pytest.approx(1.0)


def test_the_null_offset_is_kept_when_null_is_turned_off():
    simulator = with_math("NULL", volts=1.0)
    simulator.write("CALC:NULL:OFFS 0.25")
    simulator.write("CALC:STAT OFF")

    assert reading(simulator) == 1.0
    assert number(simulator, "CALC:NULL:OFFS?") == 0.25


def test_null_applies_to_other_functions_too():
    simulator = with_math("NULL", volts=1000.0, function=Function.RESISTANCE_2W)
    simulator.write("CALC:NULL:OFFS 12")

    assert reading(simulator) == pytest.approx(988)


def test_null_leaves_an_overload_alone():
    simulator = with_math("NULL", volts=2000.0)
    simulator.write("VOLT:DC:RANG 10")
    simulator.write("CALC:NULL:OFFS 1")

    assert reading(simulator) == OVERLOAD


def test_a_null_offset_must_be_a_number():
    simulator = Simulator()

    simulator.write("CALC:NULL:OFFS lots")

    assert errors(simulator) == ['-224,"Illegal parameter value"']


# --- dB and dBm


@pytest.mark.parametrize(
    ("volts", "ohms", "dbm"),
    [(1.0, 600, 2.2184875), (1.0, 50, 13.0103), (0.7746, 600, 0.0), (-1.0, 600, 2.2184875), (10.0, 8000, 10.969)],
)
def test_dbm_is_the_power_the_reading_would_dissipate_in_the_reference_resistance(volts, ohms, dbm):
    simulator = with_math("DBM", volts=volts)
    simulator.write(f"CALC:DBM:REF {ohms}")

    assert reading(simulator) == pytest.approx(dbm, abs=1e-3)


def test_db_is_dbm_relative_to_the_reference():
    simulator = with_math("DB", volts=1.0)
    simulator.write("CALC:DB:REF -3")

    assert reading(simulator) == pytest.approx(2.2184875 + 3, abs=1e-4)


def test_db_and_dbm_work_on_ac_voltage():
    simulator = with_math("DBM", volts=1.0, function=Function.AC_VOLTAGE)

    assert reading(simulator) == pytest.approx(2.2184875, abs=1e-4)


def test_no_voltage_is_minus_200_dbm():
    simulator = with_math("DBM", volts=0.0)

    assert reading(simulator) == pytest.approx(-200)


def test_dbm_of_a_high_voltage_into_the_smallest_reference_resistance():
    simulator = with_math("DBM", volts=700.0)
    simulator.write("CALC:DBM:REF MIN")
    assert number(simulator, "CALC:DBM:REF?") == 50

    assert reading(simulator) == pytest.approx(10 * math.log10(700**2 / 50 / 0.001), abs=1e-3)


def test_db_and_dbm_leave_an_overload_alone():
    simulator = with_math("DBM", volts=2000.0)
    simulator.write("VOLT:DC:RANG 10")

    assert reading(simulator) == OVERLOAD


@pytest.mark.parametrize("ohms", ["49", "8001", "0", "-600"])
def test_the_dbm_reference_resistance_is_50_to_8000_ohms(ohms):
    simulator = Simulator()

    simulator.write(f"CALC:DBM:REF {ohms}")

    assert errors(simulator) == ['-222,"Data out of range"']
    assert number(simulator, "CALC:DBM:REF?") == 600


def test_the_dbm_reference_resistance_can_be_asked_for_by_its_limits():
    simulator = Simulator()
    simulator.write("CALC:DBM:REF MAX")
    assert number(simulator, "CALC:DBM:REF?") == 8000
    simulator.write("CALC:DBM:REF DEF")
    assert number(simulator, "CALC:DBM:REF?") == 600


@pytest.mark.parametrize("reference", ["200.5", "-201"])
def test_the_db_reference_is_within_200_dbm(reference):
    simulator = Simulator()

    simulator.write(f"CALC:DB:REF {reference}")

    assert errors(simulator) == ['-222,"Data out of range"']


@pytest.mark.parametrize("operation", ["DB", "DBM"])
@pytest.mark.parametrize("function", [Function.DC_CURRENT, Function.FREQUENCY, Function.RESISTANCE_2W])
def test_db_and_dbm_cannot_be_turned_on_for_functions_that_are_not_voltage(operation, function):
    simulator = Simulator()
    simulator.write(f'FUNC "{function.value}"')
    simulator.write(f"CALC:FUNC {operation}")

    simulator.write("CALC:STAT ON")

    assert errors(simulator) == ['-221,"Settings conflict"']
    assert simulator.query("CALC:STAT?") == "0"


@pytest.mark.parametrize("function", [Function.CONTINUITY, Function.DIODE])
def test_no_math_operation_can_be_turned_on_for_continuity_or_diode(function):
    simulator = Simulator()
    simulator.write(f'FUNC "{function.value}"')

    simulator.write("CALC:STAT ON")

    assert errors(simulator) == ['-221,"Settings conflict"']
    assert simulator.query("CALC:STAT?") == "0"


def test_db_cannot_replace_null_while_the_function_is_not_voltage():
    simulator = with_math("NULL", volts=1.0, function=Function.DC_CURRENT)

    simulator.write("CALC:FUNC DB")

    assert errors(simulator) == ['-221,"Settings conflict"']
    assert simulator.query("CALC:FUNC?") == "NULL"


# --- Statistics


def test_statistics_keep_the_minimum_maximum_average_and_count_of_the_readings():
    simulator = with_math("AVER")
    for volts in (1.0, 3.0, 2.0):
        simulator.set_signal(Function.DC_VOLTAGE, volts)
        assert reading(simulator) == volts  # Statistics does not change the Reading

    assert number(simulator, "CALC:AVER:MIN?") == 1.0
    assert number(simulator, "CALC:AVER:MAX?") == 3.0
    assert number(simulator, "CALC:AVER:AVER?") == pytest.approx(2.0)
    assert number(simulator, "CALC:AVER:COUN?") == 3
    assert errors(simulator) == []


def test_the_long_forms_of_the_statistics_queries_are_understood():
    simulator = with_math("AVER")
    reading(simulator)

    assert number(simulator, "CALCULATE:AVERAGE:COUNT?") == 1
    assert number(simulator, "CALCULATE:AVERAGE:MINIMUM?") == 1.0
    assert number(simulator, "CALCULATE:AVERAGE:MAXIMUM?") == 1.0


def test_statistics_are_empty_until_a_reading_is_taken():
    simulator = with_math("AVER")

    assert [number(simulator, f"CALC:AVER:{what}?") for what in ("MIN", "MAX", "AVER", "COUN")] == [0, 0, 0, 0]


def test_statistics_count_only_while_they_are_on():
    simulator = Simulator()
    reading(simulator)
    simulator.write("CALC:FUNC AVER")
    reading(simulator)
    simulator.write("CALC:STAT ON")
    reading(simulator)
    reading(simulator)
    simulator.write("CALC:STAT OFF")
    reading(simulator)

    assert number(simulator, "CALC:AVER:COUN?") == 2


def test_turning_statistics_on_again_starts_them_afresh():
    simulator = with_math("AVER")
    reading(simulator)
    reading(simulator)
    simulator.write("CALC:STAT OFF")
    simulator.write("CALC:STAT ON")

    reading(simulator)

    assert number(simulator, "CALC:AVER:COUN?") == 1


def test_asking_for_statistics_to_be_on_when_they_are_does_not_restart_them():
    simulator = with_math("AVER")
    reading(simulator)
    simulator.write("CALC:FUNC AVER")
    simulator.write("CALC:STAT ON")

    reading(simulator)

    assert number(simulator, "CALC:AVER:COUN?") == 2


def test_an_overload_is_not_counted_in_the_statistics():
    simulator = with_math("AVER", volts=2000.0)
    simulator.write("VOLT:DC:RANG 10")
    reading(simulator)

    assert number(simulator, "CALC:AVER:COUN?") == 0


def test_statistics_use_the_readings_of_whichever_function_is_measuring():
    simulator = with_math("AVER", volts=1000.0, function=Function.FREQUENCY)
    reading(simulator)
    reading(simulator)

    assert number(simulator, "CALC:AVER:AVER?") == pytest.approx(1000)


# --- Limit Test


def limit_simulator(volts: float, lower: float = -1.0, upper: float = 1.0) -> Simulator:
    simulator = Simulator(signals={Function.DC_VOLTAGE: volts})
    simulator.write(f"CALC:LIM:LOW {lower}")
    simulator.write(f"CALC:LIM:UPP {upper}")
    simulator.write("CALC:FUNC LIM")
    simulator.write("CALC:STAT ON")
    return simulator


def questionable(simulator: Simulator) -> int:
    return int(number(simulator, "STAT:QUES:EVEN?"))


def test_a_reading_above_the_upper_limit_sets_the_limit_fail_high_bit():
    simulator = limit_simulator(1.5)

    assert reading(simulator) == 1.5  # the Reading itself is not changed
    assert questionable(simulator) == LIMIT_HIGH_BIT


def test_a_reading_below_the_lower_limit_sets_the_limit_fail_low_bit():
    simulator = limit_simulator(-1.5)

    reading(simulator)

    assert questionable(simulator) == LIMIT_LOW_BIT


@pytest.mark.parametrize("volts", [-1.0, 0.0, 1.0])
def test_a_reading_within_the_limits_sets_neither_bit(volts):
    simulator = limit_simulator(volts)

    reading(simulator)

    assert questionable(simulator) == 0


def test_an_overload_fails_the_limit_test_on_its_side():
    simulator = limit_simulator(2000.0)
    simulator.write("VOLT:DC:RANG 10")

    reading(simulator)

    assert questionable(simulator) == LIMIT_HIGH_BIT


def test_the_condition_register_follows_the_latest_reading_and_the_event_register_latches():
    simulator = limit_simulator(1.5)
    reading(simulator)
    simulator.set_signal(Function.DC_VOLTAGE, 0.0)
    reading(simulator)

    assert int(number(simulator, "STAT:QUES:COND?")) == 0
    assert questionable(simulator) == LIMIT_HIGH_BIT
    assert questionable(simulator) == 0  # reading the event register clears it


def test_the_status_byte_shows_a_limit_failure_only_when_it_is_enabled():
    simulator = limit_simulator(1.5)
    reading(simulator)
    assert int(number(simulator, "*STB?")) & QUESTIONABLE_SUMMARY == 0

    simulator.write(f"STAT:QUES:ENAB {LIMIT_LOW_BIT + LIMIT_HIGH_BIT}")

    assert int(number(simulator, "*STB?")) & QUESTIONABLE_SUMMARY
    assert int(number(simulator, "STAT:QUES:ENAB?")) == LIMIT_LOW_BIT + LIMIT_HIGH_BIT
    questionable(simulator)
    assert int(number(simulator, "*STB?")) & QUESTIONABLE_SUMMARY == 0


def test_clearing_the_status_clears_the_limit_flags():
    simulator = limit_simulator(1.5)
    reading(simulator)

    simulator.write("*CLS")

    assert questionable(simulator) == 0


def test_no_limit_flags_are_set_when_there_is_no_limit_test():
    simulator = Simulator(signals={Function.DC_VOLTAGE: 5.0})
    simulator.write("CALC:LIM:UPP 1")
    simulator.write("CALC:STAT ON")  # Null, the default operation

    reading(simulator)

    assert questionable(simulator) == 0


def test_the_limit_test_judges_the_reading_as_it_would_be_displayed_to_the_resolution_of_the_range():
    simulator = limit_simulator(1.0000004, lower=-1, upper=1)
    simulator.write("VOLT:DC:RANG 1")

    reading(simulator)

    assert questionable(simulator) == 0  # 1.0000004 V is 1.000000 V on the 1 V range at 6½ digits


# --- Turning math off


def test_changing_the_function_turns_the_math_operation_off():
    simulator = with_math("NULL")

    simulator.write('FUNC "CURR:DC"')

    assert simulator.query("CALC:STAT?") == "0"


def test_selecting_the_function_the_meter_is_in_leaves_the_math_operation_on():
    simulator = with_math("NULL")

    simulator.write('FUNC "VOLT:DC"')

    assert simulator.query("CALC:STAT?") == "1"


def test_a_reset_turns_math_off_and_restores_its_settings():
    simulator = with_math("DBM")
    simulator.write("CALC:DBM:REF 50")
    simulator.write("CALC:LIM:UPP 4")

    simulator.write("*RST")

    assert simulator.query("CALC:STAT?") == "0"
    assert number(simulator, "CALC:DBM:REF?") == 600
    assert number(simulator, "CALC:LIM:UPP?") == 0
    assert simulator.query("CALC:FUNC?") == "NULL"


def test_a_math_operation_that_is_off_does_not_touch_the_reading():
    simulator = Simulator(signals={Function.DC_VOLTAGE: 1.0})
    simulator.write("CALC:FUNC DBM")

    assert reading(simulator) == 1.0
