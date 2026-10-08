"""The Math tab: one Math Operation at a time, Null capture, Statistics, Limit Test, and the readout that shows them."""

import tkinter as tk
from tkinter import ttk

import pytest

from agilent34401a.gui.main_window import FAIL_COLOUR, MainWindow
from agilent34401a.math_operations import MathOperation
from agilent34401a.meter import Function
from agilent34401a.sim import Simulator
from tests.test_gui_app import CountingSimulator, pump, pump_for, settle, shows_reading

NOT_KNOWN = "—"


def set_text(entry: ttk.Entry, text: str) -> None:
    entry.delete(0, "end")
    entry.insert(0, text)


def state(widget: tk.Widget) -> str:
    return str(widget.cget("state"))


def running(signal: float = 1.0, function: Function = Function.DC_VOLTAGE) -> Simulator:
    return Simulator(signals={function: signal})


def operation_is(window: MainWindow, operation: MathOperation | None) -> bool:
    return window.math_tab.selected is operation and (
        operation is None or operation.label in str(window.function_label.cget("text"))
    )


def switch_to(window: MainWindow, function: Function) -> None:
    window.function_buttons[function].invoke()
    pump(window, lambda: window.function_label.cget("text") == function.label)


def choose_operation(window: MainWindow, operation: MathOperation | None) -> None:
    window.math_tab.choice_buttons[operation].invoke()
    pump(window, lambda: operation_is(window, operation))


def test_the_window_has_a_math_tab(make_window):
    window = make_window()

    assert window.notebook.tab(window.math_tab.frame, "text") == "Math"


def test_the_math_controls_wait_for_the_connection(make_window):
    window = make_window()

    assert {state(button) for button in window.math_tab.choice_buttons.values()} == {"disabled"}


def test_the_tab_says_only_one_math_operation_is_active_at_a_time(make_window):
    window = make_window()

    assert "one Math Operation" in str(window.math_tab.hint_label.cget("text"))


def test_there_is_a_choice_for_each_operation_and_for_none(make_window):
    window = make_window()

    assert {key: button.cget("text") for key, button in window.math_tab.choice_buttons.items()} == {
        None: "Off",
        MathOperation.NULL: "Null",
        MathOperation.DB: "dB",
        MathOperation.DBM: "dBm",
        MathOperation.STATISTICS: "Statistics",
        MathOperation.LIMIT_TEST: "Limit Test",
    }


def test_a_new_connection_shows_no_math_operation(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    assert window.math_tab.selected is None
    assert window.function_label.cget("text") == "DC V"


def test_choosing_null_turns_it_on_and_the_readout_says_so(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    set_text(window.math_tab.null_offset_entry, "0.25")

    window.math_tab.choice_buttons[MathOperation.NULL].invoke()

    pump(window, lambda: operation_is(window, MathOperation.NULL))
    assert window.function_label.cget("text") == "DC V · Null"
    pump(window, lambda: str(window.readout.cget("text")).startswith("750.0000 mV"))
    assert "Null 250 mV" in str(window.setup_label.cget("text"))


def test_choosing_another_operation_turns_the_first_off(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    choose_operation(window, MathOperation.NULL)

    choose_operation(window, MathOperation.DBM)

    assert window.function_label.cget("text") == "DC V · dBm"
    assert "Null" not in str(window.setup_label.cget("text"))
    assert [key for key in window.math_tab.choice_buttons if window.math_tab.is_chosen(key)] == [MathOperation.DBM]


def test_choosing_off_turns_the_operation_off(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    choose_operation(window, MathOperation.NULL)

    choose_operation(window, None)

    assert window.function_label.cget("text") == "DC V"
    pump(window, lambda: str(window.readout.cget("text")).startswith("1.000000 V"))


def test_the_operation_the_meter_was_already_doing_is_shown_on_connect(make_window):
    simulator = running(1.0)
    simulator.write("CALC:FUNC DBM")
    simulator.write("CALC:DBM:REF 50")
    simulator.write("CALC:STAT ON")
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))

    assert window.math_tab.selected is MathOperation.DBM
    assert window.math_tab.dbm_resistance_entry.get() == "50"


def test_capturing_the_current_reading_makes_it_the_null_offset(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    assert state(window.math_tab.capture_button) == "normal"

    window.math_tab.capture_button.invoke()

    pump(window, lambda: operation_is(window, MathOperation.NULL))
    assert float(window.math_tab.null_offset_entry.get()) == pytest.approx(1.0)
    pump(window, lambda: str(window.readout.cget("text")).startswith("0.0000"))


def test_there_is_nothing_to_capture_until_a_reading_arrives(make_window):
    window = make_window()

    assert state(window.math_tab.capture_button) == "disabled"


def test_dbm_uses_the_reference_resistance_that_was_typed(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    set_text(window.math_tab.dbm_resistance_entry, "50")

    window.math_tab.choice_buttons[MathOperation.DBM].invoke()

    pump(window, lambda: str(window.readout.cget("text")).startswith("13.01"))
    assert str(window.readout.cget("text")).endswith("dBm")
    assert "dBm into 50 Ω" in str(window.setup_label.cget("text"))


def test_db_is_relative_to_the_reference_that_was_typed(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    set_text(window.math_tab.db_reference_entry, "2")

    window.math_tab.choice_buttons[MathOperation.DB].invoke()

    pump(window, lambda: str(window.readout.cget("text")).startswith("0.218"))
    assert str(window.readout.cget("text")).endswith("dB")


@pytest.mark.parametrize("resistance", ["49", "8001"])
def test_a_reference_resistance_outside_50_ohms_to_8_kilohms_is_refused_with_a_reason(make_window, resistance):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    set_text(window.math_tab.dbm_resistance_entry, resistance)

    window.math_tab.choice_buttons[MathOperation.DBM].invoke()

    assert "dBm Reference Resistance" in str(window.math_tab.message_label.cget("text"))
    pump_for(window, 0.1)
    assert window.math_tab.selected is None


def test_something_that_is_not_a_number_is_refused_in_the_tab(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    set_text(window.math_tab.null_offset_entry, "a lot")

    window.math_tab.apply_buttons[MathOperation.NULL].invoke()

    assert "Null offset" in str(window.math_tab.message_label.cget("text"))
    assert window.math_tab.selected is None


def test_pressing_return_in_a_settings_field_applies_that_operation(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    set_text(window.math_tab.null_offset_entry, "0.5")

    window.show_tab("Math")
    window.math_tab.null_offset_entry.focus_force()
    window.root.update()
    window.math_tab.null_offset_entry.event_generate("<Return>")

    pump(window, lambda: operation_is(window, MathOperation.NULL))


def test_db_and_dbm_are_only_offered_for_voltage(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))
    buttons = window.math_tab.choice_buttons
    assert state(buttons[MathOperation.DBM]) == "normal"

    switch_to(window, Function.DC_CURRENT)

    assert state(buttons[MathOperation.DB]) == "disabled"
    assert state(buttons[MathOperation.DBM]) == "disabled"
    assert state(buttons[MathOperation.NULL]) == "normal"
    assert state(buttons[MathOperation.STATISTICS]) == "normal"
    assert state(buttons[MathOperation.LIMIT_TEST]) == "normal"


@pytest.mark.parametrize("function", [Function.CONTINUITY, Function.DIODE])
def test_continuity_and_diode_have_no_math_operations(make_window, function):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    switch_to(window, function)

    assert {state(button) for button in window.math_tab.choice_buttons.values()} == {"disabled"}
    assert "no Math Operations" in str(window.math_tab.message_label.cget("text"))


def test_changing_function_turns_the_operation_off_and_the_tab_shows_it(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    choose_operation(window, MathOperation.NULL)

    switch_to(window, Function.RESISTANCE_2W)

    assert window.math_tab.selected is None


# --- Statistics


def statistic(window: MainWindow, name: str) -> str:
    return str(window.math_tab.statistics_labels[name].cget("text"))


def test_the_statistics_are_not_known_until_they_are_on(make_window):
    window = make_window()
    pump(window, lambda: shows_reading(window))

    assert {statistic(window, name) for name in ("minimum", "maximum", "average", "count")} == {NOT_KNOWN}
    assert state(window.math_tab.reset_button) == "disabled"


def test_the_meters_statistics_are_shown_when_statistics_are_on(make_window):
    window = make_window(running(2.0))
    pump(window, lambda: shows_reading(window))

    choose_operation(window, MathOperation.STATISTICS)

    pump(window, lambda: statistic(window, "count") not in (NOT_KNOWN, "0"))
    assert statistic(window, "minimum").startswith("2.000000 V")
    assert statistic(window, "maximum").startswith("2.000000 V")
    assert statistic(window, "average").startswith("2.000000 V")
    assert state(window.math_tab.reset_button) == "normal"
    assert str(window.readout.cget("text")).startswith("2.000000 V")  # Statistics do not change the Reading


def test_resetting_the_statistics_starts_the_count_again(make_window):
    simulator = CountingSimulator(signals={Function.DC_VOLTAGE: 2.0})
    window = make_window(simulator)
    pump(window, lambda: shows_reading(window))
    choose_operation(window, MathOperation.STATISTICS)
    pump(window, lambda: statistic(window, "count") not in (NOT_KNOWN, "0", "1"))
    window.run_button.invoke()  # pause
    settle(window, simulator)

    window.math_tab.reset_button.invoke()

    pump(window, lambda: statistic(window, "count") == "0")
    assert window.math_tab.selected is MathOperation.STATISTICS


def test_turning_statistics_off_clears_them_from_the_tab(make_window):
    window = make_window(running(2.0))
    pump(window, lambda: shows_reading(window))
    choose_operation(window, MathOperation.STATISTICS)
    pump(window, lambda: statistic(window, "count") != NOT_KNOWN)

    choose_operation(window, None)

    pump(window, lambda: statistic(window, "count") == NOT_KNOWN)


# --- Limit Test


def limit_window(make_window, signal: float) -> MainWindow:
    window: MainWindow = make_window(running(signal))
    pump(window, lambda: shows_reading(window))
    set_text(window.math_tab.limit_lower_entry, "-1")
    set_text(window.math_tab.limit_upper_entry, "1")
    window.math_tab.choice_buttons[MathOperation.LIMIT_TEST].invoke()
    pump(window, lambda: operation_is(window, MathOperation.LIMIT_TEST))
    return window


def readout_text(window: MainWindow) -> str:
    return str(window.readout.cget("text"))


def test_a_reading_above_the_upper_limit_turns_the_readout_red_with_hi(make_window):
    window = limit_window(make_window, 1.5)

    pump(window, lambda: readout_text(window).endswith("HI"))
    assert str(window.readout.cget("fg")) == FAIL_COLOUR
    assert readout_text(window).startswith("1.500000 V")


def test_a_reading_below_the_lower_limit_turns_the_readout_red_with_lo(make_window):
    window = limit_window(make_window, -1.5)

    pump(window, lambda: readout_text(window).endswith("LO"))
    assert str(window.readout.cget("fg")) == FAIL_COLOUR


def test_a_reading_within_the_limits_leaves_the_readout_as_it_was(make_window):
    window = limit_window(make_window, 0.5)
    normal = str(window.function_label.cget("fg"))

    pump(window, lambda: readout_text(window).startswith("500.0000 mV"))
    assert str(window.readout.cget("fg")) == normal
    assert not readout_text(window).endswith(("HI", "LO"))


def test_the_readout_is_red_with_the_raw_reading_too(make_window):
    window = limit_window(make_window, 1.5)
    pump(window, lambda: readout_text(window).endswith("HI"))

    window.raw_check.invoke()

    pump(window, lambda: readout_text(window).startswith("+1.5"))
    assert readout_text(window).endswith("HI")
    assert str(window.readout.cget("fg")) == FAIL_COLOUR


def test_turning_the_limit_test_off_gives_the_readout_its_colour_back(make_window):
    window = limit_window(make_window, 1.5)
    normal = str(window.function_label.cget("fg"))
    pump(window, lambda: readout_text(window).endswith("HI"))

    choose_operation(window, None)

    pump(window, lambda: str(window.readout.cget("fg")) == normal and not readout_text(window).endswith("HI"))


def test_limits_with_the_lower_above_the_upper_are_refused(make_window):
    window = make_window(running(1.0))
    pump(window, lambda: shows_reading(window))
    set_text(window.math_tab.limit_lower_entry, "2")
    set_text(window.math_tab.limit_upper_entry, "1")

    window.math_tab.apply_buttons[MathOperation.LIMIT_TEST].invoke()

    assert "Limit Test" in str(window.math_tab.message_label.cget("text"))
    assert window.math_tab.selected is None


def test_the_limits_are_shown_back_from_the_meter(make_window):
    window = limit_window(make_window, 0.5)

    assert float(window.math_tab.limit_lower_entry.get()) == -1
    assert float(window.math_tab.limit_upper_entry.get()) == 1
    assert "Limit Test -1 V to 1 V" in str(window.setup_label.cget("text"))
