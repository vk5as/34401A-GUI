"""The Presets tab: save the current Setup, apply it again, rename, delete, export and import, and see what was rejected."""

import json
import tkinter as tk
from pathlib import Path
from typing import Any

import pytest

from agilent34401a.gui.main_window import MainWindow
from agilent34401a.meter import Autozero, Function, Setup
from agilent34401a.preset_store import PRESETS_FILE, Collision, PresetStore
from agilent34401a.settings import Settings
from agilent34401a.sim import Simulator
from agilent34401a.trigger import TriggerSettings, TriggerSource
from tests.test_gui_app import CountingSimulator, choose, connected, handled_everything_sent_so_far, pump, pump_for

NOT_CONNECTED = "Connect to a Meter first"


class RefusingSimulator(CountingSimulator):
    """A Simulator that refuses the writes that start with any of `refuse`, as a Meter would a setting it cannot take."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.refuse: tuple[str, ...] = ()

    def write(self, command: str) -> None:
        if self.refuse and not command.endswith("?") and command.startswith(self.refuse):
            self._errors.append('-221,"Settings conflict"')
            return
        super().write(command)


def answering(asked: list[Any], answer):
    """A dialog that records what it was asked and gives `answer`."""

    def respond(*question):
        asked.append(question[0] if len(question) == 1 else question)
        return answer

    return respond


def tab(window: MainWindow):
    return window.presets_tab


def state(widget: tk.Widget) -> str:
    return str(widget.cget("state"))


def names(window: MainWindow) -> list[str]:
    return [str(tab(window).tree.set(item, "name")) for item in tab(window).tree.get_children()]


def ready(window: MainWindow) -> bool:
    """Connected, and not waiting for the Meter's answer to a change."""
    return connected(window) and window.current_setup is not None and state(window.range_box) == "readonly"


def pause(window: MainWindow) -> None:
    """Stop Continuous, so that only what the test asks for reaches the Meter."""
    pump(window, lambda: ready(window))
    if window.run_button.cget("text") == "Pause":
        window.run_button.invoke()
    handled_everything_sent_so_far(window)  # the Worker finishes the Reading in progress before it pauses


def setup_of(window: MainWindow) -> Setup:
    setup = window.current_setup
    assert setup is not None
    return setup


def has_range(window: MainWindow, value: float | None) -> bool:
    return window.current_setup is not None and window.current_setup.range == value and ready(window)


def change_range(window: MainWindow, label: str, value: float | None) -> None:
    choose(window, window.range_box, label)
    pump(window, lambda: has_range(window, value))


def save_as(window: MainWindow, name: str) -> None:
    tab(window).name_entry.delete(0, "end")
    tab(window).name_entry.insert(0, name)
    tab(window).save_button.invoke()


def applied(window: MainWindow) -> bool:
    return tab(window).last_report is not None


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "config"


# --- the tab -------------------------------------------------------------------------------------------------------


def test_the_window_has_a_presets_tab(make_window):
    window = make_window()

    assert window.notebook.tab(tab(window).frame, "text") == "Presets"
    assert names(window) == []


def test_nothing_can_be_saved_while_there_is_no_setup_to_save(tk_root):
    window = MainWindow(tk.Toplevel(tk_root))  # never connected
    try:
        assert window.current_setup is None
        assert state(tab(window).save_button) == "disabled"
        assert NOT_CONNECTED in tab(window).tooltips["save"].text
        tab(window).name_entry.insert(0, "Nothing")
        tab(window).save_button.invoke()
        assert names(window) == []
    finally:
        window.close()


def test_the_current_setup_can_be_saved_once_the_meter_has_reported_it(make_window):
    window = make_window()
    assert state(tab(window).save_button) == "disabled"

    pump(window, lambda: ready(window))

    assert state(tab(window).save_button) == "normal"
    assert tab(window).tooltips["save"].text == ""
    assert state(tab(window).apply_button) == "disabled"  # no Preset to apply yet


# --- save ----------------------------------------------------------------------------------------------------------


def test_saving_stores_the_setup_the_meter_is_in_under_the_name_typed(make_window):
    window = make_window()
    pause(window)
    change_range(window, "10 V", 10.0)

    save_as(window, "Ten volts")

    assert names(window) == ["Ten volts"]
    assert tab(window).store.get("Ten volts").setup == setup_of(window)
    assert setup_of(window).range == 10.0
    assert tab(window).name_entry.get() == ""
    assert "Saved" in str(tab(window).message_label.cget("text"))


def test_saving_without_a_name_says_so_and_stores_nothing(make_window):
    window = make_window()
    pause(window)

    save_as(window, "   ")

    assert names(window) == []
    assert "name" in str(tab(window).message_label.cget("text"))


def test_saving_under_a_name_in_use_asks_before_replacing_it(make_window):
    window = make_window()
    pause(window)
    change_range(window, "10 V", 10.0)
    save_as(window, "Mine")
    change_range(window, "1 V", 1.0)
    questions: list[Any] = []
    tab(window).confirm = answering(questions, False)

    save_as(window, "mine")

    assert len(questions) == 1
    assert "Mine" in questions[0][1]
    assert tab(window).store.get("Mine").setup.range == 10.0

    tab(window).confirm = lambda _title, _message: True
    tab(window).save_button.invoke()

    assert tab(window).store.get("Mine").setup.range == 1.0
    assert names(window) == ["mine"]


def test_presets_are_kept_in_the_folder_the_settings_are_in(tk_root, folder):
    settings = Settings.load(folder)
    window = MainWindow(tk.Toplevel(tk_root), Simulator, "Simulator", settings=settings)
    try:
        pause(window)
        save_as(window, "Kept")

        assert (folder / PRESETS_FILE).exists()
        assert PresetStore.load(folder).names == ("Kept",)
    finally:
        window.close()


def test_presets_saved_before_are_listed_when_the_window_opens(tk_root, folder):
    PresetStore.load(folder).save("Earlier", Setup.default(Function.AC_VOLTAGE))
    window = MainWindow(tk.Toplevel(tk_root), Simulator, "Simulator", settings=Settings.load(folder))
    try:
        assert names(window) == ["Earlier"]
        assert str(tab(window).tree.set(tab(window).tree.get_children()[0], "setup")).startswith("AC V")
    finally:
        window.close()


def test_a_presets_file_that_could_not_be_used_is_said_so_in_the_tab(tk_root, folder):
    folder.mkdir()
    (folder / PRESETS_FILE).write_text("{ not json", encoding="utf-8")
    window = MainWindow(tk.Toplevel(tk_root), Simulator, "Simulator", settings=Settings.load(folder))
    try:
        assert names(window) == []
        assert "presets.json" in str(tab(window).message_label.cget("text"))
    finally:
        window.close()


# --- apply ---------------------------------------------------------------------------------------------------------


def test_applying_a_preset_puts_the_meter_back_in_that_setup_and_says_it_was_applied(make_window):
    window = make_window()
    pause(window)
    change_range(window, "1 V", 1.0)
    save_as(window, "One volt")
    change_range(window, "100 V", 100.0)
    tab(window).select("One volt")

    tab(window).apply_button.invoke()
    pump(window, lambda: applied(window))
    pump(window, lambda: has_range(window, 1.0))

    assert window.range_box.get() == "1 V"
    report = tab(window).last_report
    assert report is not None
    assert report.applied
    assert str(tab(window).summary_label.cget("text")) == "Preset 'One volt' was applied."
    assert str(tab(window).details_label.cget("text")) == ""


def test_applying_a_preset_of_another_function_switches_the_meter_to_it(make_window):
    window = make_window()
    pause(window)
    tab(window).store.save("Mains", Setup.default(Function.AC_VOLTAGE))
    tab(window).refresh()
    tab(window).select("Mains")

    tab(window).apply_button.invoke()
    pump(window, lambda: applied(window))

    assert setup_of(window).function is Function.AC_VOLTAGE
    assert window.function_label.cget("text") == "AC V"


def test_a_setting_the_meter_refuses_is_listed_in_the_tab_with_the_meters_error(make_window):
    simulator = RefusingSimulator()
    window = make_window(simulator)
    pause(window)
    change_range(window, "10 V", 10.0)
    save_as(window, "Ten volts")
    change_range(window, "100 V", 100.0)
    simulator.refuse = ("VOLT:DC:RANG",)
    tab(window).select("Ten volts")

    tab(window).apply_button.invoke()
    pump(window, lambda: applied(window) and "error" in str(tab(window).details_label.cget("text")))

    assert str(tab(window).summary_label.cget("text")) == (
        "Preset 'Ten volts' was only partly applied: the Meter did not take 1 setting."
    )
    details = str(tab(window).details_label.cget("text")).splitlines()
    assert details == [
        "Range: asked for 10 V, the Meter has 100 V",
        "Meter error -221: Settings conflict",
    ]
    assert window.range_box.get() == "100 V"  # the window shows what the Meter has, not what was asked for


def test_the_result_stays_until_the_next_preset_is_applied(make_window):
    window = make_window()
    pause(window)
    save_as(window, "Now")
    tab(window).select("Now")
    tab(window).apply_button.invoke()
    pump(window, lambda: applied(window))

    pump_for(window, 0.1)

    assert str(tab(window).summary_label.cget("text")) == "Preset 'Now' was applied."


def test_a_preset_with_a_burst_trigger_pauses_continuous_so_that_it_stays_as_applied(make_window):
    window = make_window()
    pause(window)
    window.run_button.invoke()  # Continuous again
    burst = Setup.default(Function.DC_VOLTAGE).with_trigger(TriggerSettings(TriggerSource.BUS, sample_count=4))
    tab(window).store.save("Burst", burst)
    tab(window).refresh()
    tab(window).select("Burst")

    tab(window).apply_button.invoke()
    pump(window, lambda: applied(window))

    assert window.run_button.cget("text") == "Run"
    assert setup_of(window).trigger.source is TriggerSource.BUS


def test_nothing_can_be_applied_without_a_connection_or_a_selection(make_window):
    window = make_window()
    pause(window)
    save_as(window, "Mine")

    assert tab(window).selected_name == "Mine"  # a Preset just saved is the one selected
    assert state(tab(window).apply_button) == "normal"
    tab(window).tree.selection_set(())
    window.root.update()
    assert state(tab(window).apply_button) == "disabled"
    assert tab(window).tooltips["apply"].text == "Select a Preset in the list."


def test_apply_is_disabled_once_the_connection_is_gone(make_window):
    window = make_window()
    pause(window)
    save_as(window, "Mine")
    tab(window).select("Mine")
    window.disconnect()

    pump(window, lambda: state(tab(window).apply_button) == "disabled")


# --- rename and delete ---------------------------------------------------------------------------------------------


def test_a_preset_can_be_renamed(make_window):
    window = make_window()
    pause(window)
    save_as(window, "Old name")
    tab(window).select("Old name")
    asked: list[Any] = []
    tab(window).ask_name = answering(asked, "New name")

    tab(window).rename_button.invoke()

    assert asked == [("Rename Preset", "Old name")]
    assert names(window) == ["New name"]
    assert tab(window).selected_name == "New name"


def test_renaming_to_a_name_in_use_is_refused_and_says_so(make_window):
    window = make_window()
    pause(window)
    save_as(window, "First")
    save_as(window, "Second")
    tab(window).select("First")
    tab(window).ask_name = lambda _title, _initial: "second"

    tab(window).rename_button.invoke()

    assert names(window) == ["First", "Second"]
    assert "already a Preset called 'Second'" in str(tab(window).message_label.cget("text"))


def test_renaming_can_be_cancelled(make_window):
    window = make_window()
    pause(window)
    save_as(window, "First")
    tab(window).select("First")
    tab(window).ask_name = lambda _title, _initial: None

    tab(window).rename_button.invoke()

    assert names(window) == ["First"]


def test_a_preset_is_deleted_only_when_the_user_confirms(make_window):
    window = make_window()
    pause(window)
    save_as(window, "Doomed")
    tab(window).select("Doomed")
    tab(window).confirm = lambda _title, _message: False
    tab(window).delete_button.invoke()
    assert names(window) == ["Doomed"]

    tab(window).confirm = lambda _title, _message: True
    tab(window).delete_button.invoke()

    assert names(window) == []
    assert state(tab(window).delete_button) == "disabled"


# --- export and import ---------------------------------------------------------------------------------------------


def test_one_preset_is_exported_to_the_file_chosen_and_the_whole_library_too(make_window, tmp_path):
    window = make_window()
    pause(window)
    save_as(window, "First")
    save_as(window, "Second")
    tab(window).select("Second")
    chosen: list[Any] = []
    one, everything = tmp_path / "one.json", tmp_path / "all.json"
    tab(window).choose_export_file = answering(chosen, one)

    tab(window).export_button.invoke()
    tab(window).choose_export_file = answering(chosen, everything)
    tab(window).export_all_button.invoke()

    assert chosen == ["Second.json", "34401A-presets.json"]
    assert json.loads(one.read_text(encoding="utf-8"))["presets"][0]["name"] == "Second"
    assert [entry["name"] for entry in json.loads(everything.read_text(encoding="utf-8"))["presets"]] == [
        "First",
        "Second",
    ]


def test_exporting_can_be_cancelled(make_window, tmp_path):
    window = make_window()
    pause(window)
    save_as(window, "First")
    tab(window).select("First")
    tab(window).choose_export_file = lambda _default: None

    tab(window).export_button.invoke()

    assert list(tmp_path.glob("*.json")) == []


def test_an_export_that_cannot_be_written_says_so(make_window, tmp_path):
    window = make_window()
    pause(window)
    save_as(window, "First")
    tab(window).select("First")
    tab(window).choose_export_file = lambda _default: tmp_path / "no-such-folder" / "out.json"

    tab(window).export_button.invoke()

    assert "Could not write" in str(tab(window).message_label.cget("text"))


def library(tmp_path: Path, *presets: tuple[str, Setup]) -> Path:
    other = PresetStore.in_memory()
    for name, setup in presets:
        other.save(name, setup)
    path = tmp_path / "library.json"
    other.export_file(path)
    return path


def test_presets_in_a_file_are_imported_and_the_result_is_said(make_window, tmp_path):
    window = make_window()
    path = library(tmp_path, ("A", Setup.default(Function.DC_VOLTAGE)), ("B", Setup.default(Function.AC_VOLTAGE)))
    tab(window).choose_import_file = lambda: path

    tab(window).import_button.invoke()

    assert names(window) == ["A", "B"]
    assert str(tab(window).message_label.cget("text")) == "Imported 2 Presets."


def test_importing_a_name_in_use_asks_what_to_do(make_window, tmp_path):
    window = make_window()
    pause(window)
    save_as(window, "A")
    path = library(tmp_path, ("a", Setup.default(Function.AC_VOLTAGE)))
    tab(window).choose_import_file = lambda: path
    asked: list[Any] = []
    tab(window).choose_collision = answering(asked, Collision.RENAME)

    tab(window).import_button.invoke()

    assert asked == [("a",)]
    assert names(window) == ["A", "a (2)"]
    assert "renamed" in str(tab(window).message_label.cget("text"))


def test_importing_can_replace_or_be_cancelled(make_window, tmp_path):
    window = make_window()
    pause(window)
    save_as(window, "A")
    path = library(tmp_path, ("A", Setup.default(Function.AC_VOLTAGE)))
    tab(window).choose_import_file = lambda: path

    tab(window).choose_collision = lambda _colliding: None
    tab(window).import_button.invoke()
    assert tab(window).store.get("A").setup.function is Function.DC_VOLTAGE

    tab(window).choose_collision = lambda _colliding: Collision.REPLACE
    tab(window).import_button.invoke()
    assert tab(window).store.get("A").setup.function is Function.AC_VOLTAGE
    assert "replaced 1" in str(tab(window).message_label.cget("text"))


def test_importing_a_file_that_is_not_a_presets_file_is_refused_with_the_reason(make_window, tmp_path):
    window = make_window()
    path = tmp_path / "other.json"
    path.write_text('{"hello": 1}', encoding="utf-8")
    tab(window).choose_import_file = lambda: path

    tab(window).import_button.invoke()

    assert names(window) == []
    assert "not a Preset file" in str(tab(window).message_label.cget("text"))


def test_the_presets_in_an_import_that_were_bad_are_listed(make_window, tmp_path):
    window = make_window()
    path = library(tmp_path, ("Good", Setup.default(Function.DC_VOLTAGE)), ("Bad", Setup.default(Function.DC_VOLTAGE)))
    document = json.loads(path.read_text(encoding="utf-8"))
    document["presets"][1]["setup"]["resolution"] = 9.5
    path.write_text(json.dumps(document), encoding="utf-8")
    tab(window).choose_import_file = lambda: path

    tab(window).import_button.invoke()

    assert names(window) == ["Good"]
    text = str(tab(window).message_label.cget("text"))
    assert "Imported 1 Preset" in text
    assert "Preset 'Bad'" in text
    assert "resolution" in text


def test_a_saved_preset_never_holds_autozero_once_because_it_holds_what_the_meter_reported(make_window):
    window = make_window()
    pause(window)
    save_as(window, "Mine")

    assert tab(window).store.get("Mine").setup.autozero is not Autozero.ONCE
