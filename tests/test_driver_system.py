import pytest

from agilent34401a.driver import Driver, SystemInfo
from agilent34401a.errors import MalformedReplyError, TransportTimeoutError
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import Simulator


def is_locked(simulator: Simulator) -> bool:
    return simulator.front_panel_locked


class RecordingSimulator(Simulator):
    """A Simulator that remembers every command written to it and the timeout in force for each query."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.commands: list[str] = []
        self.timeouts: dict[str, float] = {}

    def write(self, command: str) -> None:
        self.commands.append(command)
        self.timeouts[command] = self.timeout
        super().write(command)


class BusLockoutSimulator(RecordingSimulator):
    """A Simulator whose Connection can send the bus-level local lockout, as GPIB can."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.lockout_calls: list[bool] = []

    def set_local_lockout(self, *, locked: bool) -> bool:
        self.lockout_calls.append(locked)
        self.front_panel_locked = locked
        return True


def test_driver_reads_the_system_information():
    simulator = Simulator(calibration_count=12, calibration_message="CAL 2026")
    simulator.write("SYST:BEEP:STAT OFF")
    simulator.write('DISP:TEXT "HI"')

    info = Driver(simulator).read_system()

    assert info == SystemInfo(
        scpi_version="1994.0",
        calibration_count=12,
        calibration_message="CAL 2026",
        beeper_enabled=False,
        display_on=True,
        display_text="HI",
    )


def test_driver_reports_a_reply_that_is_not_a_calibration_count():
    class Garbled(Simulator):
        def query(self, command: str) -> str:
            return "many" if command == "CAL:COUN?" else super().query(command)

    with pytest.raises(MalformedReplyError):
        Driver(Garbled()).read_system()


def test_driver_never_writes_to_the_meters_calibration():
    simulator = RecordingSimulator()
    driver = Driver(simulator)

    driver.read_system()
    driver.reset()
    driver.self_test()
    driver.set_beeper(enabled=False)
    driver.beep()
    driver.set_display_text("HELLO")
    driver.set_display_text(None)
    driver.set_display(on=False)
    driver.lock_front_panel()
    driver.unlock_front_panel()

    writes = [command for command in simulator.commands if not command.endswith("?")]
    assert not [command for command in writes if command.upper().startswith("CAL:")]
    assert simulator.query("SYST:ERR?").startswith("+0")


def test_reset_sends_rst_and_leaves_the_driver_expecting_a_default_setup():
    simulator = RecordingSimulator()
    driver = Driver(simulator)
    driver.apply(Setup.default(Function.RESISTANCE_2W))

    errors = driver.reset()

    assert "*RST" in simulator.commands
    assert errors == []
    assert driver.setup == Setup.default(Function.DC_VOLTAGE)


def test_self_test_passes_on_the_simulator_and_gives_the_meter_time_to_finish():
    simulator = RecordingSimulator(time_scale=1, sleep=lambda _seconds: None)
    driver = Driver(simulator)

    assert driver.self_test() is True
    assert simulator.timeouts["*TST?"] > 10.0
    assert simulator.timeout == 2.0  # the timeout in force before is back afterwards


def test_self_test_reports_a_failure_and_restores_the_timeout():
    simulator = RecordingSimulator()
    simulator.self_test_passes = False
    driver = Driver(simulator)

    assert driver.self_test() is False
    assert simulator.timeout == 2.0


def test_self_test_restores_the_timeout_when_the_meter_never_answers():
    simulator = Simulator(time_scale=100, sleep=lambda _seconds: None)  # 1000 s, beyond any allowance
    driver = Driver(simulator)

    with pytest.raises(TransportTimeoutError):
        driver.self_test()

    assert simulator.timeout == 2.0


def test_self_test_rejects_a_reply_that_is_not_a_result():
    class Garbled(Simulator):
        def query(self, command: str) -> str:
            return "maybe" if command == "*TST?" else super().query(command)

    with pytest.raises(MalformedReplyError):
        Driver(Garbled()).self_test()


def test_error_check_drains_the_queue_only_when_the_status_byte_says_it_holds_something():
    simulator = RecordingSimulator()
    driver = Driver(simulator)

    assert driver.errors_if_flagged() == []
    assert "SYST:ERR?" not in simulator.commands

    simulator.write("BAD:COMMAND")
    errors = driver.errors_if_flagged()

    assert [error.code for error in errors] == [-113]
    assert driver.errors_if_flagged() == []


def test_status_byte_must_be_a_number():
    class Garbled(Simulator):
        def query(self, command: str) -> str:
            return "x" if command == "*STB?" else super().query(command)

    with pytest.raises(MalformedReplyError):
        Driver(Garbled()).errors_if_flagged()


def test_beeper_and_test_beep_are_sent():
    simulator = RecordingSimulator()
    driver = Driver(simulator)

    driver.set_beeper(enabled=False)
    driver.beep()

    assert simulator.beeper_enabled is False
    assert simulator.beeps == 1


def test_display_text_is_quoted_and_a_quote_inside_it_is_doubled():
    simulator = RecordingSimulator()
    driver = Driver(simulator)

    driver.set_display_text('A"B')

    assert 'DISP:TEXT "A""B"' in simulator.commands
    assert simulator.display_text == 'A"B'


def test_display_text_none_clears_the_message():
    simulator = Simulator()
    driver = Driver(simulator)
    driver.set_display_text("HELLO")

    driver.set_display_text(None)

    assert simulator.display_text == ""


def test_display_text_longer_than_the_display_is_refused():
    with pytest.raises(ValueError, match="12"):
        Driver(Simulator()).set_display_text("THIRTEEN CHRS")


def test_display_text_the_display_cannot_show_is_refused():
    with pytest.raises(ValueError, match="characters"):
        Driver(Simulator()).set_display_text("é")


def test_display_can_be_switched_off_and_on():
    simulator = Simulator()
    driver = Driver(simulator)

    driver.set_display(on=False)
    assert simulator.display_on is False

    driver.set_display(on=True)
    assert simulator.display_on is True


def test_lockout_without_a_bus_message_uses_the_rs232_commands():
    simulator = RecordingSimulator()
    driver = Driver(simulator)

    driver.lock_front_panel()
    assert is_locked(simulator)
    assert "SYST:RWL" in simulator.commands

    driver.unlock_front_panel()
    assert not is_locked(simulator)
    assert [c for c in simulator.commands if c != "SYST:ERR?"][-2:] == ["SYST:LOC", "SYST:REM"]


def test_lockout_prefers_the_bus_message_when_the_connection_has_one():
    simulator = BusLockoutSimulator()
    driver = Driver(simulator)

    driver.lock_front_panel()
    driver.unlock_front_panel()

    assert simulator.lockout_calls == [True, False]
    assert "SYST:RWL" not in simulator.commands


def test_driver_reports_a_beeper_state_that_is_not_on_or_off():
    class Garbled(Simulator):
        def query(self, command: str) -> str:
            return "maybe" if command == "SYST:BEEP:STAT?" else super().query(command)

    with pytest.raises(MalformedReplyError):
        Driver(Garbled()).read_system()


def test_driver_accepts_a_string_reply_without_quotes():
    class Unquoted(Simulator):
        def query(self, command: str) -> str:
            return "NO QUOTES" if command == "CAL:STR?" else super().query(command)

    assert Driver(Unquoted()).read_system().calibration_message == "NO QUOTES"
