from collections.abc import Callable
from tkinter import ttk

import pytest

from agilent34401a.gui.console import ConsoleTab
from agilent34401a.gui.main_window import MainWindow
from agilent34401a.meter import Function
from agilent34401a.sim import AGILENT_IDENTITY, Simulator
from agilent34401a.worker import Disconnected
from tests.test_gui_app import connected, press, pump


class RecordingSimulator(Simulator):
    """A Simulator that remembers every command it was sent, and how many Readings it took."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.commands: list[str] = []
        self.reads = 0

    def write(self, command: str) -> None:
        self.commands.append(command)
        if command == "READ?":
            self.reads += 1
        super().write(command)


def shows(console: ConsoleTab, text: str) -> Callable[[], bool]:
    return lambda: text in console.text


def send(window: MainWindow, command: str) -> None:
    window.console.entry.delete(0, "end")
    window.console.entry.insert(0, command)
    window.console.submit()


def test_the_window_has_a_scpi_console_tab_that_waits_for_the_connection(make_window):
    window = make_window()
    titles = [window.notebook.tab(tab, "text") for tab in window.notebook.tabs()]

    assert "SCPI console" in titles
    assert str(window.console.entry.cget("state")) == "disabled"
    assert str(window.console.send_button.cget("state")) == "disabled"

    pump(window, lambda: connected(window))

    assert str(window.console.entry.cget("state")) == "normal"
    assert str(window.console.send_button.cget("state")) == "normal"


def test_a_query_typed_into_the_console_shows_the_command_and_the_meters_reply(make_window):
    window = make_window(Simulator(identity=AGILENT_IDENTITY))
    pump(window, lambda: connected(window))

    send(window, "*IDN?")
    pump(window, shows(window.console, AGILENT_IDENTITY))

    assert window.console.text.splitlines()[0] == "> *IDN?"
    assert window.console.entry.get() == ""


def test_the_return_key_sends_the_command(make_window):
    window = make_window(Simulator(identity=AGILENT_IDENTITY))
    pump(window, lambda: connected(window))
    window.show_tab("SCPI console")
    window.console.entry.insert(0, "*IDN?")
    window.console.entry.focus_force()
    window.root.update()

    window.console.entry.event_generate("<Return>")
    pump(window, shows(window.console, AGILENT_IDENTITY))


def test_a_command_that_changes_the_setup_updates_the_main_window(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    send(window, 'FUNC "RES"')
    pump(window, lambda: window.function_label.cget("text") == Function.RESISTANCE_2W.label)

    assert "(sent)" in window.console.text


def test_the_meters_complaint_about_a_command_is_shown(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    send(window, "NOTACOMMAND")

    pump(window, shows(window.console, "Meter error -113: Undefined header"))


def test_a_query_the_meter_never_answers_is_reported_and_the_console_keeps_working(make_window):
    window = make_window(Simulator(identity=AGILENT_IDENTITY))
    pump(window, lambda: connected(window))

    send(window, "NOTAQUERY?")
    pump(window, shows(window.console, "Failed:"))
    send(window, "*IDN?")

    pump(window, shows(window.console, AGILENT_IDENTITY))


def test_raw_commands_are_served_while_continuous_runs_and_readings_carry_on(make_window):
    simulator = RecordingSimulator(identity=AGILENT_IDENTITY)
    window = make_window(simulator)
    pump(window, lambda: connected(window))
    pump(window, lambda: simulator.reads > 3)  # Continuous is running

    send(window, "*IDN?")
    pump(window, shows(window.console, AGILENT_IDENTITY))
    reads = simulator.reads

    pump(window, lambda: simulator.reads > reads + 3)
    assert window.run_button.cget("text") == "Pause"


def test_a_calibration_write_is_refused_and_never_sent_unless_the_override_is_ticked(make_window):
    simulator = RecordingSimulator()
    window = make_window(simulator)
    pump(window, lambda: connected(window))
    assert not window.console.allow_calibration.get()

    send(window, "CAL:STR 'x'")
    pump(window, shows(window.console, "Refused:"))
    assert not any(command.startswith("CAL:STR '") for command in simulator.commands)

    window.console.allow_calibration.set(value=True)
    send(window, "CAL:STR 'x'")
    pump(window, lambda: "CAL:STR 'x'" in simulator.commands)


def test_a_read_only_calibration_query_needs_no_override(make_window):
    simulator = RecordingSimulator()
    window = make_window(simulator)
    pump(window, lambda: connected(window))

    send(window, "CAL:STR?")

    pump(window, lambda: len(window.console.text.splitlines()) >= 2)  # the command and the Meter's reply
    assert "Refused" not in window.console.text
    assert "CAL:STR?" in simulator.commands


def test_a_blank_command_is_not_sent(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    send(window, "   ")

    assert window.console.history == ()
    assert window.console.text == ""


def test_up_and_down_walk_through_the_history_and_back_to_what_was_being_typed(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    for command in ("*IDN?", "FUNC?", "SYST:ERR?"):
        send(window, command)
    console = window.console
    console.entry.insert(0, "half-typed")

    console.previous_command()
    assert console.entry.get() == "SYST:ERR?"
    console.previous_command()
    assert console.entry.get() == "FUNC?"
    console.previous_command()
    console.previous_command()
    assert console.entry.get() == "*IDN?"
    console.next_command()
    assert console.entry.get() == "FUNC?"
    console.next_command()
    console.next_command()
    assert console.entry.get() == "half-typed"
    console.next_command()
    assert console.entry.get() == "half-typed"


def test_the_up_and_down_keys_recall_commands(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    send(window, "*IDN?")
    send(window, "FUNC?")
    window.show_tab("SCPI console")
    window.console.entry.focus_force()
    window.root.update()

    window.console.entry.event_generate("<Up>")
    assert window.console.entry.get() == "FUNC?"
    window.console.entry.event_generate("<Up>")
    assert window.console.entry.get() == "*IDN?"
    window.console.entry.event_generate("<Down>")
    assert window.console.entry.get() == "FUNC?"


def test_sending_the_same_command_twice_in_a_row_keeps_one_history_entry(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    send(window, "*IDN?")
    send(window, "*IDN?")
    send(window, "FUNC?")
    send(window, "*IDN?")

    assert window.console.history == ("*IDN?", "FUNC?", "*IDN?")


def test_a_refused_command_stays_in_the_history_so_it_can_be_recalled_and_edited(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    send(window, "CAL:VAL 1")
    window.console.previous_command()

    assert window.console.entry.get() == "CAL:VAL 1"


def test_clear_empties_the_output_but_not_the_history(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    send(window, "*IDN?")
    pump(window, shows(window.console, "HEWLETT"))

    window.console.clear_button.invoke()

    assert window.console.text == ""
    assert window.console.history == ("*IDN?",)


def test_show_tab_brings_the_console_forward_and_focuses_its_entry(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    other = ttk.Frame(window.notebook)
    window.add_tab("Other", other)
    window.notebook.select(other)
    assert str(window.notebook.select()) == str(other)

    window.show_tab("SCPI console")
    window.root.update()

    assert str(window.notebook.select()) == str(window.console.frame)
    assert window.root.focus_get() is window.console.entry


def test_show_tab_rejects_a_tab_that_does_not_exist(make_window):
    window = make_window()

    with pytest.raises(KeyError):
        window.show_tab("No such tab")


def test_the_console_is_disabled_again_when_the_connection_ends(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    window.console.handle_event(Disconnected())

    assert str(window.console.entry.cget("state")) == "disabled"


def test_ctrl_k_brings_up_the_console_from_anywhere_and_lists_as_available(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    other = ttk.Frame(window.notebook)
    window.add_tab("Other", other)
    window.notebook.select(other)
    elsewhere = window.function_buttons[Function.DC_VOLTAGE]

    press(elsewhere, "Control-Key-k")

    assert str(window.notebook.select()) == str(window.console.frame)
    assert window.root.focus_get() is window.console.entry
    window.show_shortcuts()
    listed = {str(table_row[0]): table_row[1:] for table_row in _rows(window)}
    assert listed["Ctrl+K"] == ("Open the SCPI console", "")


def test_ctrl_k_in_the_console_entry_does_not_eat_the_command_being_typed(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    window.console.entry.insert(0, "*IDN?")

    press(window.console.entry, "Control-Key-k")

    assert window.console.entry.get() == "*IDN?"


def _rows(window: MainWindow) -> list[tuple[str, ...]]:
    table = window.shortcuts_table
    return [tuple(str(value) for value in table.item(row, "values")) for row in table.get_children()]
