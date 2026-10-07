import pytest

from agilent34401a.errors import TransportTimeoutError
from agilent34401a.sim import Simulator

STATUS_ERROR_BIT = 4


def is_locked(simulator: Simulator) -> bool:
    return simulator.front_panel_locked


def test_status_byte_has_the_error_bit_only_while_the_error_queue_holds_something():
    simulator = Simulator()
    assert int(simulator.query("*STB?")) & STATUS_ERROR_BIT == 0

    simulator.write("NOT:A:COMMAND")
    assert int(simulator.query("*STB?")) & STATUS_ERROR_BIT == STATUS_ERROR_BIT

    simulator.query("SYST:ERR?")
    assert int(simulator.query("*STB?")) & STATUS_ERROR_BIT == 0


def test_simulator_reports_its_scpi_version():
    assert Simulator().query("SYST:VERS?") == "1994.0"


def test_self_test_passes_by_default_and_takes_about_ten_seconds():
    slept: list[float] = []
    simulator = Simulator(time_scale=1, sleep=slept.append)
    simulator.timeout = 30.0

    assert simulator.query("*TST?") == "+0"
    assert slept == [10.0]


def test_self_test_is_too_slow_for_the_default_timeout():
    simulator = Simulator(time_scale=1, sleep=lambda _seconds: None)

    with pytest.raises(TransportTimeoutError):
        simulator.query("*TST?")


def test_a_failing_self_test_answers_one_and_queues_an_error():
    simulator = Simulator()
    simulator.self_test_passes = False

    assert simulator.query("*TST?") == "+1"
    assert simulator.query("SYST:ERR?").startswith("-330")


def test_beeper_is_on_until_switched_off_and_a_test_beep_is_counted():
    simulator = Simulator()
    assert simulator.query("SYST:BEEP:STAT?") == "1"

    simulator.write("SYST:BEEP:STAT OFF")
    assert simulator.query("SYST:BEEP:STAT?") == "0"

    simulator.write("SYST:BEEP")
    assert simulator.beeps == 1


def test_display_text_can_be_set_read_back_and_cleared():
    simulator = Simulator()
    assert simulator.query("DISP:TEXT?") == '""'

    simulator.write('DISP:TEXT "HELLO BENCH"')
    assert simulator.query("DISP:TEXT?") == '"HELLO BENCH"'

    simulator.write("DISP:TEXT:CLE")
    assert simulator.query("DISP:TEXT?") == '""'


def test_display_text_keeps_a_doubled_quote_as_one_quote():
    simulator = Simulator()

    simulator.write('DISP:TEXT "A""B"')

    assert simulator.display_text == 'A"B'
    assert simulator.query("DISP:TEXT?") == '"A""B"'


def test_display_can_be_switched_off_and_on():
    simulator = Simulator()
    assert simulator.query("DISP?") == "1"

    simulator.write("DISP OFF")
    assert simulator.query("DISP?") == "0"

    simulator.write("DISP ON")
    assert simulator.query("DISP?") == "1"


def test_reset_restores_the_display_and_clears_the_text_but_keeps_the_beeper_choice():
    simulator = Simulator()
    simulator.write("DISP OFF")
    simulator.write('DISP:TEXT "X"')
    simulator.write("SYST:BEEP:STAT OFF")

    simulator.write("*RST")

    assert simulator.query("DISP?") == "1"
    assert simulator.query("DISP:TEXT?") == '""'
    assert simulator.query("SYST:BEEP:STAT?") == "0"


def test_calibration_count_and_message_can_be_read():
    simulator = Simulator(calibration_count=7, calibration_message="CAL 2026-01-01")

    assert simulator.query("CAL:COUN?") == "+7"
    assert simulator.query("CAL:STR?") == '"CAL 2026-01-01"'


def test_the_simulator_refuses_to_write_calibration_data():
    simulator = Simulator(calibration_count=7, calibration_message="KEEP")

    simulator.write('CAL:STR "CHANGED"')
    simulator.write("CAL:VAL 1")

    assert simulator.calibration_message == "KEEP"
    assert simulator.query("SYST:ERR?").startswith("-")


def test_front_panel_lockout_is_set_by_rwlock_and_released_by_local():
    simulator = Simulator()
    assert not is_locked(simulator)

    simulator.write("SYST:RWL")
    assert is_locked(simulator)

    simulator.write("SYST:LOC")
    assert not is_locked(simulator)


def test_the_simulator_rejects_a_switch_or_message_it_cannot_understand():
    simulator = Simulator()

    simulator.write("DISP SIDEWAYS")
    simulator.write("DISP:TEXT HELLO")

    assert simulator.query("SYST:ERR?").startswith("-224")
    assert simulator.query("SYST:ERR?").startswith("-224")
    assert simulator.query("SYST:ERR?").startswith("+0")
    assert simulator.display_text == ""
