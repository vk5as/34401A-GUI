from typing import TYPE_CHECKING

import pytest

from agilent34401a.errors import TransportError, TransportTimeoutError
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
    assert Simulator(dc_voltage=1.234567).query("READ?") == "+1.23456700E+00"
    assert Simulator(dc_voltage=-0.0045).query("READ?") == "-4.50000000E-03"


def test_simulated_meter_reports_overload_when_the_applied_voltage_exceeds_the_range():
    assert Simulator(dc_voltage=250.0).query("READ?") == "+9.90000000E+37"
    assert Simulator(dc_voltage=-250.0).query("READ?") == "-9.90000000E+37"


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
