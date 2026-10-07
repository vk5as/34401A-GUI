from typing import TYPE_CHECKING, Any

import pytest

from agilent34401a.errors import TransportError, TransportTimeoutError
from agilent34401a.meter import Function
from agilent34401a.sim import (
    AGILENT_IDENTITY,
    HEWLETT_PACKARD_IDENTITY,
    TIME_SCALE_ENV_VAR,
    Simulator,
)

if TYPE_CHECKING:
    from agilent34401a.transport import Transport


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_simulator_is_a_transport():
    transport: Transport = Simulator()

    assert transport.query("*IDN?")


@pytest.mark.parametrize("identity", [HEWLETT_PACKARD_IDENTITY, AGILENT_IDENTITY])
def test_simulated_meter_identifies_as_either_manufacturer(identity):
    assert Simulator(identity=identity).query("*IDN?") == identity


def test_simulated_meter_identifies_as_hewlett_packard_by_default():
    assert Simulator().query("*IDN?") == HEWLETT_PACKARD_IDENTITY


def test_simulated_meter_reads_the_applied_dc_voltage_in_the_meters_reply_format():
    assert Simulator(dc_voltage=1.23457).query("READ?") == "+1.23457000E+00"
    assert Simulator(dc_voltage=-0.0045).query("READ?") == "-4.50000000E-03"


def test_commands_are_case_insensitive_and_whitespace_tolerant():
    assert Simulator().query("  read?\n") == Simulator().query("READ?")


def test_query_is_a_write_followed_by_a_read():
    simulator = Simulator()

    simulator.write("*IDN?")

    assert simulator.read() == HEWLETT_PACKARD_IDENTITY


def test_reading_with_nothing_to_read_times_out():
    with pytest.raises(TransportTimeoutError):
        Simulator().read()


def test_error_queue_starts_empty():
    assert Simulator().query("SYST:ERR?") == '+0,"No error"'


def test_unknown_command_queues_an_undefined_header_error_that_is_then_consumed():
    simulator = Simulator()

    simulator.write("BOGUS")

    assert simulator.query("SYST:ERR?") == '-113,"Undefined header"'
    assert simulator.query("SYST:ERR?") == '+0,"No error"'


def test_clear_status_empties_the_error_queue():
    simulator = Simulator()
    simulator.write("BOGUS")

    simulator.write("*CLS")

    assert simulator.query("SYST:ERR?") == '+0,"No error"'


def test_reset_is_accepted_without_an_error():
    simulator = Simulator()

    simulator.write("*RST")

    assert simulator.query("SYST:ERR?") == '+0,"No error"'


def test_device_clear_discards_an_unread_reply():
    simulator = Simulator()
    simulator.write("*IDN?")

    simulator.clear()

    with pytest.raises(TransportTimeoutError):
        simulator.read()


def test_closed_simulator_refuses_every_operation():
    simulator = Simulator()
    simulator.close()

    for operation in (
        lambda: simulator.write("*CLS"),
        simulator.read,
        lambda: simulator.query("*IDN?"),
        simulator.clear,
    ):
        with pytest.raises(TransportError):
            operation()


def test_closing_twice_is_harmless():
    simulator = Simulator()
    simulator.close()

    simulator.close()


def test_time_scale_defaults_to_instant_in_tests():
    assert Simulator().time_scale == 0


def test_time_scale_defaults_to_real_time_without_the_environment_override(monkeypatch):
    monkeypatch.delenv(TIME_SCALE_ENV_VAR)

    assert Simulator().time_scale == 1


def test_time_scale_can_be_set_through_the_environment(monkeypatch):
    monkeypatch.setenv(TIME_SCALE_ENV_VAR, "0.25")

    assert Simulator().time_scale == 0.25


def test_negative_time_scale_is_rejected():
    with pytest.raises(ValueError, match="time scale"):
        Simulator(time_scale=-1)


def test_a_reading_takes_the_integration_time_scaled_by_the_time_scale():
    sleep = SleepRecorder()
    simulator = Simulator(time_scale=2, sleep=sleep)

    simulator.query("READ?")

    # 10 PLC at 50 Hz is 0.2 s, doubled by Autozero, then scaled by 2.
    assert sleep.calls == [pytest.approx(0.8)]


def test_commands_other_than_a_reading_take_no_time():
    sleep = SleepRecorder()
    simulator = Simulator(time_scale=1, sleep=sleep)

    simulator.query("*IDN?")
    simulator.write("*CLS")

    assert sleep.calls == []


def test_instant_time_scale_never_sleeps():
    sleep = SleepRecorder()

    Simulator(time_scale=0, sleep=sleep).query("READ?")

    assert sleep.calls == []


def test_reading_that_outlasts_the_timeout_raises_a_timeout():
    sleep = SleepRecorder()
    simulator = Simulator(time_scale=1, sleep=sleep)
    simulator.timeout = 0.1

    simulator.write("READ?")

    with pytest.raises(TransportTimeoutError):
        simulator.read()


def test_reading_that_fits_inside_the_timeout_is_delivered():
    simulator = Simulator(dc_voltage=2.0, time_scale=1, sleep=SleepRecorder())
    simulator.timeout = 5.0

    assert simulator.query("READ?") == "+2.00000000E+00"


def _simulator(function: Function | None = None, **kwargs: Any) -> Simulator:
    simulator = Simulator(**kwargs)
    if function is not None:
        simulator.write(f'FUNC "{function.value}"')
    return simulator


def _read(simulator: Simulator) -> float:
    return float(simulator.query("READ?"))


def _errors(simulator: Simulator) -> list[str]:
    found: list[str] = []
    while (reply := simulator.query("SYST:ERR?")) != '+0,"No error"':
        found.append(reply)
    return found


@pytest.mark.parametrize(
    ("function", "name"),
    [
        (Function.DC_VOLTAGE, '"VOLT"'),
        (Function.AC_VOLTAGE, '"VOLT:AC"'),
        (Function.DC_CURRENT, '"CURR"'),
        (Function.AC_CURRENT, '"CURR:AC"'),
        (Function.RESISTANCE_2W, '"RES"'),
        (Function.RESISTANCE_4W, '"FRES"'),
        (Function.FREQUENCY, '"FREQ"'),
        (Function.PERIOD, '"PER"'),
        (Function.CONTINUITY, '"CONT"'),
        (Function.DIODE, '"DIOD"'),
        (Function.DC_VOLTAGE_RATIO, '"VOLT:RAT"'),
    ],
)
def test_every_function_can_be_selected_and_is_reported_back_by_name(function, name):
    simulator = _simulator(function)

    assert simulator.query("FUNC?") == name
    assert _errors(simulator) == []


def test_a_freshly_powered_meter_measures_dc_voltage():
    assert Simulator().query("FUNC?") == '"VOLT"'


@pytest.mark.parametrize("spelling", ['FUNC "VOLT"', "func 'volt:dc'", 'FUNCTION "VOLT:DC"', 'FUNC "VOLTAGE:DC"'])
def test_function_names_may_be_written_in_the_forms_the_meter_accepts(spelling):
    simulator = _simulator(Function.PERIOD)

    simulator.write(spelling)

    assert simulator.query("FUNC?") == '"VOLT"'


def test_an_unknown_function_is_an_illegal_parameter_value_and_changes_nothing():
    simulator = _simulator(Function.RESISTANCE_2W)

    simulator.write('FUNC "BOGUS"')

    assert simulator.query("FUNC?") == '"RES"'
    assert _errors(simulator) == ['-224,"Illegal parameter value"']


@pytest.mark.parametrize(
    ("function", "expected"),
    [
        (Function.DC_VOLTAGE, 1.0),
        (Function.AC_VOLTAGE, 1.0),
        (Function.DC_CURRENT, 0.001),
        (Function.AC_CURRENT, 0.001),
        (Function.RESISTANCE_2W, 1000.0),
        (Function.RESISTANCE_4W, 1000.0),
        (Function.FREQUENCY, 1000.0),
        (Function.PERIOD, 0.001),
        (Function.CONTINUITY, 0.5),
        (Function.DIODE, 0.6),
        (Function.DC_VOLTAGE_RATIO, 1.0),
    ],
)
def test_each_function_reads_its_own_applied_signal(function, expected):
    simulator = _simulator(function)

    assert _read(simulator) == pytest.approx(expected)


def test_applied_signals_can_be_chosen_per_function():
    simulator = Simulator(signals={Function.RESISTANCE_2W: 4700.0, Function.DC_VOLTAGE: 3.3})

    assert _read(simulator) == pytest.approx(3.3)
    simulator.write('FUNC "RES"')
    assert _read(simulator) == pytest.approx(4700.0)


def test_dc_voltage_shortcut_sets_the_dc_voltage_signal():
    assert _read(Simulator(dc_voltage=2.5)) == pytest.approx(2.5)


@pytest.mark.parametrize(
    ("prefix", "function", "ranges"),
    [
        ("VOLT:DC", Function.DC_VOLTAGE, (0.1, 1, 10, 100, 1000)),
        ("VOLT:AC", Function.AC_VOLTAGE, (0.1, 1, 10, 100, 750)),
        ("CURR:DC", Function.DC_CURRENT, (0.01, 0.1, 1, 3)),
        ("CURR:AC", Function.AC_CURRENT, (1, 3)),
        ("RES", Function.RESISTANCE_2W, (100, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8)),
        ("FRES", Function.RESISTANCE_4W, (100, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8)),
        ("FREQ:VOLT", Function.FREQUENCY, (0.1, 1, 10, 100, 750)),
        ("PER:VOLT", Function.PERIOD, (0.1, 1, 10, 100, 750)),
    ],
)
def test_each_function_has_its_range_table_and_starts_in_autorange(prefix, function, ranges):
    simulator = _simulator(function)

    assert simulator.query(f"{prefix}:RANG:AUTO?") == "1"
    for range_value in ranges:
        simulator.write(f"{prefix}:RANG {range_value:g}")
        assert float(simulator.query(f"{prefix}:RANG?")) == pytest.approx(range_value)
        assert simulator.query(f"{prefix}:RANG:AUTO?") == "0"
    assert _errors(simulator) == []


def test_range_query_uses_the_meters_number_format():
    simulator = Simulator()

    simulator.write("VOLT:DC:RANG 10")

    assert simulator.query("VOLT:DC:RANG?") == "+1.00000000E+01"


def test_autorange_can_be_switched_off_and_on_with_on_off_or_one_zero():
    simulator = Simulator()

    simulator.write("VOLT:DC:RANG:AUTO OFF")
    assert simulator.query("VOLT:DC:RANG:AUTO?") == "0"
    simulator.write("VOLT:DC:RANG:AUTO 1")
    assert simulator.query("VOLT:DC:RANG:AUTO?") == "1"
    simulator.write("VOLT:DC:RANG 1")
    simulator.write("VOLT:DC:RANG:AUTO ON")
    assert simulator.query("VOLT:DC:RANG:AUTO?") == "1"
    simulator.write("VOLT:DC:RANG:AUTO 0")
    assert simulator.query("VOLT:DC:RANG:AUTO?") == "0"
    assert _errors(simulator) == []


def test_a_range_between_two_ranges_rounds_up_to_the_next_one():
    simulator = Simulator()

    simulator.write("VOLT:DC:RANG 5")

    assert float(simulator.query("VOLT:DC:RANG?")) == 10
    assert _errors(simulator) == []


def test_a_range_larger_than_any_the_function_has_is_out_of_range():
    simulator = Simulator()
    simulator.write("VOLT:DC:RANG 1")

    simulator.write("VOLT:DC:RANG 5000")

    assert float(simulator.query("VOLT:DC:RANG?")) == 1
    assert _errors(simulator) == ['-222,"Data out of range"']


@pytest.mark.parametrize(("word", "expected"), [("MIN", 0.1), ("MAX", 1000.0)])
def test_range_accepts_min_and_max(word, expected):
    simulator = Simulator()

    simulator.write(f"VOLT:DC:RANG {word}")

    assert float(simulator.query("VOLT:DC:RANG?")) == expected


def test_a_range_that_is_not_a_number_is_an_illegal_parameter_value():
    simulator = Simulator()

    simulator.write("VOLT:DC:RANG lots")

    assert _errors(simulator) == ['-224,"Illegal parameter value"']


def test_a_command_with_a_missing_parameter_is_reported():
    simulator = Simulator()

    simulator.write("VOLT:DC:RANG")

    assert _errors(simulator) == ['-109,"Missing parameter"']


def test_range_settings_belong_to_their_own_function():
    simulator = Simulator()
    simulator.write("VOLT:DC:RANG 1")
    simulator.write("VOLT:AC:RANG 100")

    assert float(simulator.query("VOLT:DC:RANG?")) == 1
    assert float(simulator.query("VOLT:AC:RANG?")) == 100
    assert simulator.query("CURR:DC:RANG:AUTO?") == "1"


def test_the_dc_voltage_ratio_function_uses_the_dc_voltage_range_and_integration_time():
    simulator = Simulator()
    simulator.write("VOLT:DC:RANG 10")
    simulator.write("VOLT:DC:NPLC 1")

    simulator.write('FUNC "VOLT:DC:RAT"')

    assert float(simulator.query("VOLT:DC:RANG?")) == 10
    assert _read(simulator) == pytest.approx(1.0)


@pytest.mark.parametrize("prefix", ["VOLT:DC", "CURR:DC", "RES", "FRES"])
def test_integration_time_starts_at_10_nplc_and_accepts_the_five_values(prefix):
    simulator = Simulator()

    assert float(simulator.query(f"{prefix}:NPLC?")) == 10
    for nplc in (0.02, 0.2, 1, 10, 100):
        simulator.write(f"{prefix}:NPLC {nplc:g}")
        assert float(simulator.query(f"{prefix}:NPLC?")) == pytest.approx(nplc)
    assert _errors(simulator) == []


def test_integration_time_query_uses_the_meters_number_format():
    assert Simulator().query("VOLT:DC:NPLC?") == "+1.00000000E+01"


@pytest.mark.parametrize(("word", "expected"), [("MIN", 0.02), ("MAX", 100), ("DEF", 10)])
def test_integration_time_accepts_min_max_and_def(word, expected):
    simulator = Simulator()

    simulator.write(f"VOLT:DC:NPLC {word}")

    assert float(simulator.query("VOLT:DC:NPLC?")) == expected


def test_an_integration_time_the_meter_does_not_have_is_an_illegal_parameter_value():
    simulator = Simulator()

    simulator.write("VOLT:DC:NPLC 5")

    assert float(simulator.query("VOLT:DC:NPLC?")) == 10
    assert _errors(simulator) == ['-224,"Illegal parameter value"']


@pytest.mark.parametrize("prefix", ["VOLT:AC", "CURR:AC", "FREQ", "PER", "CONT", "DIOD"])
def test_functions_without_an_integration_time_reject_the_command(prefix):
    simulator = Simulator()

    simulator.write(f"{prefix}:NPLC 1")

    assert _errors(simulator) == ['-113,"Undefined header"']


def test_long_form_sense_headers_are_understood():
    simulator = Simulator()

    simulator.write("SENSE:VOLTAGE:DC:RANGE 10")
    simulator.write(":SENS:VOLT:DC:NPLCYCLES 1")

    assert float(simulator.query("SENS:VOLT:DC:RANG?")) == 10
    assert float(simulator.query("VOLT:DC:NPLC?")) == 1


def test_reset_returns_every_setting_to_its_default():
    simulator = Simulator()
    simulator.write('FUNC "RES"')
    simulator.write("RES:RANG 1000")
    simulator.write("VOLT:DC:NPLC 100")

    simulator.write("*RST")

    assert simulator.query("FUNC?") == '"VOLT"'
    assert simulator.query("RES:RANG:AUTO?") == "1"
    assert float(simulator.query("VOLT:DC:NPLC?")) == 10


def test_autorange_selects_the_lowest_range_that_holds_the_signal():
    # Each range reads up to 120 % of full scale, so 1.1 V still fits the 1 V range.
    for volts, resolution in ((0.05, 1e-8), (1.1, 1e-6), (7.0, 1e-5), (90.0, 1e-4), (700.0, 1e-3)):
        reading = _read(Simulator(dc_voltage=volts))
        assert reading == pytest.approx(volts, abs=resolution)


@pytest.mark.parametrize(
    ("volts", "range_value", "expected"),
    [
        (1.23456789, 10, 1.23457),
        (0.98765432, 1, 0.987654),
        (0.0987654321, 0.1, 0.0987654),
        (1.23456789, 1000, 1.235),
    ],
)
def test_readings_are_rounded_to_the_resolution_of_the_range_in_use(volts, range_value, expected):
    simulator = Simulator(dc_voltage=volts)
    simulator.write(f"VOLT:DC:RANG {range_value}")

    assert _read(simulator) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(
    ("nplc", "expected"),
    [(0.02, 0.9123), (0.2, 0.91235), (1, 0.91235), (10, 0.912346), (100, 0.912346)],
)
def test_integration_time_decides_how_many_digits_a_reading_has(nplc, expected):
    simulator = Simulator(dc_voltage=0.91234567)
    simulator.write("VOLT:DC:RANG 1")
    simulator.write(f"VOLT:DC:NPLC {nplc}")

    assert _read(simulator) == pytest.approx(expected, abs=1e-12)


def test_overload_in_a_fixed_range_starts_beyond_120_percent_of_the_range():
    simulator = Simulator(dc_voltage=1.19)
    simulator.write("VOLT:DC:RANG 1")
    assert _read(simulator) == pytest.approx(1.19)

    simulator = Simulator(dc_voltage=1.21)
    simulator.write("VOLT:DC:RANG 1")
    assert simulator.query("READ?") == "+9.90000000E+37"


def test_the_1000_volt_range_has_no_over_range():
    assert Simulator(dc_voltage=1000.0).query("READ?") == "+1.00000000E+03"
    assert Simulator(dc_voltage=1001.0).query("READ?") == "+9.90000000E+37"
    assert Simulator(dc_voltage=-1001.0).query("READ?") == "-9.90000000E+37"


@pytest.mark.parametrize(
    ("function", "limit"),
    [(Function.AC_VOLTAGE, 750.0), (Function.DC_CURRENT, 3.0), (Function.AC_CURRENT, 3.0)],
)
def test_the_750_volt_and_3_amp_ranges_have_no_over_range(function, limit):
    simulator = Simulator(signals={function: limit})
    simulator.write(f'FUNC "{function.value}"')
    assert simulator.query("READ?") == f"{limit:+.8E}"

    simulator = Simulator(signals={function: limit * 1.01})
    simulator.write(f'FUNC "{function.value}"')
    assert simulator.query("READ?") == "+9.90000000E+37"


def test_other_ranges_read_up_to_120_percent():
    simulator = Simulator(signals={Function.AC_CURRENT: 1.19})
    simulator.write('FUNC "CURR:AC"')
    simulator.write("CURR:AC:RANG 1")

    assert float(simulator.query("READ?")) == pytest.approx(1.19)


@pytest.mark.parametrize(
    ("function", "signal"),
    [
        (Function.AC_VOLTAGE, 760.0),
        (Function.DC_CURRENT, 4.0),
        (Function.AC_CURRENT, 4.0),
        (Function.RESISTANCE_2W, 130e6),
        (Function.RESISTANCE_4W, 130e6),
        (Function.CONTINUITY, 1300.0),
        (Function.DIODE, 1.3),
    ],
)
def test_every_function_overloads_when_the_signal_exceeds_its_ranges(function, signal):
    simulator = Simulator(signals={function: signal})
    simulator.write(f'FUNC "{function.value}"')

    assert simulator.query("READ?") == "+9.90000000E+37"


def test_a_reading_in_the_current_function_is_rounded_to_its_digits():
    simulator = Simulator(signals={Function.FREQUENCY: 1234.56789})
    simulator.write('FUNC "FREQ"')

    assert _read(simulator) == pytest.approx(1234.57, abs=1e-9)


@pytest.mark.parametrize(
    ("function", "nplc", "expected_seconds"),
    [
        (Function.DC_VOLTAGE, 100, 4.0),
        (Function.DC_VOLTAGE, 0.02, 0.0008),
        (Function.RESISTANCE_2W, 10, 0.4),
        (Function.DC_VOLTAGE_RATIO, 10, 0.8),
    ],
)
def test_a_reading_takes_as_long_as_the_integration_time_says(function, nplc, expected_seconds):
    sleep = SleepRecorder()
    simulator = Simulator(time_scale=1, sleep=sleep)
    simulator.timeout = 60
    simulator.write(f'FUNC "{function.value}"')
    simulator.write(f"{'VOLT:DC' if function is Function.DC_VOLTAGE_RATIO else function.value}:NPLC {nplc}")

    simulator.query("READ?")

    assert sleep.calls == [pytest.approx(expected_seconds)]


def test_an_unknown_common_command_is_an_undefined_header():
    simulator = Simulator()

    simulator.write("*FOO")

    assert _errors(simulator) == ['-113,"Undefined header"']


def test_func_without_a_name_is_a_missing_parameter():
    simulator = Simulator()

    simulator.write("FUNC")

    assert _errors(simulator) == ['-109,"Missing parameter"']
    assert simulator.query("FUNC?") == '"VOLT"'


def test_range_def_returns_to_autorange():
    simulator = Simulator()
    simulator.write("VOLT:DC:RANG 10")

    simulator.write("VOLT:DC:RANG DEF")

    assert simulator.query("VOLT:DC:RANG:AUTO?") == "1"


def test_switching_autorange_off_holds_the_range_it_had_chosen_and_off_again_changes_nothing():
    simulator = Simulator(dc_voltage=5.0)

    simulator.write("VOLT:DC:RANG:AUTO OFF")
    simulator.write("VOLT:DC:RANG:AUTO OFF")

    assert float(simulator.query("VOLT:DC:RANG?")) == 10
    assert simulator.query("VOLT:DC:RANG:AUTO?") == "0"


def test_an_autorange_flag_that_is_neither_on_nor_off_is_an_illegal_parameter_value():
    simulator = Simulator()

    simulator.write("VOLT:DC:RANG:AUTO maybe")

    assert _errors(simulator) == ['-224,"Illegal parameter value"']
    assert simulator.query("VOLT:DC:RANG:AUTO?") == "1"


def _has_reply(simulator: Simulator) -> bool:
    return simulator.has_reply  # a function, so the type checker does not assume the answer cannot change


def test_a_query_leaves_a_reply_waiting_until_it_is_read():
    simulator = Simulator()
    assert not _has_reply(simulator)

    simulator.write("*IDN?")
    assert _has_reply(simulator)

    simulator.read()
    assert not _has_reply(simulator)


@pytest.mark.parametrize("command", ["*CLS", "*RST", 'FUNC "RES"', "BOGUS", "BOGUS?", "VOLT:DC:RANG 10"])
def test_commands_that_have_no_answer_leave_no_reply_waiting(command):
    simulator = Simulator()

    simulator.write(command)

    assert not _has_reply(simulator)
