import csv
import tkinter as tk
from pathlib import Path

import pytest

from agilent34401a.csv_log import COLUMNS, Recording
from agilent34401a.gui.main_window import MainWindow
from agilent34401a.meter import Function
from tests.test_gui_app import connected, pump
from tests.test_gui_app import press as press_key


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def rows_written(window: MainWindow, at_least: int):
    return lambda: window.recording.rows_written >= at_least


def connect(window: MainWindow, path: Path | None = None) -> list[str]:
    """Wait for the Connection; later errors the window would show in a dialog are collected in the list returned."""
    errors: list[str] = []
    window.recording.show_error = lambda _title, message: errors.append(message)
    if path is not None:
        window.recording.choose_file = lambda: path
    pump(window, lambda: connected(window))
    return errors


def entries(menu: tk.Menu) -> list[int]:
    return [index for index in range(int(menu.index("end") or 0) + 1) if menu.type(index) != "separator"]


def menu_labels(window: MainWindow) -> list[str]:
    menu = window.menu("File")
    return [str(menu.entrycget(index, "label")) for index in entries(menu)]


def choose_in_file_menu(window: MainWindow, label: str) -> None:
    menu = window.menu("File")
    for index in entries(menu):
        if menu.entrycget(index, "label") == label:
            menu.invoke(index)
            return
    pytest.fail(f"No {label!r} in the File menu: {menu_labels(window)}")


# --- Record ----------------------------------------------------------------------------------------------------


def test_the_record_button_streams_readings_to_the_chosen_file_as_they_arrive(make_window, tmp_path: Path):
    path = tmp_path / "capture.csv"
    window = make_window()
    connect(window, path)

    window.recording.record_button.invoke()
    pump(window, rows_written(window, 3))
    # The rows are on disk now, while Recording is still going.
    on_disk = read_rows(path)
    written = window.recording.rows_written
    window.recording.record_button.invoke()

    assert list(on_disk[0]) == list(COLUMNS)
    assert len(on_disk) >= 3
    assert {row["function"] for row in on_disk} == {"DC V"}
    assert all(row["timestamp_iso"] and row["value"] and row["raw"] for row in on_disk)
    assert on_disk[0]["elapsed_s"] == "0.000"
    assert len(read_rows(path)) == written


def test_while_recording_the_window_shows_that_and_the_file_name_and_then_stops_showing_it(make_window, tmp_path: Path):
    path = tmp_path / "capture.csv"
    window = make_window()
    connect(window, path)
    assert not window.recording.indicator.winfo_manager()
    assert window.recording.record_button.cget("text") == "Record"

    window.recording.record_button.invoke()
    window.root.update()

    assert window.recording.active
    assert window.recording.path == path
    assert "REC" in str(window.recording.indicator.cget("text"))
    assert "capture.csv" in str(window.recording.indicator.cget("text"))
    assert window.recording.indicator.winfo_manager() == "pack"
    assert window.recording.record_button.cget("text") == "Stop"

    window.recording.record_button.invoke()
    window.root.update()

    assert not window.recording.active
    assert not window.recording.indicator.winfo_manager()
    assert window.recording.record_button.cget("text") == "Record"


def test_a_long_path_is_shortened_in_the_indicator_but_ends_with_the_file_name(make_window, tmp_path: Path):
    folder = tmp_path / ("deeply-nested-folder-" * 6)
    folder.mkdir()
    path = folder / "capture.csv"
    window = make_window()
    connect(window, path)

    window.recording.start(path)

    text = str(window.recording.indicator.cget("text"))
    assert text.endswith("capture.csv")
    assert len(text) < len(str(path))


def test_recording_goes_on_when_the_function_changes_and_every_row_names_its_function(make_window, tmp_path: Path):
    path = tmp_path / "capture.csv"
    window = make_window()
    connect(window, path)
    window.recording.record_button.invoke()
    pump(window, rows_written(window, 2))

    window.function_buttons[Function.RESISTANCE_2W].invoke()
    start = window.recording.rows_written
    pump(window, lambda: {row["function"] for row in read_rows(path)} >= {"DC V", "2-wire Ω"})
    pump(window, rows_written(window, start + 2))
    window.recording.stop()

    rows = read_rows(path)
    assert window.recording.active is False
    functions = [row["function"] for row in rows]
    assert functions[0] == "DC V"
    assert functions[-1] == "2-wire Ω"
    assert {row["unit"] for row in rows if row["function"] == "2-wire Ω"} == {"Ω"}
    assert {row["range"] for row in rows if row["function"] == "2-wire Ω"} == {"auto"}


def test_ctrl_l_starts_and_stops_recording_and_help_lists_it_as_available(make_window, tmp_path: Path):
    path = tmp_path / "capture.csv"
    window = make_window()
    connect(window, path)

    press_key(window.root, "Control-Key-l")
    assert window.recording.active
    pump(window, rows_written(window, 1))
    press_key(window.root, "Control-Key-l")

    assert not window.recording.active
    shortcut = next(shortcut for shortcut in window.shortcuts.entries() if shortcut.sequence == "Ctrl+L")
    assert shortcut.available
    assert read_rows(path)


def test_the_file_menu_starts_and_stops_recording_and_its_entry_says_which(make_window, tmp_path: Path):
    path = tmp_path / "capture.csv"
    window = make_window()
    connect(window, path)
    assert "Record to CSV…" in menu_labels(window)

    choose_in_file_menu(window, "Record to CSV…")
    assert window.recording.active
    assert "Stop recording" in menu_labels(window)
    assert "Record to CSV…" not in menu_labels(window)

    choose_in_file_menu(window, "Stop recording")
    assert not window.recording.active
    assert "Record to CSV…" in menu_labels(window)


def test_cancelling_the_file_dialog_does_not_start_recording(make_window):
    window = make_window()
    connect(window)
    window.recording.choose_file = lambda: None

    window.recording.record_button.invoke()

    assert not window.recording.active
    assert window.recording.record_button.cget("text") == "Record"


def test_a_file_that_cannot_be_created_is_reported_and_nothing_records(make_window, tmp_path: Path):
    window = make_window()
    errors = connect(window, tmp_path / "nowhere" / "capture.csv")

    window.recording.record_button.invoke()

    assert not window.recording.active
    assert len(errors) == 1
    assert "nowhere" in errors[0]


def test_a_write_that_fails_part_way_ends_the_recording_and_says_so(make_window, tmp_path: Path, monkeypatch):
    path = tmp_path / "capture.csv"
    window = make_window()
    errors = connect(window, path)
    original = Recording.add
    calls: list[int] = []

    def full_disk(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            message = "No space left on device"
            raise OSError(message)
        original(self, *args, **kwargs)

    monkeypatch.setattr(Recording, "add", full_disk)
    window.recording.record_button.invoke()
    pump(window, lambda: bool(errors))

    assert not window.recording.active
    assert "No space left on device" in errors[0]
    assert len(read_rows(path)) == 2


def test_recording_to_a_second_file_replaces_the_first_one_cleanly(make_window, tmp_path: Path):
    window = make_window()
    connect(window)
    window.recording.start(tmp_path / "one.csv")
    pump(window, rows_written(window, 1))

    window.recording.start(tmp_path / "two.csv")
    pump(window, rows_written(window, 1))
    window.recording.stop()

    assert read_rows(tmp_path / "one.csv")
    assert read_rows(tmp_path / "two.csv")[0]["elapsed_s"] == "0.000"


def test_closing_the_window_while_recording_closes_the_file(make_window, tmp_path: Path):
    path = tmp_path / "capture.csv"
    window = make_window()
    connect(window, path)
    window.recording.record_button.invoke()
    pump(window, rows_written(window, 2))
    recording = window.recording

    window.close()

    assert not recording.active
    assert len(read_rows(path)) >= 2


# --- Export ------------------------------------------------------------------------------------------------------


def test_the_file_menu_exports_the_history_to_a_csv_file(make_window, tmp_path: Path):
    path = tmp_path / "history.csv"
    window = make_window()
    errors = connect(window)
    pump(window, lambda: len(window.chart.history) >= 3)
    window.recording.choose_export_file = lambda: path

    choose_in_file_menu(window, "Export History as CSV…")

    rows = read_rows(path)
    assert errors == []
    assert len(rows) >= 3
    assert {row["function"] for row in rows} == {"DC V"}
    assert all(row["timestamp_iso"] and row["range"] == "auto" for row in rows)
    assert rows[0]["elapsed_s"] == "0.000"


def test_cancelling_the_export_dialog_writes_nothing(make_window, tmp_path: Path):
    window = make_window()
    connect(window)
    window.recording.choose_export_file = lambda: None

    choose_in_file_menu(window, "Export History as CSV…")

    assert list(tmp_path.iterdir()) == []


def test_an_export_that_fails_is_reported(make_window, tmp_path: Path):
    window = make_window()
    errors = connect(window)
    window.recording.choose_export_file = lambda: tmp_path / "nowhere" / "history.csv"

    choose_in_file_menu(window, "Export History as CSV…")

    assert len(errors) == 1
    assert "nowhere" in errors[0]
