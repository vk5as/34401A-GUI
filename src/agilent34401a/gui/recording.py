"""Recording Readings to a CSV file while they arrive, and exporting the History to one.

The Record button, the File menu and Ctrl+L all toggle the same `RecordingControl`. While it records, the status bar
shows `● REC` and the file's path; every Reading that arrives is written (whatever its Function) and flushed to the file
at once (`csv_log`), so a crash loses nothing.
"""

import logging
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from agilent34401a.csv_log import Recording, export_history
from agilent34401a.gui.csv_dialog import ask_csv_file
from agilent34401a.gui.themes import ERROR_STYLE

if TYPE_CHECKING:
    from collections.abc import Callable

    from agilent34401a.gui.main_window import MainWindow
    from agilent34401a.history import History
    from agilent34401a.worker import ReadingTaken

_LOG = logging.getLogger(__name__)

_MAX_PATH_CHARS = 60
_RECORD = "Record"
_STOP = "Stop"
_RECORD_ENTRY = "Record to CSV…"
_STOP_ENTRY = "Stop recording"
_EXPORT_ENTRY = "Export History as CSV…"
_ERROR_TITLE = "CSV"


def _shortened(path: Path) -> str:
    text = str(path)
    return text if len(text) <= _MAX_PATH_CHARS else f"…{text[-(_MAX_PATH_CHARS - 1) :]}"


class RecordingControl:
    """Record to CSV and export the History: the buttons, the indicator and the state behind them.

    `choose_file`, `choose_export_file` (each returns the path chosen, or None for cancel) and `show_error` are
    replaceable, which is how tests answer the dialogs.
    """

    def __init__(self, window: "MainWindow", history: "Callable[[], History]") -> None:
        self._history = history
        self._recording: Recording | None = None
        self._parent: tk.Misc = window.root
        self.choose_file: Callable[[], Path | None] = self._ask_record_file
        self.choose_export_file: Callable[[], Path | None] = self._ask_export_file
        self.show_error: Callable[[str, str], None] = self._show_error

        self.record_button = ttk.Button(window.controls, text=_RECORD, command=self.toggle)
        window.add_control(self.record_button)
        self.indicator = ttk.Label(window.status_bar, text="", style=ERROR_STYLE)  # shown while recording
        window.add_reading_listener(self.on_reading)
        window.add_close_callback(self.stop)
        window.add_menu_command("File", _RECORD_ENTRY, self.toggle)
        self._menu = window.menu("File")
        self._menu_index = int(self._menu.index("end") or 0)
        window.add_menu_command("File", _EXPORT_ENTRY, self.ask_export_history)

    # --- Record -------------------------------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._recording is not None

    @property
    def path(self) -> Path | None:
        """The file being written, or None when not Recording."""
        return None if self._recording is None else self._recording.path

    @property
    def rows_written(self) -> int:
        """How many Readings the current Recording has written (0 when there is none)."""
        return 0 if self._recording is None else self._recording.rows_written

    def toggle(self) -> None:
        """Stop Recording if it is going; otherwise ask where to record, and begin."""
        if self.active:
            self.stop()
            return
        path = self.choose_file()
        if path is not None:
            self.start(path)

    def start(self, path: Path) -> bool:
        """Record every Reading from now on to a new file at `path`; return whether that worked.

        A Recording already going is ended once the new file is open. If the file cannot be created the error is
        shown and the earlier Recording, if any, carries on.
        """
        try:
            recording = Recording.start(path)
        except OSError as error:
            self.show_error(_ERROR_TITLE, f"Could not record to {path}: {error}")
            return False
        self.stop()
        self._recording = recording
        self.record_button.configure(text=_STOP)
        self._menu.entryconfigure(self._menu_index, label=_STOP_ENTRY)
        self.indicator.configure(text=f"● REC  {_shortened(path)}")
        self.indicator.pack(side="right", padx=(0, 12))
        return True

    def stop(self) -> None:
        """End the Recording and close its file. Does nothing when not Recording."""
        recording = self._recording
        if recording is None:
            return
        self._recording = None
        recording.stop()
        self.record_button.configure(text=_RECORD)
        self._menu.entryconfigure(self._menu_index, label=_RECORD_ENTRY)
        self.indicator.pack_forget()

    def on_reading(self, taken: "ReadingTaken") -> None:
        recording = self._recording
        if recording is None:
            return
        try:
            recording.add(taken.reading, taken.timestamp, setup=taken.setup, taken_at=taken.taken_at)
        except OSError as error:
            path = recording.path
            _LOG.warning("Recording to %s failed: %s", path, error)
            self.stop()
            self.show_error(_ERROR_TITLE, f"Recording to {path} stopped because the file could not be written: {error}")

    # --- Export -------------------------------------------------------------------------------------------------

    def ask_export_history(self) -> None:
        """Ask where to save the History as CSV, and save it there."""
        path = self.choose_export_file()
        if path is not None:
            self.export_history(path)

    def export_history(self, path: Path) -> bool:
        """Save the History to a new CSV file at `path`; return whether that worked."""
        try:
            export_history(self._history(), path)
        except OSError as error:
            self.show_error(_ERROR_TITLE, f"Could not export the History to {path}: {error}")
            return False
        return True

    # --- dialogs ------------------------------------------------------------------------------------------------

    def _ask_record_file(self) -> Path | None:
        return ask_csv_file(self._parent, "Record Readings to CSV")

    def _ask_export_file(self) -> Path | None:
        return ask_csv_file(self._parent, "Export History as CSV")

    def _show_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self._parent)


def install_recording(window: "MainWindow") -> RecordingControl:
    """Add the Record button, the indicator and the File menu entries to `window`."""
    chart = window.chart  # not the window: a reference back to it would make a cycle (ADR-0008)
    return RecordingControl(window, lambda: chart.history)
