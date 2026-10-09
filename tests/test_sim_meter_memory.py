"""The Simulator's Meter Memory: `*SAV` and `*RCL` keep the whole Setup in four numbered locations."""

import pytest

from agilent34401a.sim import Simulator

NEVER_STORED = '-314,"Save/recall memory lost"'
OUT_OF_RANGE = '-222,"Data out of range"'


def errors(simulator: Simulator) -> list[str]:
    found = []
    while (entry := simulator.query("SYST:ERR?")) != '+0,"No error"':
        found.append(entry)
    return found


def configure_voltage(simulator: Simulator) -> None:
    for command in (
        'FUNC "VOLT:AC"',
        "VOLT:AC:RANG 10",
        "DET:BAND 200",
        'FUNC "RES"',
        "RES:RANG 1000",
        "RES:NPLC 10",
        'FUNC "VOLT:DC"',
        "VOLT:DC:RANG 1",
        "VOLT:DC:NPLC 1",
        "ZERO:AUTO OFF",
        "TRIG:SOUR BUS",
        "TRIG:DEL 0.5",
        "SAMP:COUN 7",
        "TRIG:COUN 3",
        "CALC:FUNC DB",
        "CALC:DB:REF 5",
        "CALC:STAT ON",
    ):
        simulator.write(command)


def describe(simulator: Simulator) -> dict[str, str]:
    """Everything a Setup holds, as the Meter reports it per Function (switching Function to look at each)."""
    seen = {}
    for function in ("VOLT:DC", "VOLT:AC", "RES"):
        simulator.write(f'FUNC "{function}"')
        prefix = {"VOLT:DC": "VOLT:DC", "VOLT:AC": "VOLT:AC", "RES": "RES"}[function]
        seen[f"{function} range"] = simulator.query(f"{prefix}:RANG?")
        seen[f"{function} autorange"] = simulator.query(f"{prefix}:RANG:AUTO?")
        if function != "VOLT:AC":
            seen[f"{function} nplc"] = simulator.query(f"{prefix}:NPLC?")
    seen["filter"] = simulator.query("DET:BAND?")
    seen["autozero"] = simulator.query("ZERO:AUTO?")
    for query in ("TRIG:SOUR?", "TRIG:DEL?", "TRIG:DEL:AUTO?", "SAMP:COUN?", "TRIG:COUN?"):
        seen[query] = simulator.query(query)
    return seen


def test_a_stored_setup_is_recalled_whole_after_everything_was_changed():
    simulator = Simulator()
    configure_voltage(simulator)
    simulator.write("*SAV 1")
    stored = describe(simulator)
    for command in ("*RST", 'FUNC "FREQ"', "VOLT:DC:NPLC 100", "TRIG:SOUR EXT", "SAMP:COUN 2"):
        simulator.write(command)

    simulator.write("*RCL 1")

    assert errors(simulator) == []
    assert simulator.query("FUNC?") == '"VOLT"'
    assert describe(simulator) == stored


def test_the_active_function_and_the_math_operation_come_back_with_a_recall():
    simulator = Simulator()
    simulator.write('FUNC "RES"')
    simulator.write("CALC:FUNC NULL")
    simulator.write("CALC:STAT ON")
    simulator.write("*SAV 2")
    simulator.write("*RST")
    assert simulator.query("CALC:STAT?") == "0"

    simulator.write("*RCL 2")

    assert simulator.query("FUNC?") == '"RES"'
    assert simulator.query("CALC:STAT?") == "1"
    assert simulator.query("CALC:FUNC?") == "NULL"


def test_each_function_keeps_its_own_range_and_integration_time_through_a_store_and_recall():
    simulator = Simulator()
    simulator.write('FUNC "VOLT:AC"')
    simulator.write("VOLT:AC:RANG 10")
    simulator.write('FUNC "VOLT:DC"')
    simulator.write("VOLT:DC:NPLC 0.2")
    simulator.write("*SAV 3")
    simulator.write("*RST")

    simulator.write("*RCL 3")

    assert simulator.query("VOLT:DC:NPLC?") == "+2.00000000E-01"
    simulator.write('FUNC "VOLT:AC"')
    assert simulator.query("VOLT:AC:RANG?") == "+1.00000000E+01"


def test_stored_setups_survive_a_reset_and_a_later_store_replaces_only_its_own_location():
    simulator = Simulator()
    simulator.write("VOLT:DC:NPLC 1")
    simulator.write("*SAV 1")
    simulator.write("VOLT:DC:NPLC 10")
    simulator.write("*SAV 2")
    simulator.write("*RST")

    simulator.write("*RCL 1")
    assert simulator.query("VOLT:DC:NPLC?") == "+1.00000000E+00"
    simulator.write("*RCL 2")
    assert simulator.query("VOLT:DC:NPLC?") == "+1.00000000E+01"
    simulator.write("VOLT:DC:NPLC 100")
    simulator.write("*SAV 1")
    simulator.write("*RCL 2")
    assert simulator.query("VOLT:DC:NPLC?") == "+1.00000000E+01"
    assert errors(simulator) == []


def test_a_recalled_setup_is_a_copy_so_changing_the_meter_afterwards_does_not_change_the_stored_one():
    simulator = Simulator()
    simulator.write("VOLT:DC:RANG 10")
    simulator.write("*SAV 1")
    simulator.write("*RCL 1")
    simulator.write("VOLT:DC:RANG 1")

    simulator.write("*RCL 1")

    assert simulator.query("VOLT:DC:RANG?") == "+1.00000000E+01"


def test_location_zero_is_the_power_down_state_which_starts_as_the_reset_state():
    simulator = Simulator()
    simulator.write("VOLT:DC:NPLC 100")
    simulator.write('FUNC "FREQ"')

    simulator.write("*RCL 0")

    assert errors(simulator) == []
    assert simulator.query("FUNC?") == '"VOLT"'
    assert simulator.query("VOLT:DC:NPLC?") == "+1.00000000E+01"


def test_recalling_a_location_that_was_never_stored_is_an_error_and_changes_nothing():
    simulator = Simulator()
    simulator.write('FUNC "RES"')

    simulator.write("*RCL 2")

    assert errors(simulator) == [NEVER_STORED]
    assert simulator.query("FUNC?") == '"RES"'


@pytest.mark.parametrize("command", ["*SAV 4", "*SAV -1", "*RCL 4", "*RCL -1", "*SAV 1.5", "*RCL 99"])
def test_a_location_outside_the_four_is_out_of_range(command):
    simulator = Simulator()
    simulator.write("*SAV 1")

    simulator.write(command)

    assert errors(simulator) == [OUT_OF_RANGE]


def test_location_zero_cannot_be_stored_to_because_the_meter_uses_it_for_power_down():
    simulator = Simulator()

    simulator.write("*SAV 0")

    assert errors(simulator) == [OUT_OF_RANGE]
    simulator.write("*RCL 0")
    assert errors(simulator) == []


@pytest.mark.parametrize("command", ["*SAV", "*RCL"])
def test_a_missing_location_is_a_missing_parameter(command):
    simulator = Simulator()

    simulator.write(command)

    assert errors(simulator) == ['-109,"Missing parameter"']


@pytest.mark.parametrize("command", ["*SAV TWO", "*RCL ONE"])
def test_a_location_that_is_not_a_number_is_an_illegal_parameter(command):
    simulator = Simulator()

    simulator.write(command)

    assert errors(simulator) == ['-224,"Illegal parameter value"']


def test_the_meter_memory_is_not_a_query():
    simulator = Simulator()

    simulator.write("*SAV? 1")

    assert errors(simulator) == ['-113,"Undefined header"']
