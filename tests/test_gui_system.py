# ruff: noqa: F811 - the shared GUI fixtures are imported from test_gui_app and used as test arguments
import threading
import tkinter as tk
from collections.abc import Callable

from agilent34401a.gui.main_window import MainWindow
from agilent34401a.gui.system_tab import RESET_QUESTION, SELF_TEST_RUNNING, is_checked
from agilent34401a.meter import Function, Setup
from agilent34401a.settings import Settings
from agilent34401a.sim import AGILENT_IDENTITY, Simulator
from tests.test_gui_app import make_window, pump, pump_for, tk_root  # noqa: F401 - the shared fixtures

TIMEOUT_S = 10.0


class SlowSelfTestSimulator(Simulator):
    """A Simulator whose Readings are instant but whose self-test takes `scale` times as long as the real one."""

    def __init__(self, scale: float, sleep) -> None:
        super().__init__(time_scale=0, sleep=sleep)
        self._scale = scale

    def query(self, command: str) -> str:
        self.time_scale = self._scale if command == "*TST?" else 0.0
        return super().query(command)


class LoggingSimulator(Simulator):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)
        super().write(command)


def system_ready(window: MainWindow) -> bool:
    """The tab has been told who the Meter is and has read its system information."""
    return str(window.system_tab.scpi_label.cget("text")) != "—"


def open_system_tab(make_window: Callable[..., MainWindow], simulator: Simulator | None = None) -> MainWindow:
    window: MainWindow = make_window(simulator or Simulator(identity=AGILENT_IDENTITY))
    pump(window, lambda: system_ready(window))
    return window


def test_system_tab_is_one_of_the_tabs(make_window):
    window = make_window()

    assert str(window.system_tab) in window.notebook.tabs()
    assert window.notebook.tab(window.system_tab, "text") == "System"


def test_system_tab_shows_identity_firmware_and_scpi_version(make_window):
    window = open_system_tab(make_window, Simulator(identity=AGILENT_IDENTITY))
    tab = window.system_tab

    assert tab.manufacturer_label.cget("text") == "Agilent Technologies"
    assert tab.model_label.cget("text") == "34401A"
    assert tab.serial_label.cget("text") == "MY45000001"
    assert tab.firmware_label.cget("text") == "11-5-2"
    assert tab.scpi_label.cget("text") == "1994.0"


def test_system_tab_shows_the_calibration_count_and_message_and_offers_no_way_to_change_them(make_window):
    window = open_system_tab(make_window, Simulator(calibration_count=4, calibration_message="CAL 2026-01-01"))
    tab = window.system_tab

    assert tab.calibration_count_label.cget("text") == "4"
    assert tab.calibration_message_label.cget("text") == "CAL 2026-01-01"
    assert tab.calibration_count_label.winfo_class() == "TLabel"
    assert tab.calibration_message_label.winfo_class() == "TLabel"


def test_controls_wait_for_the_connection(make_window):
    window = make_window()

    assert str(window.system_tab.reset_button.cget("state")) == "disabled"

    pump(window, lambda: system_ready(window))

    assert str(window.system_tab.reset_button.cget("state")) == "normal"


def test_reset_asks_first_and_does_nothing_when_the_user_says_no(make_window):
    simulator = LoggingSimulator()
    window = open_system_tab(make_window, simulator)
    seen: list[str] = []

    def decline(_title: str, message: str) -> bool:
        seen.append(message)
        return False

    window.system_tab.confirm = decline
    window.worker.apply_setup(Setup.default(Function.RESISTANCE_2W))
    pump(window, lambda: window.function_label.cget("text") == Function.RESISTANCE_2W.label)

    window.system_tab.reset_button.invoke()
    pump_for(window, 0.2)

    assert seen == [RESET_QUESTION]
    assert "*RST" not in simulator.writes
    assert window.function_label.cget("text") == Function.RESISTANCE_2W.label


def test_reset_after_confirmation_returns_the_meter_to_its_power_on_setup(make_window):
    window = open_system_tab(make_window)
    window.system_tab.confirm = lambda _title, _message: True
    window.worker.apply_setup(Setup.default(Function.RESISTANCE_2W))
    pump(window, lambda: window.function_label.cget("text") == Function.RESISTANCE_2W.label)

    window.system_tab.reset_button.invoke()

    pump(window, lambda: window.system_tab.reset_result.cget("text") == "Meter reset")
    pump(window, lambda: window.function_label.cget("text") == Function.DC_VOLTAGE.label)


def test_self_test_reports_a_pass_and_disables_itself_while_it_runs(make_window):
    release = threading.Event()
    entered = threading.Event()

    def slow(_seconds: float) -> None:
        entered.set()
        release.wait(TIMEOUT_S)

    window = open_system_tab(make_window, SlowSelfTestSimulator(1, slow))
    tab = window.system_tab

    tab.self_test_button.invoke()
    pump(window, entered.is_set)  # the Worker is inside the ten second test, and Tk is still being pumped

    assert tab.self_test_result.cget("text") == SELF_TEST_RUNNING
    assert str(tab.self_test_button.cget("state")) == "disabled"
    assert str(tab.reset_button.cget("state")) == "disabled"
    release.set()

    pump(window, lambda: tab.self_test_result.cget("text") == "Self-test passed")
    assert str(tab.self_test_button.cget("state")) == "normal"


def test_self_test_reports_a_failure_and_logs_the_errors_the_meter_queued(make_window):
    simulator = Simulator()
    simulator.self_test_passes = False
    window = open_system_tab(make_window, simulator)

    window.system_tab.self_test_button.invoke()

    pump(window, lambda: window.system_tab.self_test_result.cget("text") == "Self-test FAILED")
    pump(window, lambda: len(window.system_tab.error_tree.get_children()) == 1)
    tree = window.system_tab.error_tree
    assert str(tree.set(tree.get_children()[0])["code"]) == "-330"


def test_a_self_test_the_meter_never_finishes_is_reported_and_the_buttons_come_back(make_window):
    window = open_system_tab(make_window, SlowSelfTestSimulator(100, lambda _seconds: None))
    tab = window.system_tab

    tab.self_test_button.invoke()

    pump(window, lambda: "Self-test failed" in str(tab.problem_label.cget("text")))
    assert tab.self_test_result.cget("text") == ""
    assert str(tab.self_test_button.cget("state")) == "normal"


def test_beeper_switch_and_test_beep(make_window):
    simulator = Simulator()
    window = open_system_tab(make_window, simulator)
    tab = window.system_tab
    assert is_checked(tab.beeper_check) is True

    tab.beeper_check.invoke()
    pump(window, lambda: simulator.beeper_enabled is False)

    tab.beep_button.invoke()
    pump(window, lambda: simulator.beeps == 1)
    assert is_checked(tab.beeper_check) is False


def test_display_message_is_shown_and_cleared(make_window):
    simulator = Simulator()
    window = open_system_tab(make_window, simulator)
    tab = window.system_tab

    tab.message_entry.insert(0, "HELLO")
    tab.show_message_button.invoke()
    pump(window, lambda: simulator.display_text == "HELLO")

    tab.clear_message_button.invoke()
    pump(window, lambda: simulator.display_text == "")
    assert tab.message_entry.get() == ""


def test_display_message_entry_stops_at_what_the_display_can_hold(make_window):
    window = open_system_tab(make_window)
    entry = window.system_tab.message_entry

    entry.insert(0, "ABCDEFGHIJKLMNOP")

    assert entry.get() == ""  # tk rejects the whole insert when it would be too long
    entry.insert(0, "ABCDEFGHIJKL")
    assert entry.get() == "ABCDEFGHIJKL"


def test_display_can_be_switched_off_and_on(make_window):
    simulator = Simulator()
    window = open_system_tab(make_window, simulator)

    window.system_tab.display_check.invoke()
    pump(window, lambda: simulator.display_on is False)

    window.system_tab.display_check.invoke()
    pump(window, lambda: simulator.display_on is True)


def test_lockout_is_optional_and_can_be_released(make_window):
    simulator = Simulator()
    window = open_system_tab(make_window, simulator)
    tab = window.system_tab
    assert is_checked(tab.lockout_check) is False

    tab.lockout_check.invoke()
    pump(window, lambda: simulator.front_panel_locked)

    tab.lockout_check.invoke()
    pump(window, lambda: not simulator.front_panel_locked)


def test_every_meter_error_is_logged_with_a_timestamp(make_window):
    simulator = Simulator()
    window = open_system_tab(make_window, simulator)
    tab = window.system_tab
    simulator.write("BAD:COMMAND")

    tab.check_errors_button.invoke()

    pump(window, lambda: len(tab.error_tree.get_children()) == 1)
    row = tab.error_tree.set(tab.error_tree.get_children()[0])
    assert (str(row["code"]), row["message"]) == ("-113", "Undefined header")
    assert len(str(row["time"])) == len("2026-10-08 12:00:00")


def test_errors_the_meter_queues_during_a_setup_change_reach_the_log(make_window):
    simulator = Simulator()
    window = open_system_tab(make_window, simulator)
    tab = window.system_tab
    simulator.write("BAD:COMMAND")

    window.worker.apply_setup(Setup.default(Function.DC_VOLTAGE))

    pump(window, lambda: len(tab.error_tree.get_children()) == 1)


def test_clearing_the_log_empties_it(make_window):
    simulator = Simulator()
    window = open_system_tab(make_window, simulator)
    tab = window.system_tab
    simulator.write("BAD:COMMAND")
    tab.check_errors_button.invoke()
    pump(window, lambda: len(tab.error_tree.get_children()) == 1)

    tab.clear_log_button.invoke()

    assert tab.error_tree.get_children() == ()


def test_controls_are_disabled_when_the_connection_ends(make_window):
    window = open_system_tab(make_window)

    window.worker.shutdown()

    pump(window, lambda: str(window.system_tab.reset_button.cget("state")) == "disabled")


def test_the_window_checks_for_meter_errors_at_the_interval_in_the_settings(tk_root):
    settings = Settings.in_memory()
    settings.error_check_interval_s = 7.5
    window = MainWindow(tk.Toplevel(tk_root), Simulator, "Simulator", settings=settings)
    try:
        assert window.worker.error_check_interval_s == 7.5
    finally:
        window.close()
