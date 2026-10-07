"""The Simulator's sense options: AC Filter, Gate Time, Autozero, Input Impedance and Terminals."""

import pytest

from agilent34401a.meter import Function, Terminals
from agilent34401a.sim import Simulator


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _errors(simulator: Simulator) -> list[str]:
    errors = []
    while (reply := simulator.query("SYST:ERR?")) != '+0,"No error"':
        errors.append(reply)
    return errors


def _timed_reading(commands: list[str], *, function: Function) -> float:
    sleep = SleepRecorder()
    simulator = Simulator(time_scale=1, sleep=sleep)
    simulator.timeout = 60
    simulator.write(f'FUNC "{function.value}"')
    for command in commands:
        simulator.write(command)
    simulator.query("READ?")
    (seconds,) = sleep.calls
    return seconds


def test_the_ac_filter_starts_at_20_hz():
    assert float(Simulator().query("DET:BAND?")) == 20


@pytest.mark.parametrize("hertz", [3, 20, 200])
def test_the_three_ac_filters_can_be_selected(hertz):
    simulator = Simulator()

    simulator.write(f"DET:BAND {hertz}")

    assert float(simulator.query("DET:BAND?")) == hertz
    assert _errors(simulator) == []


def test_the_ac_filter_query_uses_the_meters_number_format():
    simulator = Simulator()
    simulator.write("DET:BAND 200")

    assert simulator.query("DET:BAND?") == "+2.00000000E+02"


@pytest.mark.parametrize(("word", "expected"), [("MIN", 3), ("MAX", 200), ("DEF", 20), ("10", 20), ("50", 200)])
def test_an_ac_filter_between_two_bands_selects_the_next_one_up(word, expected):
    simulator = Simulator()

    simulator.write(f"DET:BAND {word}")

    assert float(simulator.query("DET:BAND?")) == expected


def test_an_ac_filter_above_200_hz_is_out_of_range():
    simulator = Simulator()

    simulator.write("DET:BAND 500")

    assert float(simulator.query("DET:BAND?")) == 20
    assert _errors(simulator) == ['-222,"Data out of range"']


def test_long_forms_of_the_ac_filter_command_are_understood():
    simulator = Simulator()

    simulator.write(":SENSE:DETECTOR:BANDWIDTH 3")

    assert float(simulator.query("SENS:DET:BAND?")) == 3


def test_a_reset_puts_the_ac_filter_back():
    simulator = Simulator()
    simulator.write("DET:BAND 3")

    simulator.write("*RST")

    assert float(simulator.query("DET:BAND?")) == 20


@pytest.mark.parametrize(("hertz", "seconds"), [(3, 7.0), (20, 1.0), (200, 0.1)])
@pytest.mark.parametrize("function", [Function.AC_VOLTAGE, Function.AC_CURRENT])
def test_the_ac_filter_decides_how_long_an_ac_reading_takes(function, hertz, seconds):
    assert _timed_reading([f"DET:BAND {hertz}"], function=function) == pytest.approx(seconds)


@pytest.mark.parametrize("prefix", ["FREQ", "PER"])
def test_the_gate_time_starts_at_100_ms(prefix):
    assert float(Simulator().query(f"{prefix}:APER?")) == pytest.approx(0.1)


@pytest.mark.parametrize("prefix", ["FREQ", "PER"])
@pytest.mark.parametrize("seconds", [0.01, 0.1, 1])
def test_the_three_gate_times_can_be_selected(prefix, seconds):
    simulator = Simulator()

    simulator.write(f"{prefix}:APER {seconds:g}")

    assert float(simulator.query(f"{prefix}:APER?")) == pytest.approx(seconds)
    assert _errors(simulator) == []


def test_frequency_and_period_each_have_their_own_gate_time():
    simulator = Simulator()

    simulator.write("FREQ:APER 1")

    assert float(simulator.query("PER:APER?")) == pytest.approx(0.1)


@pytest.mark.parametrize(("word", "expected"), [("MIN", 0.01), ("MAX", 1), ("DEF", 0.1), ("0.05", 0.1)])
def test_a_gate_time_between_two_choices_selects_the_next_one_up(word, expected):
    simulator = Simulator()

    simulator.write(f"FREQ:APER {word}")

    assert float(simulator.query("FREQ:APER?")) == pytest.approx(expected)


def test_a_gate_time_above_one_second_is_out_of_range():
    simulator = Simulator()

    simulator.write("PER:APER 5")

    assert float(simulator.query("PER:APER?")) == pytest.approx(0.1)
    assert _errors(simulator) == ['-222,"Data out of range"']


@pytest.mark.parametrize("function", [Function.FREQUENCY, Function.PERIOD])
@pytest.mark.parametrize("seconds", [0.01, 0.1, 1])
def test_the_gate_time_is_how_long_a_frequency_or_period_reading_takes(function, seconds):
    prefix = function.value

    assert _timed_reading([f"{prefix}:APER {seconds:g}"], function=function) == pytest.approx(seconds)


@pytest.mark.parametrize(("seconds", "expected"), [(0.01, 1234.6), (0.1, 1234.57), (1, 1234.568)])
def test_the_gate_time_decides_how_many_digits_a_frequency_has(seconds, expected):
    simulator = Simulator(signals={Function.FREQUENCY: 1234.56789})
    simulator.write('FUNC "FREQ"')
    simulator.write(f"FREQ:APER {seconds:g}")

    assert float(simulator.query("READ?")) == pytest.approx(expected, abs=1e-9)


def test_autozero_starts_on():
    assert Simulator().query("ZERO:AUTO?") == "1"


@pytest.mark.parametrize(("word", "expected"), [("OFF", "0"), ("ON", "1"), ("0", "0"), ("1", "1")])
def test_autozero_can_be_turned_on_and_off(word, expected):
    simulator = Simulator()

    simulator.write(f"ZERO:AUTO {word}")

    assert simulator.query("ZERO:AUTO?") == expected
    assert _errors(simulator) == []


def test_autozero_once_takes_one_offset_measurement_and_leaves_autozero_off():
    simulator = Simulator()

    simulator.write("ZERO:AUTO ONCE")

    assert simulator.query("ZERO:AUTO?") == "0"
    assert _errors(simulator) == []


def test_an_autozero_the_meter_does_not_have_is_an_illegal_parameter_value():
    simulator = Simulator()

    simulator.write("ZERO:AUTO SOMETIMES")

    assert simulator.query("ZERO:AUTO?") == "1"
    assert _errors(simulator) == ['-224,"Illegal parameter value"']


def test_autozero_without_a_value_is_a_missing_parameter():
    simulator = Simulator()

    simulator.write("ZERO:AUTO")

    assert _errors(simulator) == ['-109,"Missing parameter"']


def test_autozero_is_shared_by_the_functions_that_have_it():
    simulator = Simulator()
    simulator.write("ZERO:AUTO OFF")

    simulator.write('FUNC "CURR:DC"')

    assert simulator.query("ZERO:AUTO?") == "0"


def test_a_reset_turns_autozero_back_on():
    simulator = Simulator()
    simulator.write("ZERO:AUTO OFF")

    simulator.write("*RST")

    assert simulator.query("ZERO:AUTO?") == "1"


def test_turning_autozero_off_halves_the_time_of_a_reading():
    on = _timed_reading(["VOLT:DC:NPLC 100"], function=Function.DC_VOLTAGE)
    off = _timed_reading(["VOLT:DC:NPLC 100", "ZERO:AUTO OFF"], function=Function.DC_VOLTAGE)

    assert on == pytest.approx(4.0)
    assert off == pytest.approx(2.0)


def test_autozero_once_leaves_the_readings_as_fast_as_off():
    seconds = _timed_reading(["VOLT:DC:NPLC 100", "ZERO:AUTO ONCE"], function=Function.DC_VOLTAGE)

    assert seconds == pytest.approx(2.0)


def test_input_impedance_starts_at_10_megohms():
    assert Simulator().query("INP:IMP:AUTO?") == "0"


@pytest.mark.parametrize(("word", "expected"), [("ON", "1"), ("OFF", "0"), ("1", "1"), ("0", "0")])
def test_input_impedance_can_be_switched_between_10_megohms_and_over_10_gigohms(word, expected):
    simulator = Simulator()

    simulator.write(f"INP:IMP:AUTO {word}")

    assert simulator.query("INP:IMP:AUTO?") == expected
    assert _errors(simulator) == []


def test_an_input_impedance_the_meter_does_not_have_is_an_illegal_parameter_value():
    simulator = Simulator()

    simulator.write("INP:IMP:AUTO MAYBE")

    assert _errors(simulator) == ['-224,"Illegal parameter value"']


def test_a_reset_puts_the_input_impedance_back_to_10_megohms():
    simulator = Simulator()
    simulator.write("INP:IMP:AUTO ON")

    simulator.write("*RST")

    assert simulator.query("INP:IMP:AUTO?") == "0"


def test_the_terminals_are_front_until_a_test_chooses_the_rear_ones():
    simulator = Simulator()

    assert simulator.terminals is Terminals.FRONT
    assert simulator.query("ROUT:TERM?") == "FRON"


def test_the_rear_terminals_are_reported_when_the_switch_is_set_to_rear():
    simulator = Simulator()

    simulator.terminals = Terminals.REAR

    assert simulator.query("ROUT:TERM?") == "REAR"


def test_a_reset_does_not_move_the_terminals_switch():
    simulator = Simulator()
    simulator.terminals = Terminals.REAR

    simulator.write("*RST")

    assert simulator.query("ROUT:TERM?") == "REAR"


def test_the_terminals_can_only_be_read_remotely():
    simulator = Simulator()

    simulator.write("ROUT:TERM REAR")

    assert simulator.terminals is Terminals.FRONT
    assert _errors(simulator) == ['-113,"Undefined header"']


def test_the_long_form_of_the_terminals_query_is_understood():
    assert Simulator().query(":ROUTE:TERMINALS?") == "FRON"
