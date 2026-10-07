import gc
import os
import re
import threading
import time
import tkinter as tk
import weakref
from collections.abc import Callable, Iterator
from functools import partial
from tkinter import ttk
from typing import cast

import pytest

from agilent34401a import __version__
from agilent34401a.errors import TransportError
from agilent34401a.gui.app import main
from agilent34401a.gui.main_window import NO_READING, MainWindow
from agilent34401a.meter import Function, GateTime, Resolution
from agilent34401a.settings import Settings, Theme
from agilent34401a.sim import AGILENT_IDENTITY, Simulator
from agilent34401a.transport import Transport

TIMEOUT_S = 10.0


class CountingSimulator(Simulator):
    """A Simulator that counts Readings and can garble chosen ones."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads = 0
        self.garbage_on: set[int] = set()
        self.hold_reading: tuple[int, threading.Event] | None = None
        self.refuse_ranges_of: str | None = None
        self.garbage_function = False

    def write(self, command: str) -> None:
        if self.refuse_ranges_of and not command.endswith("?") and command.startswith(f"{self.refuse_ranges_of}:RANG"):
            self._errors.append('-222,"Data out of range"')
            return
        super().write(command)

    def query(self, command: str) -> str:
        if command == "FUNC?" and self.garbage_function:
            return "garbage"
        if command == "READ?":
            self.reads += 1
            if self.hold_reading and self.hold_reading[0] == self.reads:
                self.hold_reading[1].wait(TIMEOUT_S)
            if self.reads in self.garbage_on:
                return "not a number"
        return super().query(command)


def pump(window: MainWindow, until: Callable[[], bool], timeout: float = TIMEOUT_S) -> None:
    """Run Tk's event loop the way mainloop would, until `until` holds."""
    deadline = time.monotonic() + timeout
    while not until():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting for the window")
        window.root.update()
        time.sleep(0.002)


def pump_for(window: MainWindow, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    pump(window, lambda: time.monotonic() >= deadline)


_TK_START_ATTEMPTS = 3


@pytest.fixture(scope="session")
def tk_root() -> Iterator[tk.Tk]:
    """One Tk interpreter for the whole run, with each test getting its own Toplevel on it.

    Creating a fresh interpreter per test made Windows CI fail now and then with "Can't find a usable init.tcl".
    """
    root = None
    for attempt in range(_TK_START_ATTEMPTS):
        try:
            root = tk.Tk()
            break
        except tk.TclError:
            # CI always has a display (xvfb on Linux), so a missing one there is a failure, not a skip.
            if attempt == _TK_START_ATTEMPTS - 1:
                if os.environ.get("CI"):
                    raise
                pytest.skip("no display available")
            time.sleep(0.5)
    assert root is not None
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def make_window(tk_root):
    windows: list[MainWindow] = []

    def make(simulator: Simulator | None = None, *, opener: Callable[[], Transport] | None = None) -> MainWindow:
        meter = simulator if simulator is not None else Simulator()
        window = MainWindow(tk.Toplevel(tk_root), opener or (lambda: meter), "Simulator")
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.close()


def _exists(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole application is gone
        return False


def connected(window: MainWindow) -> bool:
    return str(window.status_connection.cget("text")).startswith("Connected")


def test_window_is_titled_with_the_version(make_window):
    window = make_window()

    assert __version__ in window.root.title()


def test_status_bar_shows_the_connection_state_resource_and_identity(make_window):
    window = make_window(Simulator(identity=AGILENT_IDENTITY))
    assert window.status_connection.cget("text") == "Connecting…"

    pump(window, lambda: connected(window))

    assert window.status_connection.cget("text") == "Connected · Simulator"
    assert "Agilent Technologies" in window.status_identity.cget("text")
    assert "34401A" in window.status_identity.cget("text")


def test_continuous_readings_update_the_readout_as_soon_as_the_window_is_connected(make_window):
    window = make_window(Simulator(dc_voltage=1.234567))
    assert window.readout.cget("text") == NO_READING

    pump(window, lambda: window.readout.cget("text") != NO_READING)

    assert window.readout.cget("text") == "1.234570 V"
    assert window.function_label.cget("text") == "DC V"


def test_an_overload_reading_is_shown_as_ovld(make_window):
    window = make_window(Simulator(dc_voltage=-5000.0))

    pump(window, lambda: window.readout.cget("text") != NO_READING)

    assert window.readout.cget("text") == "OVLD"


def test_pause_stops_the_readings_and_run_starts_them_again(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: simulator.reads > 0)
    assert window.run_button.cget("text") == "Pause"

    window.run_button.invoke()
    assert window.run_button.cget("text") == "Run"
    pump_for(window, 0.2)  # lets the Reading in progress finish
    paused_at = simulator.reads
    pump_for(window, 0.2)

    assert simulator.reads == paused_at

    window.run_button.invoke()
    assert window.run_button.cget("text") == "Pause"
    pump(window, lambda: simulator.reads > paused_at)


def test_run_and_pause_are_unavailable_until_the_meter_is_connected(make_window):
    window = make_window()

    assert str(window.run_button.cget("state")) == "disabled"

    pump(window, lambda: connected(window))

    assert str(window.run_button.cget("state")) == "normal"


def test_status_bar_shows_the_reading_rate_while_running_and_clears_it_when_paused(make_window):
    window = make_window(Simulator(time_scale=0.1, sleep=time.sleep))  # 40 ms per Reading
    assert window.status_rate.cget("text") == ""

    pump(window, lambda: re.fullmatch(r"\d+\.\d Readings/s", window.status_rate.cget("text")) is not None)

    window.run_button.invoke()
    pump_for(window, 0.3)  # Readings still in flight must not bring the rate back

    assert window.status_rate.cget("text") == ""


def test_window_stays_responsive_while_readings_are_slow(make_window):
    simulator = CountingSimulator(time_scale=1, sleep=time.sleep)  # 400 ms per Reading
    window = make_window(simulator)
    pump(window, lambda: connected(window))

    slowest_update = 0.0
    deadline = time.monotonic() + TIMEOUT_S
    while simulator.reads < 2 and time.monotonic() < deadline:
        started = time.monotonic()
        window.root.update()
        slowest_update = max(slowest_update, time.monotonic() - started)
        time.sleep(0.002)

    assert simulator.reads >= 2
    assert slowest_update < 0.25  # a window blocked on a Reading would stall for the full 400 ms


def test_pausing_while_a_slow_reading_is_in_progress_does_not_block_the_window(make_window):
    simulator = CountingSimulator(time_scale=1, sleep=time.sleep)  # 400 ms per Reading
    window = make_window(simulator)
    pump(window, lambda: simulator.reads > 0)

    started = time.monotonic()
    window.run_button.invoke()
    window.root.update()

    assert time.monotonic() - started < 0.25


def test_a_failed_connection_is_reported_and_leaves_the_window_usable(make_window):
    def refuse():
        message = "no such resource"
        raise TransportError(message)

    window = make_window(opener=refuse)

    pump(window, lambda: window.status_connection.cget("text").startswith("Connection failed"))

    assert window.status_connection.cget("text") == "Connection failed: no such resource"
    assert str(window.run_button.cget("state")) == "disabled"
    assert window.readout.cget("text") == NO_READING


def test_a_device_that_is_not_a_34401a_is_refused(make_window):
    window = make_window(Simulator(identity="Rigol Technologies,DM3058,DM3O123456789,01.01"))

    pump(window, lambda: window.status_connection.cget("text").startswith("Connection failed"))

    assert "Rigol Technologies" in window.status_connection.cget("text")


def test_a_lost_reading_is_reported_in_the_status_bar_and_cleared_by_the_next_one(make_window):
    simulator = CountingSimulator()
    simulator.garbage_on = {2}
    next_reading = threading.Event()
    simulator.hold_reading = (3, next_reading)  # keeps the third Reading back until the lost one has been seen
    window = make_window(simulator)

    pump(window, lambda: "not a number" in window.status_message.cget("text"))
    next_reading.set()
    pump(window, lambda: window.status_message.cget("text") == "")

    assert window.readout.cget("text") == "1.000000 V"


def test_an_unexpected_worker_failure_is_reported_and_stops_the_readings(make_window):
    class Exploding(Simulator):
        def query(self, command: str) -> str:
            if command == "READ?":
                message = "boom"
                raise RuntimeError(message)
            return super().query(command)

    window = make_window(Exploding())

    pump(window, lambda: window.status_connection.cget("text").startswith("Failed"))

    assert "boom" in window.status_connection.cget("text")
    assert str(window.run_button.cget("state")) == "disabled"


def choose(window: MainWindow, box: ttk.Combobox, label: str) -> None:
    """Pick an entry in a combobox the way a user would."""
    box.set(label)
    box.event_generate("<<ComboboxSelected>>")
    window.root.update()


def shows_reading(window: MainWindow) -> bool:
    return str(window.readout.cget("text")) != NO_READING


FUNCTION_LABELS = [
    (Function.DC_VOLTAGE, "DC V"),
    (Function.AC_VOLTAGE, "AC V"),
    (Function.DC_CURRENT, "DC I"),
    (Function.AC_CURRENT, "AC I"),
    (Function.RESISTANCE_2W, "2-wire Ω"),
    (Function.RESISTANCE_4W, "4-wire Ω"),
    (Function.FREQUENCY, "Frequency"),
    (Function.PERIOD, "Period"),
    (Function.CONTINUITY, "Continuity"),
    (Function.DIODE, "Diode"),
    (Function.DC_VOLTAGE_RATIO, "DC V ratio"),
]


def test_there_is_a_button_for_every_function(make_window):
    window = make_window()

    assert [(function, button.cget("text")) for function, button in window.function_buttons.items()] == FUNCTION_LABELS


@pytest.mark.parametrize(
    ("function", "expected"),
    [
        (Function.DC_VOLTAGE, "1.000000 V"),
        (Function.AC_VOLTAGE, "1.000000 V"),
        (Function.DC_CURRENT, "1.000000 mA"),
        (Function.AC_CURRENT, "1.000000 mA"),
        (Function.RESISTANCE_2W, "1.000000 kΩ"),
        (Function.RESISTANCE_4W, "1.000000 kΩ"),
        (Function.FREQUENCY, "1.00000 kHz"),
        (Function.PERIOD, "1.00000 ms"),
        (Function.CONTINUITY, "500.000 mΩ"),
        (Function.DIODE, "600.000 mV"),
        (Function.DC_VOLTAGE_RATIO, "1.000000"),
    ],
)
def test_choosing_a_function_shows_its_readings_with_the_right_unit(make_window, function, expected):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    window.function_buttons[function].invoke()

    pump(
        window, lambda: window.function_label.cget("text") == function.label and window.readout.cget("text") == expected
    )


def test_the_readout_describes_the_setup_it_is_measuring(make_window):
    window = make_window()

    pump(window, lambda: shows_reading(window))
    assert window.setup_label.cget("text") == "DC V · Autorange · 6½ digits · 10 NPLC"

    window.function_buttons[Function.RESISTANCE_4W].invoke()
    pump(window, lambda: window.function_label.cget("text") == "4-wire Ω")

    assert window.setup_label.cget("text") == "4-wire Ω · Autorange · 6½ digits · 10 NPLC"


def test_the_range_box_offers_only_the_ranges_of_the_current_function(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    assert tuple(window.range_box.cget("values")) == ("Auto", "100 mV", "1 V", "10 V", "100 V", "1 kV")

    window.function_buttons[Function.DC_CURRENT].invoke()
    pump(window, lambda: window.function_label.cget("text") == "DC I")
    assert tuple(window.range_box.cget("values")) == ("Auto", "10 mA", "100 mA", "1 A", "3 A")

    window.function_buttons[Function.AC_CURRENT].invoke()
    pump(window, lambda: window.function_label.cget("text") == "AC I")
    assert tuple(window.range_box.cget("values")) == ("Auto", "1 A", "3 A")


def test_choosing_a_fixed_range_applies_it_and_an_overload_shows_as_ovld(make_window):
    window = make_window(Simulator(dc_voltage=5.0))
    pump(window, lambda: window.readout.cget("text") == "5.000000 V")

    choose(window, window.range_box, "1 V")

    pump(window, lambda: window.readout.cget("text") == "OVLD")
    assert window.setup_label.cget("text") == "DC V · 1 V range · 6½ digits · 10 NPLC"

    choose(window, window.range_box, "10 V")
    pump(window, lambda: window.readout.cget("text") == "5.000000 V")
    choose(window, window.range_box, "Auto")
    pump(window, lambda: "Autorange" in window.setup_label.cget("text"))


def test_choosing_a_resolution_sets_the_digits_shown_and_the_integration_time(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    assert tuple(window.resolution_box.cget("values")) == tuple(resolution.label for resolution in Resolution)

    choose(window, window.resolution_box, "4½ digits")

    pump(window, lambda: window.readout.cget("text") == "1.0000 V")
    assert window.nplc_box.get() == "0.02 NPLC"
    assert window.setup_label.cget("text") == "DC V · Autorange · 4½ digits · 0.02 NPLC"


def test_choosing_an_integration_time_decides_the_resolution(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    assert tuple(window.nplc_box.cget("values")) == ("0.02 NPLC", "0.2 NPLC", "1 NPLC", "10 NPLC", "100 NPLC")

    choose(window, window.nplc_box, "0.2 NPLC")

    pump(window, lambda: window.readout.cget("text") == "1.00000 V")
    assert window.resolution_box.get() == "5½ digits"


@pytest.mark.parametrize(
    "function",
    [
        Function.AC_VOLTAGE,
        Function.AC_CURRENT,
        Function.FREQUENCY,
        Function.PERIOD,
        Function.CONTINUITY,
        Function.DIODE,
    ],
)
def test_integration_time_is_disabled_for_functions_that_have_none(make_window, function):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    assert str(window.nplc_box.cget("state")) == "readonly"

    window.function_buttons[function].invoke()
    pump(window, lambda: window.function_label.cget("text") == function.label)

    assert str(window.nplc_box.cget("state")) == "disabled"


@pytest.mark.parametrize(
    "function",
    [
        Function.AC_VOLTAGE,
        Function.AC_CURRENT,
        Function.CONTINUITY,
        Function.DIODE,
    ],
)
def test_resolution_is_disabled_for_functions_that_measure_at_a_fixed_one(make_window, function):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    assert str(window.resolution_box.cget("state")) == "readonly"

    window.function_buttons[function].invoke()
    pump(window, lambda: window.function_label.cget("text") == function.label)

    assert str(window.resolution_box.cget("state")) == "disabled"
    assert window.resolution_box.get() == function.fixed_resolution.label


@pytest.mark.parametrize(
    ("function", "resolution", "gate_time"),
    [
        (Function.FREQUENCY, Resolution.SIX_HALF, GateTime.ONE_SECOND),
        (Function.PERIOD, Resolution.FOUR_HALF, GateTime.TEN_MILLISECONDS),
    ],
)
def test_resolution_of_frequency_and_period_is_chosen_through_the_gate_time(
    make_window, function, resolution, gate_time
):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    window.function_buttons[function].invoke()
    pump(window, lambda: window.function_label.cget("text") == function.label)
    assert str(window.resolution_box.cget("state")) == "readonly"

    choose(window, window.resolution_box, resolution.label)

    pump(window, lambda: window.setup_label.cget("text").endswith(f"{gate_time.label} gate"))


@pytest.mark.parametrize("function", [Function.CONTINUITY, Function.DIODE])
def test_range_is_disabled_for_functions_with_a_fixed_range(make_window, function):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    window.function_buttons[function].invoke()
    pump(window, lambda: window.function_label.cget("text") == function.label)

    assert str(window.range_box.cget("state")) == "disabled"


def test_the_raw_reading_toggle_shows_exactly_what_the_meter_returned(make_window):
    window = make_window(Simulator(dc_voltage=1.5))
    pump(window, lambda: window.readout.cget("text") == "1.500000 V")

    window.raw_check.invoke()
    pump(window, lambda: window.readout.cget("text") == "+1.50000000E+00")

    window.raw_check.invoke()
    pump(window, lambda: window.readout.cget("text") == "1.500000 V")


def test_an_overload_stays_ovld_until_the_raw_toggle_is_used(make_window):
    window = make_window(Simulator(dc_voltage=5000.0))
    pump(window, lambda: window.readout.cget("text") == "OVLD")

    window.raw_check.invoke()

    pump(window, lambda: window.readout.cget("text") == "+9.90000000E+37")


def test_connecting_shows_the_setup_the_meter_was_already_in_without_changing_it(make_window):
    simulator = CountingSimulator()
    simulator.write('FUNC "FRES"')
    simulator.write("FRES:RANG 1000")
    simulator.write("FRES:NPLC 1")
    window = make_window(simulator)

    pump(window, lambda: shows_reading(window))

    assert window.setup_label.cget("text") == "4-wire Ω · 1 kΩ range · 5½ digits · 1 NPLC"
    assert window.range_box.get() == "1 kΩ"
    assert window.resolution_box.get() == "5½ digits"
    assert window.nplc_box.get() == "1 NPLC"
    assert window._function_var.get() == Function.RESISTANCE_4W.value


def test_the_controls_are_unavailable_until_the_meter_is_connected_and_after_a_failure(make_window):
    def refuse():
        message = "no such resource"
        raise TransportError(message)

    window = make_window(opener=refuse)
    window.root.update()
    pump(window, lambda: window.status_connection.cget("text").startswith("Connection failed"))

    for button in window.function_buttons.values():
        assert str(button.cget("state")) == "disabled"
    for box in (window.range_box, window.resolution_box, window.nplc_box):
        assert str(box.cget("state")) == "disabled"


def test_errors_the_meter_queues_after_a_change_appear_in_the_status_bar_and_the_actual_setup_is_shown(make_window):
    simulator = CountingSimulator(dc_voltage=1.0)
    simulator.refuse_ranges_of = "VOLT:DC"
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))

    choose(window, window.range_box, "10 V")

    pump(window, lambda: "-222" in window.status_error.cget("text"))
    assert "Data out of range" in window.status_error.cget("text")
    pump(window, lambda: window.range_box.get() == "Auto")


def test_the_error_is_cleared_by_the_next_successful_change(make_window):
    simulator = CountingSimulator()
    simulator.refuse_ranges_of = "VOLT:DC"
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))
    choose(window, window.range_box, "10 V")
    pump(window, lambda: "-222" in window.status_error.cget("text"))

    simulator.refuse_ranges_of = None
    choose(window, window.range_box, "10 V")

    pump(window, lambda: window.status_error.cget("text") == "")
    assert window.range_box.get() == "10 V"


def settle(window: MainWindow, simulator: CountingSimulator) -> None:
    """Pump until the Worker has stopped taking Readings."""
    while True:
        before = simulator.reads
        pump_for(window, 0.1)
        if simulator.reads == before:
            return


def test_switching_function_clears_the_old_reading_until_the_new_function_has_one(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))
    window.run_button.invoke()  # pause
    settle(window, simulator)
    release = threading.Event()
    simulator.hold_reading = (simulator.reads + 1, release)

    window.function_buttons[Function.RESISTANCE_2W].invoke()
    pump(window, lambda: window.function_label.cget("text") == "2-wire Ω")
    assert window.readout.cget("text") == NO_READING

    window.run_button.invoke()
    pump_for(window, 0.2)
    assert window.readout.cget("text") == NO_READING
    release.set()

    pump(window, lambda: window.readout.cget("text") == "1.000000 kΩ")


def test_switching_function_keeps_the_settings_the_meter_holds_for_each_function(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    choose(window, window.range_box, "10 V")
    pump(window, lambda: "10 V range" in window.setup_label.cget("text"))

    window.function_buttons[Function.RESISTANCE_2W].invoke()
    pump(window, lambda: window.setup_label.cget("text") == "2-wire Ω · Autorange · 6½ digits · 10 NPLC")
    window.function_buttons[Function.DC_VOLTAGE].invoke()

    pump(window, lambda: window.setup_label.cget("text") == "DC V · 10 V range · 6½ digits · 10 NPLC")


def test_controls_are_locked_while_a_change_is_with_the_meter_so_two_changes_cannot_clash(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))
    release = threading.Event()
    stuck_at = simulator.reads + 5
    simulator.hold_reading = (stuck_at, release)
    pump(window, lambda: simulator.reads >= stuck_at)  # the Worker is now stuck in a Reading

    choose(window, window.range_box, "10 V")
    assert str(window.range_box.cget("state")) == "disabled"
    assert str(window.resolution_box.cget("state")) == "disabled"
    assert str(window.function_buttons[Function.DIODE].cget("state")) == "disabled"
    choose(window, window.resolution_box, "5½ digits")  # ignored: the controls are locked
    release.set()

    pump(window, lambda: window.setup_label.cget("text") == "DC V · 10 V range · 6½ digits · 10 NPLC")
    assert str(window.range_box.cget("state")) == "readonly"
    assert str(window.function_buttons[Function.DIODE].cget("state")) == "normal"


def test_a_change_the_worker_could_not_confirm_puts_the_controls_back_to_what_the_meter_reported(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))
    simulator.garbage_function = True

    window.function_buttons[Function.DIODE].invoke()

    pump(window, lambda: "Setup change failed" in window.status_error.cget("text"))
    assert window._function_var.get() == Function.DC_VOLTAGE.value
    assert window.function_label.cget("text") == "DC V"
    assert str(window.range_box.cget("state")) == "readonly"


def test_a_reading_is_shown_with_the_resolution_it_was_taken_at(make_window):
    window = make_window(Simulator(dc_voltage=1.23456789))
    pump(window, lambda: window.readout.cget("text") == "1.234570 V")

    choose(window, window.resolution_box, "5½ digits")

    pump(window, lambda: window.readout.cget("text") == "1.23460 V")


def test_closing_the_window_shuts_the_worker_down_and_closes_the_transport(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: simulator.reads > 0)

    window.close()

    assert window.worker_is_alive() is False
    with pytest.raises(TransportError):
        simulator.query("*IDN?")
    assert not _exists(window.root)


def test_closing_twice_is_harmless(make_window):
    window = make_window()

    window.close()
    window.close()


def test_the_window_manager_close_button_closes_the_window_cleanly(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: simulator.reads > 0)

    window.root.tk.call(window.root.protocol("WM_DELETE_WINDOW"))

    assert window.worker_is_alive() is False


def test_running_the_app_with_simulate_shows_a_window_and_returns_success_when_it_is_closed(monkeypatch):
    shown_titles = []

    def close_immediately(self, _n=0):
        shown_titles.append(self.title())
        self.destroy()

    try:
        tk.Tk().destroy()
    except tk.TclError:
        if os.environ.get("CI"):
            raise
        pytest.skip("no display available")
    monkeypatch.setattr(tk.Tk, "mainloop", close_immediately)

    assert main(["--simulate"]) == 0
    assert shown_titles == [f"Agilent 34401A {__version__}"]


def test_running_the_app_without_simulate_explains_that_connections_are_not_available_yet(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2
    assert "--simulate" in capsys.readouterr().err


def test_version_flag_prints_the_version_and_exits_successfully(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_the_tab_strip_is_shown_once_a_feature_has_added_its_tab(make_window):
    window = make_window()

    assert window.notebook.winfo_manager() == "pack"
    assert "System" in [window.notebook.tab(tab, "text") for tab in window.notebook.tabs()]


def test_added_tabs_appear_in_order_under_their_titles(make_window):
    window = make_window()

    window.add_tab("Trigger", ttk.Frame(window.notebook))
    window.add_tab("Math", ttk.Frame(window.notebook))

    assert [window.notebook.tab(tab, "text") for tab in window.notebook.tabs()][-2:] == ["Trigger", "Math"]
    assert window.notebook.winfo_manager() == "pack"


def test_the_window_keeps_the_settings_it_was_given(tk_root):
    settings = Settings.in_memory()
    window = MainWindow(tk.Toplevel(tk_root), Simulator, "Simulator", settings=settings)
    try:
        assert window.settings is settings
    finally:
        window.close()


def test_a_window_without_settings_gets_defaults_that_are_never_saved(make_window):
    window = make_window()

    assert window.settings.path is None
    assert window.settings.theme is Theme.SYSTEM


def test_a_menu_command_runs_when_its_entry_is_invoked(make_window):
    window = make_window()
    calls = []

    window.add_menu_command("File", "Connect…", lambda: calls.append("connect"))

    menu = window.menu("File")
    assert menu.entrycget(0, "label") == "Connect…"
    menu.invoke(0)
    assert calls == ["connect"]


def test_menus_appear_in_the_usual_order_however_they_are_added(make_window):
    window = make_window()

    window.add_menu_command("Help", "Shortcuts", lambda: None)
    window.add_menu_command("File", "Connect…", lambda: None)
    window.add_menu_command("View", "Compact", lambda: None)

    labels = [window.menubar.entrycget(index, "label") for index in range(window.menubar.index("end") + 1)]
    assert labels == ["File", "View", "Help"]


def test_entries_added_to_a_menu_keep_their_order(make_window):
    window = make_window()

    window.add_menu_command("File", "Connect…", lambda: None)
    window.add_menu_command("File", "Disconnect", lambda: None)

    menu = window.menu("File")
    assert [menu.entrycget(index, "label") for index in range(menu.index("end") + 1)] == ["Connect…", "Disconnect"]


def test_the_menubar_is_shown_because_the_view_menu_is_always_there(make_window):
    window = make_window()

    assert str(window.root.cget("menu")) == str(window.menubar)
    assert window.menubar.entrycget(0, "label") == "View"


def test_an_unusual_menu_name_goes_after_the_usual_ones(make_window):
    window = make_window()

    window.add_menu_command("Tools", "Something", lambda: None)
    window.add_menu_command("Help", "Shortcuts", lambda: None)

    labels = [window.menubar.entrycget(index, "label") for index in range(window.menubar.index("end") + 1)]
    assert labels == ["View", "Help", "Tools"]


def _style(window: MainWindow) -> ttk.Style:
    return ttk.Style(window.root)


def _settings_with(**fields) -> Settings:
    settings = Settings.in_memory()
    for name, value in fields.items():
        setattr(settings, name, value)
    return settings


def _window_with(tk_root, settings: Settings) -> MainWindow:
    return MainWindow(tk.Toplevel(tk_root), Simulator, "Simulator", settings=settings)


def test_the_dark_theme_paints_the_window_dark_and_the_light_theme_light(tk_root):
    backgrounds = {}
    for theme in (Theme.DARK, Theme.LIGHT):
        window = _window_with(tk_root, _settings_with(theme=theme))
        try:
            backgrounds[theme] = _style(window).lookup("TFrame", "background")
        finally:
            window.close()

    assert backgrounds[Theme.DARK] == "#232629"
    assert backgrounds[Theme.LIGHT] == "#f0f0f0"


def _entry_count(menu: tk.Menu) -> int:
    last = menu.index("end")
    return 0 if last is None else last + 1


def _choose_in_menu(menu: tk.Menu, label: str) -> None:
    for index in range(_entry_count(menu)):
        if menu.type(index) != "separator" and menu.entrycget(index, "label") == label:
            menu.invoke(index)
            return
    pytest.fail(f"no {label!r} entry in the menu")


def _submenu(window: MainWindow, menu: str, label: str) -> tk.Menu:
    parent = window.menu(menu)
    for index in range(_entry_count(parent)):
        if parent.type(index) == "cascade" and parent.entrycget(index, "label") == label:
            return cast("tk.Menu", parent.nametowidget(parent.entrycget(index, "menu")))
    pytest.fail(f"no {label!r} submenu in the {menu} menu")


def test_choosing_a_theme_in_the_view_menu_restyles_the_window_at_once_and_saves_the_choice(tk_root, tmp_path):
    settings = Settings.load(tmp_path)
    window = _window_with(tk_root, settings)
    try:
        _choose_in_menu(_submenu(window, "View", "Theme"), "Dark")

        assert _style(window).lookup("TFrame", "background") == "#232629"
        assert str(window.root.cget("background")) == "#232629"
        assert settings.theme is Theme.DARK
        assert Settings.load(tmp_path).theme is Theme.DARK
    finally:
        window.close()


def test_a_callback_hears_the_palette_now_and_after_every_theme_change(make_window):
    window = make_window()
    heard = []

    window.on_theme_changed(lambda palette: heard.append(palette.name))
    window.set_theme(Theme.DARK)
    window.set_theme(Theme.LIGHT)
    window.set_theme(Theme.SYSTEM)

    assert heard == ["System", "Dark", "Light", "System"]


def test_system_leaves_the_light_and_dark_themes_alone_and_uses_a_native_ttk_theme(make_window):
    window = make_window()

    window.set_theme(Theme.DARK)
    window.set_theme(Theme.SYSTEM)

    assert _style(window).theme_use() not in {"agilent34401a_dark", "agilent34401a_light"}
    assert _style(window).lookup("TFrame", "background") != "#232629"


def test_the_error_message_in_the_status_bar_follows_the_theme(make_window):
    window = make_window()

    window.set_theme(Theme.DARK)
    dark = _style(window).lookup("Error.TLabel", "foreground")
    window.set_theme(Theme.LIGHT)
    light = _style(window).lookup("Error.TLabel", "foreground")

    assert dark != light
    assert str(window.status_error.cget("style")) == "Error.TLabel"


def test_the_readout_keeps_its_vfd_colours_in_every_theme(make_window):
    window = make_window()
    before = (str(window.readout.cget("bg")), str(window.readout.cget("fg")))

    window.set_theme(Theme.LIGHT)

    assert (str(window.readout.cget("bg")), str(window.readout.cget("fg"))) == before


def test_menus_follow_the_theme_including_ones_added_later(make_window):
    window = make_window()
    window.set_theme(Theme.DARK)

    window.add_menu_command("Tools", "Something", lambda: None)

    assert str(window.menu("Tools").cget("background")) == window.palette.surface
    window.set_theme(Theme.LIGHT)
    assert str(window.menu("Tools").cget("background")) == window.palette.surface
    assert window.palette.name == "Light"


def press(widget: tk.Misc, key: str) -> None:
    """Send the key to `widget` as if typed there; Tk delivers key events to the widget that has focus."""
    widget.focus_force()
    widget.update()
    widget.event_generate(f"<{key}>")
    widget.update()


def _shows_function(window: MainWindow, function: Function) -> bool:
    """Whether the window shows `function` and is ready for the next change (not still waiting for the Meter)."""
    ready = str(window.function_buttons[Function.DC_VOLTAGE].cget("state")) == "normal"
    return ready and window.function_label.cget("text") == function.label


def test_the_function_keys_select_the_eleven_functions_in_order(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    for number, function in enumerate(Function, start=1):
        press(window.root, f"Key-F{number}")
        pump(window, partial(_shows_function, window, function))


def test_r_runs_and_pauses_the_continuous_readings(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    assert window.run_button.cget("text") == "Pause"

    press(window.root, "Key-r")
    assert window.run_button.cget("text") == "Run"

    press(window.root, "Key-R")
    assert window.run_button.cget("text") == "Pause"


def test_r_does_nothing_before_there_is_a_connection(make_window):
    window = make_window(opener=lambda: (_ for _ in ()).throw(TransportError("no")))
    pump(window, lambda: window.status_connection.cget("text").startswith("Connection failed"))

    press(window.root, "Key-r")

    assert window.run_button.cget("text") == "Run"


def test_keys_without_ctrl_stay_quiet_while_a_text_field_has_focus(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    field = ttk.Entry(window.root)
    field.pack()

    press(field, "Key-r")
    press(field, "Key-F3")

    assert window.run_button.cget("text") == "Pause"
    assert window.function_label.cget("text") == Function.DC_VOLTAGE.label


def test_keys_still_work_while_a_read_only_combobox_has_focus(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    press(window.range_box, "Key-r")

    assert window.run_button.cget("text") == "Run"


def test_ctrl_shortcuts_work_even_in_a_text_field(make_window):
    window = make_window()
    calls = []
    window.register_shortcut("Ctrl+L", "Test", lambda: calls.append("l"))
    field = tk.Text(window.root)
    field.pack()

    press(field, "Control-Key-l")

    assert calls == ["l"]


def test_space_does_not_fire_a_shortcut_on_top_of_a_focused_button_pressing_itself(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    calls = []
    window.register_shortcut("Space", "Test", lambda: calls.append("space"))

    press(window.run_button, "Key-space")
    press(window.root, "Key-space")

    assert calls == ["space"]


def test_a_feature_registers_a_shortcut_and_it_runs_its_handler(make_window):
    window = make_window()
    calls = []

    window.register_shortcut("Ctrl+L", "Record", lambda: calls.append("record"))
    press(window.root, "Control-Key-l")

    assert calls == ["record"]


def test_a_shortcut_cannot_be_taken_twice(make_window):
    window = make_window()

    with pytest.raises(ValueError, match="already used"):
        window.register_shortcut("F1", "Something else", lambda: None)


def _listed(window: MainWindow) -> dict[str, tuple[str, str]]:
    table = window.shortcuts_table
    rows = (table.item(row, "values") for row in table.get_children())
    return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}


def test_help_shortcuts_lists_every_shortcut_marking_the_planned_ones(make_window):
    window = make_window()

    _choose_in_menu(window.menu("Help"), "Shortcuts")

    listed = _listed(window)
    assert [f"F{number}" for number in range(1, 12)] == [key for key in listed if key.startswith("F")]
    assert listed["F2"][0] == "Select AC V"
    assert listed["R"][1] == ""
    for planned in ("Space", "Ctrl+L", "Ctrl+,"):
        assert "planned" in listed[planned][1]


def test_a_planned_shortcut_is_listed_as_available_once_a_feature_registers_it(make_window):
    window = make_window()
    window.register_shortcut("Ctrl+L", "Start or stop recording", lambda: None)

    window.show_shortcuts()

    assert _listed(window)["Ctrl+L"] == ("Start or stop recording", "")
    assert "planned" in _listed(window)["Ctrl+,"][1]


def test_asking_for_the_shortcuts_twice_shows_one_window(make_window):
    window = make_window()

    window.show_shortcuts()
    first = window.shortcuts_dialog
    window.show_shortcuts()

    assert window.shortcuts_dialog is first


def _shown(*widgets: tk.Misc) -> list[bool]:
    return [bool(widget.winfo_ismapped()) for widget in widgets]


def _compact_toggle(window: MainWindow) -> None:
    _choose_in_menu(window.menu("View"), "Compact mode")


def test_compact_mode_keeps_only_the_readout_the_function_buttons_and_the_range(tk_root):
    window = _window_with(tk_root, _settings_with(compact_mode=True))
    try:
        window.add_tab("Extra", ttk.Frame(window.notebook))
        window.root.update()

        kept = [window.readout, window.function_label, window.function_buttons[Function.DC_VOLTAGE], window.range_box]
        dropped = [window.setup_label, window.run_button, window.resolution_box, window.nplc_box, window.raw_check]
        assert all(_shown(*kept))
        assert not any(_shown(*dropped))
        assert not any(_shown(window.notebook))
    finally:
        window.close()


def test_the_view_menu_switches_compact_mode_on_and_off_and_saves_it(tk_root, tmp_path):
    settings = Settings.load(tmp_path)
    window = _window_with(tk_root, settings)
    try:
        window.add_tab("Extra", ttk.Frame(window.notebook))
        _compact_toggle(window)
        window.root.update()

        assert settings.compact_mode is True
        assert Settings.load(tmp_path).compact_mode is True
        assert not window.run_button.winfo_ismapped()

        _compact_toggle(window)
        window.root.update()

        assert Settings.load(tmp_path).compact_mode is False
        assert window.run_button.winfo_ismapped()
        assert window.setup_label.winfo_ismapped()
        assert window.resolution_box.winfo_ismapped()
        assert window.notebook.winfo_ismapped()
    finally:
        window.close()


def test_leaving_compact_mode_puts_everything_back_in_its_place(tk_root):
    window = _window_with(tk_root, _settings_with(compact_mode=True))
    try:
        window.set_compact(compact=False)
        window.root.update()

        order = sorted(
            (window.run_button, window.range_box, window.resolution_box, window.nplc_box, window.raw_check),
            key=lambda widget: widget.winfo_x(),
        )
        assert order == [window.run_button, window.range_box, window.resolution_box, window.nplc_box, window.raw_check]
        assert window.setup_label.winfo_y() < window.readout.winfo_y()
    finally:
        window.close()


def test_compact_mode_still_takes_readings_and_r_still_pauses(tk_root):
    window = _window_with(tk_root, _settings_with(compact_mode=True))
    try:
        pump(window, lambda: shows_reading(window))

        press(window.root, "Key-r")

        assert window.run_button.cget("text") == "Run"
    finally:
        window.close()


def _ignore_palette(_window: MainWindow, _palette: object) -> None:
    pass


def test_a_closed_window_is_freed_at_once_not_by_the_cycle_collector_in_whichever_thread_runs_it(tk_root):
    """Tk variables must be finalised on the main thread, so a closed window may not sit in a reference cycle."""
    gc.disable()
    try:
        window = _window_with(tk_root, Settings.in_memory())
        window.register_shortcut("Ctrl+L", "Record", window.close)  # a handler that is a method of the window
        window.on_theme_changed(partial(_ignore_palette, window))
        window.close()
        freed = weakref.ref(window)
        del window
        assert freed() is None
    finally:
        gc.enable()
