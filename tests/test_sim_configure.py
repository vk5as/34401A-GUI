"""The Simulator's CONFigure and MEASure forms: one command to select a Function, with its defaults, and take a Reading."""

import pytest

from agilent34401a.meter import Function
from agilent34401a.sim import Simulator

NO_ERROR = '+0,"No error"'


def errors(simulator: Simulator) -> list[str]:
    found = []
    while (entry := simulator.query("SYST:ERR?")) != NO_ERROR:
        found.append(entry)
    return found


def test_configure_selects_the_function_with_a_range_and_the_default_integration_time():
    simulator = Simulator()
    simulator.write("VOLT:DC:NPLC 100")

    simulator.write("CONF:RES 1000")

    assert errors(simulator) == []
    assert simulator.query("FUNC?") == '"RES"'
    assert simulator.query("RES:RANG?") == "+1.00000000E+03"
    assert simulator.query("RES:RANG:AUTO?") == "0"
    simulator.write("CONF:VOLT:DC")
    assert simulator.query("VOLT:DC:NPLC?") == "+1.00000000E+01"  # back to the default
    assert simulator.query("VOLT:DC:RANG:AUTO?") == "1"


def test_configure_puts_the_trigger_back_to_one_immediate_reading_and_turns_math_off():
    simulator = Simulator()
    for command in ("TRIG:SOUR BUS", "SAMP:COUN 9", "CALC:FUNC DB", "CALC:STAT ON"):
        simulator.write(command)

    simulator.write("CONF:VOLT:AC")

    assert simulator.query("TRIG:SOUR?") == "IMM"
    assert simulator.query("SAMP:COUN?") == "+1.00000000E+00"
    assert simulator.query("CALC:STAT?") == "0"


def test_the_long_forms_and_the_dc_default_work():
    simulator = Simulator()

    simulator.write("CONFIGURE:VOLTAGE:AC 10")
    assert simulator.query("FUNC?") == '"VOLT:AC"'
    simulator.write("CONFigure:CURRent")
    assert simulator.query("FUNC?") == '"CURR"'
    assert errors(simulator) == []


def test_measure_configures_and_returns_a_reading():
    simulator = Simulator(signals={Function.RESISTANCE_2W: 470.0})

    reply = simulator.query("MEAS:RES?")

    assert float(reply) == pytest.approx(470.0)
    assert simulator.query("FUNC?") == '"RES"'
    assert errors(simulator) == []


def test_a_range_that_does_not_exist_is_an_error_that_changes_nothing():
    simulator = Simulator()
    simulator.write('FUNC "FRES"')

    simulator.write("MEAS:VOLT:DC? 5000")

    assert errors(simulator) == ['-222,"Data out of range"']
    assert simulator.query("FUNC?") == '"FRES"'


def test_a_resolution_is_not_modelled_and_says_so_instead_of_being_ignored():
    simulator = Simulator()

    simulator.write("CONF:VOLT:DC 10,0.001")

    assert errors(simulator) == ['-224,"Illegal parameter value"']
    assert simulator.query("FUNC?") == '"VOLT"'


def test_an_unknown_function_is_an_undefined_header():
    simulator = Simulator()

    simulator.write("CONF:NOPE")

    assert errors(simulator) == ['-113,"Undefined header"']


def test_configure_is_not_a_query_and_measure_is_only_a_query():
    simulator = Simulator()

    simulator.write("CONF:VOLT:DC?")
    simulator.write("MEAS:VOLT:DC")

    assert errors(simulator) == ['-113,"Undefined header"', '-113,"Undefined header"']
