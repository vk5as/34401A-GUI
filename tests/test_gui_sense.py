"""The Sense tab: AC Filter, Gate Time, Autozero and Input Impedance controls, and the Terminals indicator."""

# The window fixtures are shared with test_gui_app and used as arguments, which ruff reads as redefinitions.
# ruff: noqa: F811

import threading
from tkinter import ttk

import pytest

from agilent34401a.gui.main_window import MainWindow
from agilent34401a.meter import Function, Terminals
from agilent34401a.sim import Simulator
from tests.test_gui_app import (  # noqa: F401 - make_window and tk_root are fixtures
    CountingSimulator,
    choose,
    make_window,
    pump,
    shows_reading,
    tk_root,
)

NOT_APPLICABLE = "—"
AC_FUNCTIONS = (Function.AC_VOLTAGE, Function.AC_CURRENT)
COUNTER_FUNCTIONS = (Function.FREQUENCY, Function.PERIOD)
AUTOZERO_FUNCTIONS = (
    Function.DC_VOLTAGE,
    Function.DC_CURRENT,
    Function.RESISTANCE_2W,
    Function.RESISTANCE_4W,
    Function.DC_VOLTAGE_RATIO,
)


def sense_boxes(window: MainWindow) -> list[ttk.Combobox]:
    tab = window.sense_tab
    return [tab.ac_filter_box, tab.gate_time_box, tab.autozero_box, tab.input_impedance_box]


def state(box: ttk.Combobox) -> str:
    return str(box.cget("state"))


def switch_to(window: MainWindow, function: Function) -> None:
    window.function_buttons[function].invoke()
    pump(window, lambda: window.function_label.cget("text") == function.label)


def shows_setup(window: MainWindow, text: str) -> bool:
    return str(window.setup_label.cget("text")).endswith(text)


def test_the_window_has_a_sense_tab(make_window):
    window = make_window()

    assert window.notebook.tab(window.sense_tab.frame, "text") == "Sense"


def test_the_sense_controls_wait_for_the_connection(make_window):
    window = make_window()

    assert [state(box) for box in sense_boxes(window)] == ["disabled"] * 4


@pytest.mark.parametrize("function", list(Function))
def test_each_sense_control_is_enabled_only_for_the_functions_it_applies_to(make_window, function):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    if function is not Function.DC_VOLTAGE:
        switch_to(window, function)

    tab = window.sense_tab
    assert (state(tab.ac_filter_box) == "readonly") == (function in AC_FUNCTIONS)
    assert (state(tab.gate_time_box) == "readonly") == (function in COUNTER_FUNCTIONS)
    assert (state(tab.autozero_box) == "readonly") == (function in AUTOZERO_FUNCTIONS)
    assert (state(tab.input_impedance_box) == "readonly") == (function is Function.DC_VOLTAGE)


def test_a_control_that_does_not_apply_shows_a_dash(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    assert window.sense_tab.ac_filter_box.get() == NOT_APPLICABLE
    assert window.sense_tab.gate_time_box.get() == NOT_APPLICABLE


def test_the_controls_show_what_the_meter_was_already_set_to_on_connect(make_window):
    simulator = Simulator()
    for command in ("DET:BAND 3", "ZERO:AUTO OFF", "INP:IMP:AUTO ON", "FREQ:APER 1"):
        simulator.write(command)
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))

    assert window.sense_tab.autozero_box.get() == "Off"
    assert window.sense_tab.input_impedance_box.get() == ">10 GΩ"
    switch_to(window, Function.AC_VOLTAGE)
    assert window.sense_tab.ac_filter_box.get() == "3 Hz"
    switch_to(window, Function.FREQUENCY)
    assert window.sense_tab.gate_time_box.get() == "1 s"


def test_the_choices_are_the_ones_the_meter_has(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    tab = window.sense_tab

    assert list(tab.autozero_box.cget("values")) == ["On", "Off", "Once"]
    assert list(tab.input_impedance_box.cget("values")) == ["10 MΩ", ">10 GΩ"]
    switch_to(window, Function.AC_CURRENT)
    assert list(tab.ac_filter_box.cget("values")) == ["3 Hz", "20 Hz", "200 Hz"]
    switch_to(window, Function.PERIOD)
    assert list(tab.gate_time_box.cget("values")) == ["10 ms", "100 ms", "1 s"]


@pytest.mark.parametrize("function", AC_FUNCTIONS)
def test_choosing_an_ac_filter_applies_it_and_shows_what_the_meter_reports(make_window, function):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    switch_to(window, function)

    choose(window, window.sense_tab.ac_filter_box, "200 Hz")

    pump(window, lambda: shows_setup(window, "200 Hz filter"))
    assert window.sense_tab.ac_filter_box.get() == "200 Hz"


@pytest.mark.parametrize(
    ("function", "gate_time", "digits"),
    [(Function.FREQUENCY, "1 s", "6½ digits"), (Function.PERIOD, "10 ms", "4½ digits")],
)
def test_choosing_a_gate_time_applies_it_and_decides_the_resolution(make_window, function, gate_time, digits):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    switch_to(window, function)

    choose(window, window.sense_tab.gate_time_box, gate_time)

    pump(window, lambda: shows_setup(window, f"{gate_time} gate"))
    assert digits in window.setup_label.cget("text")
    assert window.resolution_box.get() == digits


def test_choosing_autozero_off_applies_it(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    choose(window, window.sense_tab.autozero_box, "Off")

    pump(window, lambda: shows_setup(window, "Autozero off"))
    assert window.sense_tab.autozero_box.get() == "Off"


def test_autozero_once_is_shown_as_off_afterwards_because_the_meter_leaves_it_off(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    choose(window, window.sense_tab.autozero_box, "Once")

    pump(window, lambda: shows_setup(window, "Autozero off"))
    assert window.sense_tab.autozero_box.get() == "Off"


def test_choosing_a_high_input_impedance_applies_it_for_dc_voltage(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    choose(window, window.sense_tab.input_impedance_box, ">10 GΩ")

    pump(window, lambda: shows_setup(window, ">10 GΩ input"))
    assert window.sense_tab.input_impedance_box.get() == ">10 GΩ"


def test_options_chosen_for_one_function_are_still_there_after_a_visit_to_another(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    choose(window, window.sense_tab.input_impedance_box, ">10 GΩ")
    pump(window, lambda: shows_setup(window, ">10 GΩ input"))

    switch_to(window, Function.DIODE)
    switch_to(window, Function.DC_VOLTAGE)

    assert window.sense_tab.input_impedance_box.get() == ">10 GΩ"


def test_the_sense_controls_are_locked_while_a_change_is_with_the_meter(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))
    release = threading.Event()
    stuck_at = simulator.reads + 5
    simulator.hold_reading = (stuck_at, release)
    pump(window, lambda: simulator.reads >= stuck_at)

    choose(window, window.sense_tab.autozero_box, "Off")
    assert [state(box) for box in sense_boxes(window)] == ["disabled"] * 4
    choose(window, window.sense_tab.input_impedance_box, ">10 GΩ")  # ignored: the controls are locked
    release.set()

    pump(window, lambda: shows_setup(window, "Autozero off"))
    assert state(window.sense_tab.autozero_box) == "readonly"
    assert "input" not in window.setup_label.cget("text")


class RefusingSimulator(CountingSimulator):
    """A Simulator that queues an error instead of accepting `ZERO:AUTO`."""

    def write(self, command: str) -> None:
        if command.startswith("ZERO:AUTO") and not command.endswith("?"):
            self._errors.append('-222,"Data out of range"')
            return
        super().write(command)


def test_an_option_the_meter_refuses_reports_the_error_and_shows_what_it_kept(make_window):
    window = make_window(RefusingSimulator())
    pump(window, lambda: shows_reading(window))

    choose(window, window.sense_tab.autozero_box, "Off")

    pump(window, lambda: "-222" in window.status_error.cget("text"))
    assert window.sense_tab.autozero_box.get() == "On"
    assert state(window.sense_tab.autozero_box) == "readonly"


def test_the_terminals_indicator_says_front_for_a_meter_on_its_front_terminals(make_window):
    window = make_window()

    pump(window, lambda: window.status_terminals.cget("text") == "Terminals: Front")


def test_the_terminals_indicator_says_rear_for_a_meter_on_its_rear_terminals(make_window):
    simulator = Simulator()
    simulator.terminals = Terminals.REAR
    window = make_window(simulator)

    pump(window, lambda: window.status_terminals.cget("text") == "Terminals: Rear")


def test_the_terminals_indicator_follows_the_switch_after_the_next_setup_change(make_window):
    simulator = Simulator()
    window = make_window(simulator)
    pump(window, lambda: window.status_terminals.cget("text") == "Terminals: Front")
    simulator.terminals = Terminals.REAR

    window.function_buttons[Function.DIODE].invoke()

    pump(window, lambda: window.status_terminals.cget("text") == "Terminals: Rear")
