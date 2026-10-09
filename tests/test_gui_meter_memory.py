"""The Meter Memory section of the Presets tab: the Meter's own numbered, unnamed Setups, stored and recalled on request."""

import tkinter as tk
from typing import Any

from agilent34401a.gui.main_window import MainWindow
from agilent34401a.meter import Function
from tests.test_gui_app import CountingSimulator, choose, pump
from tests.test_gui_presets import answering, change_range, pause, ready, tab

NOT_CONNECTED = "Connect to a Meter first"


class RecordingSimulator(CountingSimulator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.memory_commands: list[str] = []

    def write(self, command: str) -> None:
        if command.startswith(("*SAV", "*RCL")):
            self.memory_commands.append(command)
        super().write(command)


def state(widget: tk.Widget) -> str:
    return str(widget.cget("state"))


def choose_location(window: MainWindow, location: int) -> None:
    tab(window).memory_box.set(tab(window).memory_labels[location])
    tab(window).memory_box.event_generate("<<ComboboxSelected>>")
    window.root.update()


def said(window: MainWindow, text: str) -> bool:
    return text in str(tab(window).memory_message.cget("text"))


def test_the_section_is_there_and_says_these_are_the_meters_own_unnamed_slots(make_window):
    window = make_window()

    text = str(tab(window).memory_note.cget("text"))
    assert "Meter Memory" in str(tab(window).memory_frame.cget("text"))
    assert "unnamed" in text
    assert "Presets" in text  # unlike them


def test_nothing_can_be_stored_or_recalled_before_the_meter_is_connected_and_the_tooltips_say_so(make_window):
    window = make_window()

    for button in (tab(window).store_button, tab(window).recall_button):
        assert state(button) == "disabled"
    assert NOT_CONNECTED in tab(window).tooltips["store"].text
    assert NOT_CONNECTED in tab(window).tooltips["recall"].text


def test_once_connected_a_location_can_be_stored_after_confirming_and_is_acknowledged(make_window):
    simulator = RecordingSimulator()
    window = make_window(simulator)
    pause(window)
    asked: list[Any] = []
    tab(window).confirm = answering(asked, True)
    choose_location(window, 2)

    pump(window, lambda: state(tab(window).store_button) == "normal")
    tab(window).store_button.invoke()

    pump(window, lambda: said(window, "Stored"))
    assert simulator.memory_commands == ["*SAV 2"]
    assert "overwrite" in str(asked[0]).lower()
    assert "2" in str(asked[0])


def test_declining_the_confirmation_sends_nothing(make_window):
    simulator = RecordingSimulator()
    window = make_window(simulator)
    pause(window)
    tab(window).confirm = answering([], False)

    tab(window).store_button.invoke()
    tab(window).recall_button.invoke()

    assert simulator.memory_commands == []


def test_a_recall_asks_first_and_the_window_shows_the_setup_the_meter_is_then_in(make_window):
    simulator = RecordingSimulator()
    window = make_window(simulator)
    pause(window)
    tab(window).confirm = answering([], True)
    change_range(window, "1 V", 1.0)
    choose_location(window, 1)
    tab(window).store_button.invoke()
    pump(window, lambda: said(window, "Stored"))
    change_range(window, "Auto", None)
    asked: list[Any] = []
    tab(window).confirm = answering(asked, True)

    tab(window).recall_button.invoke()

    pump(window, lambda: said(window, "Recalled") and ready(window))
    assert simulator.memory_commands == ["*SAV 1", "*RCL 1"]
    assert window.current_setup is not None
    assert window.current_setup.range == 1.0
    assert "replace" in str(asked[0]).lower()


def test_recalling_a_location_that_holds_nothing_shows_the_meters_complaint_not_success(make_window):
    window = make_window(RecordingSimulator())
    pause(window)
    tab(window).confirm = answering([], True)
    choose_location(window, 3)

    tab(window).recall_button.invoke()

    pump(window, lambda: said(window, "-314"))
    assert not said(window, "Recalled")


def test_the_power_down_location_can_be_recalled_but_not_stored_to(make_window):
    window = make_window(RecordingSimulator())
    pause(window)
    choose_location(window, 0)

    pump(window, lambda: state(tab(window).store_button) == "disabled")
    assert state(tab(window).recall_button) == "normal"
    assert "power-down" in tab(window).tooltips["store"].text


def test_the_buttons_are_disabled_again_when_the_connection_ends(make_window):
    window = make_window(RecordingSimulator())
    pause(window)
    assert state(tab(window).store_button) == "normal"

    window.disconnect()

    pump(window, lambda: state(tab(window).store_button) == "disabled")
    assert state(tab(window).recall_button) == "disabled"


def test_storing_is_never_done_by_itself_when_connecting_or_changing_the_setup(make_window):
    simulator = RecordingSimulator()
    window = make_window(simulator)
    pause(window)
    choose(window, window.range_box, "1 V")
    pump(window, lambda: ready(window))

    window.disconnect()
    pump(window, lambda: state(tab(window).store_button) == "disabled")

    assert simulator.memory_commands == []
    assert window.current_setup is None or window.current_setup.function is Function.DC_VOLTAGE
