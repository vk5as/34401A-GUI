"""The Trigger tab: Single, and Burst with Trigger Source, Trigger Delay, Sample Count and Trigger Count."""

import csv
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

import pytest

from agilent34401a.gui.main_window import MainWindow
from agilent34401a.trigger import TriggerSettings, TriggerSource
from tests.test_gui_app import CountingSimulator, connected, handled_everything_sent_so_far, pump, pump_for
from tests.test_gui_app import press as press_key

if TYPE_CHECKING:
    from agilent34401a.worker import ReadingTaken

NOT_CONNECTED = "Connect to a Meter first."


class TriggeringSimulator(CountingSimulator):
    """A Simulator that fires its external trigger input from the Worker's thread when the status byte is polled."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.polls = 0
        self.fire_on_poll: int | None = None
        self.supports_device_clear = True
        self.clears = 0
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)
        super().write(command)

    def query(self, command: str) -> str:
        if command == "*STB?":
            self.polls += 1
            if self.polls == self.fire_on_poll:
                self.external_trigger()
        return super().query(command)

    def clear(self) -> None:
        self.clears += 1
        super().clear()


def state(widget: tk.Widget) -> str:
    return str(widget.cget("state"))


def tab(window: MainWindow):
    return window.trigger_tab


def type_into(window: MainWindow, entry: ttk.Entry, text: str) -> None:
    entry.delete(0, "end")
    entry.insert(0, text)
    window.root.update()


def pause(window: MainWindow) -> None:
    """Stop Continuous, so that only what the test asks for reaches the Meter."""
    pump(window, lambda: connected(window))
    if window.run_button.cget("text") == "Pause":
        window.run_button.invoke()
    handled_everything_sent_so_far(window)  # the Worker finishes the Reading in progress before it pauses


def burst_done(window: MainWindow) -> bool:
    return tab(window).last_burst is not None


def readings_in_history(window: MainWindow) -> int:
    return len(window.chart.history)


# --- the tab ----------------------------------------------------------------------------------------------------


def test_the_window_has_a_trigger_tab_and_a_single_button(make_window):
    window = make_window()

    assert window.notebook.tab(tab(window).frame, "text") == "Trigger"
    assert tab(window).single_button.cget("text") == "Single"
    assert str(tab(window).single_button.winfo_parent()) == str(window.controls)


def test_nothing_can_be_started_before_the_meter_is_connected_and_the_tooltips_say_so(make_window):
    window = make_window()

    for widget in (tab(window).single_button, tab(window).start_button, tab(window).cancel_button):
        assert state(widget) == "disabled"
    assert all(state(button) == "disabled" for button in tab(window).source_buttons.values())
    assert tab(window).tooltips["single"].text == NOT_CONNECTED
    assert tab(window).tooltips["start"].text == NOT_CONNECTED


def test_the_controls_start_from_the_trigger_settings_the_meter_already_has(make_window):
    simulator = TriggeringSimulator()
    simulator.write("TRIG:DEL 0.25")  # Continuous, which starts on connect, leaves a fixed Trigger Delay alone
    window = make_window(simulator)
    pump(window, lambda: connected(window))

    assert tab(window).settings == TriggerSettings(TriggerSource.IMMEDIATE, 0.25, 1, 1)
    assert tab(window).source_var.get() == "IMM"
    assert tab(window).delay_entry.get() == "0.25"
    assert tab(window).sample_entry.get() == "1"
    assert tab(window).trigger_entry.get() == "1"


def test_the_controls_show_the_one_immediate_reading_that_continuous_puts_a_burst_setup_back_to(make_window):
    simulator = TriggeringSimulator()
    for command in ("TRIG:SOUR BUS", "TRIG:DEL 0.25", "SAMP:COUN 30", "TRIG:COUN 4"):
        simulator.write(command)
    window = make_window(simulator)

    pump(window, lambda: connected(window) and tab(window).source_var.get() == "IMM")

    assert tab(window).settings == TriggerSettings(TriggerSource.IMMEDIATE, 0.25, 1, 1)


def test_a_reset_meter_shows_an_automatic_delay_and_one_reading(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    assert tab(window).settings == TriggerSettings()
    assert state(tab(window).delay_entry) == "disabled"
    assert state(tab(window).start_button) == "normal"


def test_the_delay_can_be_fixed(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    tab(window).delay_fixed_button.invoke()
    type_into(window, tab(window).delay_entry, "1.5")

    assert state(tab(window).delay_entry) == "normal"
    assert tab(window).settings == TriggerSettings(delay=1.5)


# --- refusing bad settings before starting -------------------------------------------------------------------------


def test_a_burst_larger_than_reading_memory_cannot_be_started_and_the_reason_is_shown(make_window):
    simulator = TriggeringSimulator()
    window = make_window(simulator)
    pause(window)
    sent = len(simulator.writes)

    type_into(window, tab(window).sample_entry, "100")
    type_into(window, tab(window).trigger_entry, "6")

    assert state(tab(window).start_button) == "disabled"
    assert "512" in tab(window).tooltips["start"].text
    assert "600" in str(tab(window).summary_label.cget("text"))
    tab(window).start_burst()  # even when asked another way, nothing is sent
    window.root.update()
    assert "512" in str(tab(window).message_label.cget("text"))
    assert simulator.writes[sent:] == []


@pytest.mark.parametrize(
    ("entry", "text", "name"),
    [
        ("sample_entry", "many", "Sample Count"),
        ("sample_entry", "0", "Sample Count"),
        ("trigger_entry", "-3", "Trigger Count"),
    ],
)
def test_entries_that_are_not_valid_counts_disable_start_and_name_the_problem(make_window, entry, text, name):
    window = make_window()
    pump(window, lambda: connected(window))

    type_into(window, getattr(tab(window), entry), text)

    assert state(tab(window).start_button) == "disabled"
    assert name in tab(window).tooltips["start"].text
    assert name in str(tab(window).summary_label.cget("text"))


def test_a_delay_outside_zero_to_an_hour_disables_start(make_window):
    window = make_window()
    pump(window, lambda: connected(window))
    tab(window).delay_fixed_button.invoke()

    type_into(window, tab(window).delay_entry, "4000")

    assert state(tab(window).start_button) == "disabled"
    assert "Trigger Delay" in tab(window).tooltips["start"].text


def test_there_is_no_control_that_offers_an_infinite_trigger_count(make_window):
    window = make_window()

    assert not hasattr(tab(window), "infinite_check")  # a Burst with one always fails, so it is not offered


def test_an_infinite_trigger_count_a_preset_has_is_shown_and_cannot_be_a_burst(make_window):
    window = make_window()
    pump(window, lambda: connected(window))

    tab(window).set_settings(TriggerSettings(trigger_count=None))

    assert tab(window).trigger_entry.get() == "infinite"
    assert tab(window).settings.trigger_count is None
    assert state(tab(window).start_button) == "disabled"
    assert "Trigger Count" in tab(window).tooltips["start"].text
    type_into(window, tab(window).trigger_entry, "3")  # typing a number brings the Burst back
    assert state(tab(window).start_button) == "normal"


# --- Single ----------------------------------------------------------------------------------------------------------


def test_single_takes_exactly_one_reading_and_pauses_continuous(make_window):
    simulator = TriggeringSimulator(dc_voltage=1.5)
    window = make_window(simulator)
    pump(window, lambda: connected(window))
    pump(window, lambda: simulator.reads >= 2)

    tab(window).single_button.invoke()
    pump(window, lambda: window.run_button.cget("text") == "Run")
    pump_for(window, 0.1)
    before = simulator.reads
    tab(window).single_button.invoke()
    pump(window, lambda: simulator.reads == before + 1)
    pump_for(window, 0.1)

    assert simulator.reads == before + 1
    assert str(window.readout.cget("text")).startswith("1.5")


def test_the_space_key_takes_a_single_reading(make_window):
    simulator = TriggeringSimulator()
    window = make_window(simulator)
    pause(window)
    before = simulator.reads

    press_key(window.root, "space")
    pump(window, lambda: simulator.reads == before + 1)
    pump_for(window, 0.1)

    assert simulator.reads == before + 1
    entry = {s.sequence: s for s in window.shortcuts.entries()}["Space"]
    assert entry.available


# --- Burst -----------------------------------------------------------------------------------------------------------


def test_a_burst_puts_its_readings_in_the_readout_the_history_and_the_chart(make_window):
    simulator = TriggeringSimulator(dc_voltage=2.0)
    window = make_window(simulator)
    pause(window)
    before = readings_in_history(window)
    type_into(window, tab(window).sample_entry, "25")

    tab(window).start_button.invoke()
    pump(window, lambda: burst_done(window))

    assert readings_in_history(window) == before + 25
    assert len(tab(window).last_burst.readings) == 25
    assert str(window.readout.cget("text")).startswith("2.0")
    assert "25 Readings" in str(tab(window).status_label.cget("text"))
    assert state(tab(window).start_button) == "normal"
    assert state(tab(window).export_button) == "normal"


def test_a_burst_leaves_continuous_readings_working(make_window):
    simulator = TriggeringSimulator()
    window = make_window(simulator)
    pause(window)
    type_into(window, tab(window).sample_entry, "10")
    type_into(window, tab(window).trigger_entry, "2")
    tab(window).source_buttons[TriggerSource.BUS].invoke()

    tab(window).start_button.invoke()
    pump(window, lambda: burst_done(window))
    before = simulator.reads
    window.run_button.invoke()
    pump(window, lambda: simulator.reads > before + 2)

    assert len(tab(window).last_burst.readings) == 20


def test_the_controls_are_locked_while_a_burst_waits_and_cancel_is_available(make_window):
    simulator = TriggeringSimulator()
    window = make_window(simulator)
    pause(window)
    tab(window).source_buttons[TriggerSource.EXTERNAL].invoke()

    tab(window).start_button.invoke()
    pump(window, lambda: "external trigger" in str(tab(window).status_label.cget("text")))

    assert state(tab(window).cancel_button) == "normal"
    assert state(tab(window).start_button) == "disabled"
    assert state(tab(window).single_button) == "disabled"
    assert state(tab(window).sample_entry) == "disabled"
    assert all(state(button) == "disabled" for button in tab(window).source_buttons.values())


def test_an_external_burst_completes_when_the_trigger_input_fires(make_window):
    simulator = TriggeringSimulator()
    simulator.fire_on_poll = 4
    window = make_window(simulator)
    pause(window)
    tab(window).source_buttons[TriggerSource.EXTERNAL].invoke()
    type_into(window, tab(window).sample_entry, "5")

    tab(window).start_button.invoke()
    pump(window, lambda: burst_done(window))

    assert len(tab(window).last_burst.readings) == 5
    assert tab(window).last_burst.trigger.source is TriggerSource.EXTERNAL


def test_cancelling_a_burst_that_waits_for_an_external_trigger_frees_the_window(make_window):
    simulator = TriggeringSimulator()
    window = make_window(simulator)
    pause(window)
    tab(window).source_buttons[TriggerSource.EXTERNAL].invoke()
    tab(window).start_button.invoke()
    pump(window, lambda: state(tab(window).cancel_button) == "normal")

    tab(window).cancel_button.invoke()
    pump(window, lambda: state(tab(window).start_button) == "normal")

    assert simulator.clears == 1
    assert "cancelled" in str(tab(window).status_label.cget("text"))
    assert state(tab(window).cancel_button) == "disabled"
    assert tab(window).last_burst is None
    tab(window).single_button.invoke()
    pump(window, lambda: shows_a_reading(window))


def shows_a_reading(window: MainWindow) -> bool:
    return str(window.readout.cget("text")) not in {"--------", ""}


def test_a_burst_the_meter_will_not_start_is_reported_in_the_tab(make_window):
    simulator = TriggeringSimulator(time_scale=1, clock=lambda: 100.0)  # a measurement that never ends
    simulator.write("SAMP:COUN 100")
    simulator.write("INIT")
    window = make_window(simulator)
    pause(window)
    tab(window).source_buttons[TriggerSource.BUS].invoke()

    tab(window).start_button.invoke()
    pump(window, lambda: "Init ignored" in str(tab(window).message_label.cget("text")))

    assert state(tab(window).start_button) == "normal"


# --- what a Connection cannot do -----------------------------------------------------------------------------------------


def test_the_external_source_is_disabled_with_a_tooltip_when_the_connection_cannot_send_a_device_clear(make_window):
    simulator = TriggeringSimulator()
    simulator.supports_device_clear = False
    window = make_window(simulator)
    pump(window, lambda: connected(window))

    external = tab(window).source_buttons[TriggerSource.EXTERNAL]

    assert state(external) == "disabled"
    assert "device clear" in tab(window).tooltips["external"].text
    assert state(tab(window).source_buttons[TriggerSource.BUS]) == "normal"
    assert state(tab(window).source_buttons[TriggerSource.IMMEDIATE]) == "normal"


def test_the_external_source_is_available_when_the_connection_can_send_a_device_clear(make_window):
    window = make_window(TriggeringSimulator())
    pump(window, lambda: connected(window))

    assert state(tab(window).source_buttons[TriggerSource.EXTERNAL]) == "normal"
    assert tab(window).tooltips["external"].text == ""


def test_a_tooltip_shows_its_text_under_the_widget_and_goes_away(make_window):
    window = make_window()
    tooltip = tab(window).tooltips["single"]

    tooltip.show()
    window.root.update()
    assert tooltip.window is not None
    tooltip.hide()
    assert tooltip.window is None
    tooltip.text = ""
    tooltip.show()
    assert tooltip.window is None


# --- export ----------------------------------------------------------------------------------------------------------


def test_the_burst_readings_can_be_exported_to_csv(make_window, tmp_path: Path):
    simulator = TriggeringSimulator(dc_voltage=3.0)
    window = make_window(simulator)
    pause(window)
    type_into(window, tab(window).sample_entry, "6")
    tab(window).start_button.invoke()
    pump(window, lambda: burst_done(window))
    path = tmp_path / "burst.csv"
    tab(window).choose_export_file = lambda: path

    tab(window).export_button.invoke()

    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 6
    assert {row["value"] for row in rows} == {"3.0"}
    assert rows[0]["elapsed_s"] == "0.000"
    assert float(rows[-1]["elapsed_s"]) > 0
    assert all(row["timestamp_iso"] for row in rows)


def test_exporting_is_unavailable_until_there_is_a_burst_and_reports_a_file_that_cannot_be_written(
    make_window, tmp_path: Path
):
    simulator = TriggeringSimulator()
    window = make_window(simulator)
    pause(window)
    errors: list[str] = []
    tab(window).show_error = lambda _title, message: errors.append(message)
    assert state(tab(window).export_button) == "disabled"
    tab(window).start_button.invoke()
    pump(window, lambda: burst_done(window))

    assert not tab(window).export_burst(tmp_path / "missing-folder" / "burst.csv")

    assert "Could not export the Burst" in errors[0]


def test_the_file_menu_offers_the_burst_export(make_window):
    window = make_window()
    menu = window.menu("File")
    labels = [
        str(menu.entrycget(i, "label")) for i in range(int(menu.index("end") or 0) + 1) if menu.type(i) != "separator"
    ]

    assert "Export Burst as CSV…" in labels


def test_readings_of_a_burst_are_ordinary_reading_events(make_window):
    simulator = TriggeringSimulator()
    window = make_window(simulator)
    pause(window)
    seen: list[ReadingTaken] = []
    window.add_reading_listener(seen.append)
    type_into(window, tab(window).sample_entry, "4")

    tab(window).start_button.invoke()
    pump(window, lambda: burst_done(window))

    assert len(seen) == 4
    assert seen[0].setup.trigger.sample_count == 4
