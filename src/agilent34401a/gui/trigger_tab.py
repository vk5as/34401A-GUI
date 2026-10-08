"""The Trigger tab and the Single button: Trigger Source, Trigger Delay, Sample Count and Trigger Count for a Burst.

A Burst has the Meter take its Readings into Reading Memory on its own and then collects them; they appear in the
readout, the chart and the History like any others, and can be exported to CSV. Single takes exactly one Reading
(the Space key does the same). Options the Connection cannot support are disabled, and their tooltip says why.
"""

import tkinter as tk
import weakref
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from agilent34401a.csv_log import LoggedReading, write_csv
from agilent34401a.errors import InvalidSetupError
from agilent34401a.gui.themes import ERROR_STYLE
from agilent34401a.gui.tooltip import Tooltip
from agilent34401a.trigger import READING_MEMORY_SIZE, TriggerSettings, TriggerSource
from agilent34401a.worker import (
    BurstCancelled,
    BurstFailed,
    BurstFinished,
    BurstProgressed,
    BurstStarted,
    Connected,
    ConnectionFailed,
    ConnectionLost,
    Disconnected,
    Event,
    SetupChanged,
    Worker,
    WorkerFailed,
)

if TYPE_CHECKING:
    from agilent34401a.gui.main_window import MainWindow

TITLE = "Trigger"
_AUTOMATIC = "auto"
_FIXED = "fixed"
_NOT_CONNECTED = "Connect to a Meter first."
_NO_DEVICE_CLEAR = (
    "This Connection is a raw socket, which cannot send the device clear that cancels a wait for an external "
    "trigger, so the External Trigger Source is not available."
)
_SINGLE_TOOLTIP = "Take one Reading (Space)."
_WIDTH = 8
_ERROR_TITLE = "CSV"


def _default_name() -> str:
    return f"34401A-burst-{datetime.now():%Y%m%d-%H%M%S}.csv"  # noqa: DTZ005 - a local time in a file name


def _whole_number(text: str, name: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        message = f"{name} must be a whole number, not {text.strip()!r}"
        raise InvalidSetupError(message) from None


def _seconds(text: str) -> float:
    try:
        return float(text.strip())
    except ValueError:
        message = f"Trigger Delay must be a number of seconds, not {text.strip()!r}"
        raise InvalidSetupError(message) from None


class TriggerTab:
    """Controls for a Burst, and the Single button. Feed it every Worker event with `handle`.

    `frame` is the widget to add to the window's notebook. The tab is a plain object that owns the frame, and never
    holds the window (see ADR-0008). `choose_export_file` (returns a path, or None for cancel) and `show_error` are
    replaceable, which is how tests answer the dialogs.
    """

    def __init__(
        self,
        parent: ttk.Notebook,
        worker: Worker,
        single_parent: tk.Misc,
        *,
        pause_continuous: Callable[[], None] = lambda: None,
    ) -> None:
        self.frame = ttk.Frame(parent, padding=12)
        self._worker = worker
        self._pause_continuous = pause_continuous
        self._root = parent.winfo_toplevel()
        self.choose_export_file: Callable[[], Path | None] = self._ask_export_file
        self.show_error: Callable[[str, str], None] = self._show_error
        self._connected = False
        self._supports_device_clear = True
        self._bursting = False
        self._meter_trigger: TriggerSettings | None = None  # what the Meter last reported, to follow changes to it
        self.last_burst: BurstFinished | None = None
        self.source_var = tk.StringVar(value=TriggerSource.IMMEDIATE.value)
        self.delay_mode_var = tk.StringVar(value=_AUTOMATIC)
        self.delay_var = tk.StringVar(value="0")
        self.sample_count_var = tk.StringVar(value="1")
        self.trigger_count_var = tk.StringVar(value="1")
        self.infinite_var = tk.BooleanVar(value=False)
        self.tooltips: dict[str, Tooltip] = {}
        self._traces: list[tuple[tk.Variable, str]] = []
        self.single_button = ttk.Button(single_parent, text="Single", command=self.single, state="disabled")
        self.tooltips["single"] = Tooltip(self.single_button, _NOT_CONNECTED)
        self._build_settings()
        self._build_burst()
        self.frame.bind("<Destroy>", self._on_destroy)
        self._sync()

    def _on_destroy(self, event: "tk.Event[tk.Misc]") -> None:
        if event.widget is self.frame:
            self._pause_continuous = lambda: None  # the window's, so dropped to keep the pair out of the collector
            for variable, name in self._traces:  # a trace holds the tab for as long as Tcl keeps it
                variable.trace_remove("write", name)
            self._traces.clear()

    # --- building ----------------------------------------------------------------------------------------------

    def _build_settings(self) -> None:
        frame = self.frame
        ttk.Label(frame, text="Trigger Source").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        sources = ttk.Frame(frame)
        sources.grid(row=0, column=1, columnspan=3, sticky="w")
        self.source_buttons: dict[TriggerSource, ttk.Radiobutton] = {}
        for source in TriggerSource:
            button = ttk.Radiobutton(
                sources, text=source.label, value=source.value, variable=self.source_var, command=self._changed
            )
            button.pack(side="left", padx=(0, 12))
            self.source_buttons[source] = button
        self.tooltips["external"] = Tooltip(self.source_buttons[TriggerSource.EXTERNAL])

        ttk.Label(frame, text="Trigger Delay").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=4)
        delay = ttk.Frame(frame)
        delay.grid(row=1, column=1, columnspan=3, sticky="w")
        self.delay_auto_button = ttk.Radiobutton(
            delay, text="Automatic", value=_AUTOMATIC, variable=self.delay_mode_var, command=self._changed
        )
        self.delay_fixed_button = ttk.Radiobutton(
            delay, text="Fixed", value=_FIXED, variable=self.delay_mode_var, command=self._changed
        )
        self.delay_entry = self._entry(delay, self.delay_var)
        self.delay_auto_button.pack(side="left", padx=(0, 12))
        self.delay_fixed_button.pack(side="left", padx=(0, 4))
        self.delay_entry.pack(side="left")
        ttk.Label(delay, text="s (0 to 3600)").pack(side="left", padx=(4, 0))

        ttk.Label(frame, text="Sample Count").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=4)
        self.sample_entry = self._entry(frame, self.sample_count_var)
        self.sample_entry.grid(row=2, column=1, sticky="w")
        ttk.Label(frame, text="Readings per trigger").grid(row=2, column=2, sticky="w", padx=(8, 0))

        ttk.Label(frame, text="Trigger Count").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=4)
        self.trigger_entry = self._entry(frame, self.trigger_count_var)
        self.trigger_entry.grid(row=3, column=1, sticky="w")
        self.infinite_check = ttk.Checkbutton(frame, text="Infinite", variable=self.infinite_var, command=self._changed)
        self.infinite_check.grid(row=3, column=2, sticky="w", padx=(8, 0))

    def _entry(self, parent: tk.Misc, variable: tk.StringVar) -> ttk.Entry:
        entry = ttk.Entry(parent, textvariable=variable, width=_WIDTH)
        self._traces.append((variable, variable.trace_add("write", self._on_typed)))
        return entry

    def _on_typed(self, *_args: str) -> None:
        self._changed()

    def _build_burst(self) -> None:
        frame = self.frame
        self.summary_label = ttk.Label(frame, text="")
        self.summary_label.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.start_button = ttk.Button(buttons, text="Start Burst", command=self.start_burst)
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel_burst)
        self.export_button = ttk.Button(buttons, text="Export Burst as CSV…", command=self.ask_export)
        self.start_button.pack(side="left", padx=(0, 8))
        self.cancel_button.pack(side="left", padx=(0, 8))
        self.export_button.pack(side="left")
        self.tooltips["start"] = Tooltip(self.start_button)
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=1, length=240)
        self.progress.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.status_label = ttk.Label(frame, text="")
        self.status_label.grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.message_label = ttk.Label(frame, text="", style=ERROR_STYLE)
        self.message_label.grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 0))

    # --- the settings the controls hold --------------------------------------------------------------------------

    @property
    def settings(self) -> TriggerSettings:
        """The trigger settings the controls hold. Raises `InvalidSetupError` if they are not a valid set."""
        delay = None if self.delay_mode_var.get() == _AUTOMATIC else _seconds(self.delay_var.get())
        triggers = None if self.infinite_var.get() else _whole_number(self.trigger_count_var.get(), "Trigger Count")
        return TriggerSettings(
            TriggerSource(self.source_var.get()),
            delay,
            _whole_number(self.sample_count_var.get(), "Sample Count"),
            triggers,
        )

    def set_settings(self, settings: TriggerSettings) -> None:
        """Show `settings` in the controls (a Preset, say, that holds them)."""
        self.source_var.set(settings.source.value)
        self.delay_mode_var.set(_AUTOMATIC if settings.delay is None else _FIXED)
        self.delay_var.set("0" if settings.delay is None else f"{settings.delay:g}")
        self.sample_count_var.set(str(settings.sample_count))
        self.infinite_var.set(settings.trigger_count is None)
        self.trigger_count_var.set("1" if settings.trigger_count is None else str(settings.trigger_count))
        self._sync()

    def _problem(self) -> str:
        """Say why the controls cannot start a Burst, or "" if they can."""
        try:
            self.settings.check_fits_reading_memory()
        except InvalidSetupError as error:
            return str(error)
        return ""

    # --- state -------------------------------------------------------------------------------------------------

    def _changed(self) -> None:
        self.message_label.configure(text="")
        self._sync()

    def _sync(self) -> None:
        """Bring every control into line with the Connection, the Burst in progress and the settings typed."""
        idle = self._connected and not self._bursting
        problem = self._problem()
        try:
            readings = self.settings.readings
            summary = "" if readings is None else f"{readings} of {READING_MEMORY_SIZE} Readings in Reading Memory"
        except InvalidSetupError as error:
            summary = str(error)
        self.summary_label.configure(text=summary)
        for source, button in self.source_buttons.items():
            usable = source is not TriggerSource.EXTERNAL or self._supports_device_clear
            button.configure(state="normal" if idle and usable else "disabled")
        self.tooltips["external"].text = "" if self._supports_device_clear else _NO_DEVICE_CLEAR
        state = "normal" if idle else "disabled"
        for widget in (self.delay_auto_button, self.delay_fixed_button, self.sample_entry, self.infinite_check):
            widget.configure(state=state)
        self.delay_entry.configure(state=state if self.delay_mode_var.get() == _FIXED else "disabled")
        self.trigger_entry.configure(state=state if not self.infinite_var.get() else "disabled")
        self.start_button.configure(state="normal" if idle and not problem else "disabled")
        self.tooltips["start"].text = "" if idle and not problem else (problem if idle else self._unavailable_reason())
        self.cancel_button.configure(state="normal" if self._bursting else "disabled")
        self.export_button.configure(state="normal" if self.last_burst and not self._bursting else "disabled")
        self.single_button.configure(state="normal" if idle else "disabled")
        self.tooltips["single"].text = _SINGLE_TOOLTIP if idle else self._unavailable_reason()

    def _unavailable_reason(self) -> str:
        """Say why Single and Start Burst are unavailable."""
        return "A Burst is in progress." if self._bursting else _NOT_CONNECTED

    # --- requests ----------------------------------------------------------------------------------------------

    def single(self) -> None:
        """Take exactly one Reading (pausing Continuous first). Does nothing when not connected or during a Burst."""
        if not self._connected or self._bursting:
            return
        self._pause_continuous()
        self._worker.single()

    def start_burst(self) -> None:
        """Have the Meter take a Burst with the settings in the controls; a problem with them is shown instead."""
        if not self._connected or self._bursting:
            return
        try:
            settings = self.settings
            self._worker.start_burst(settings)  # refuses a Burst larger than Reading Memory before sending anything
        except InvalidSetupError as error:
            self.message_label.configure(text=str(error))
            return
        self._pause_continuous()
        self._bursting = True
        self.message_label.configure(text="")
        self.status_label.configure(text="Starting the Burst…")
        self._sync()

    def cancel_burst(self) -> None:
        """Abandon the Burst in progress; the Meter is sent a device clear."""
        if self._bursting:
            self._worker.cancel_burst()
            self.status_label.configure(text="Cancelling…")

    # --- events ------------------------------------------------------------------------------------------------

    def handle(self, event: Event) -> None:
        """Follow the Worker: the Connection, what the Meter is set to, and the Burst in progress."""
        match event:
            case Connected(setup=setup, supports_device_clear=supports):
                self._connected = True
                self._supports_device_clear = supports
                self._follow_meter(setup.trigger)
            case SetupChanged(setup):
                self._follow_meter(setup.trigger)
            case ConnectionFailed() | ConnectionLost() | WorkerFailed() | Disconnected():
                self._connected = False
                self._bursting = False
            case BurstStarted(_, expected, waits):
                self.progress.configure(maximum=expected, value=0)
                waiting = "Waiting for the external trigger" if waits else "Taking the Burst"
                self.status_label.configure(text=f"{waiting}: 0 of {expected} Readings")
                self._bursting = True
            case BurstProgressed(points, expected):
                self.progress.configure(value=points)
                self.status_label.configure(text=f"{points} of {expected} Readings")
            case BurstFinished(readings):
                self._bursting = False
                self.last_burst = event
                self.progress.configure(value=self.progress.cget("maximum"))
                self.status_label.configure(text=f"Burst complete: {len(readings)} Readings collected")
            case BurstCancelled():
                self._bursting = False
                self.status_label.configure(text="Burst cancelled")
            case BurstFailed(message):
                self._bursting = False
                self.status_label.configure(text="")
                self.message_label.configure(text=message)
        self._sync()

    def _follow_meter(self, trigger: TriggerSettings) -> None:
        """Show what the Meter holds when that changed (on connect, or after a Preset or console command)."""
        if trigger != self._meter_trigger:
            self._meter_trigger = trigger
            if trigger.source is TriggerSource.EXTERNAL and not self._supports_device_clear:
                trigger = TriggerSettings(
                    TriggerSource.IMMEDIATE, trigger.delay, trigger.sample_count, trigger.trigger_count
                )
            self.set_settings(trigger)

    # --- export ------------------------------------------------------------------------------------------------

    def ask_export(self) -> None:
        """Ask where to save the last Burst's Readings as CSV, and save them there."""
        path = self.choose_export_file()
        if path is not None:
            self.export_burst(path)

    def export_burst(self, path: Path) -> bool:
        """Save the Readings of the last Burst to a new CSV file at `path`; return whether that worked."""
        burst = self.last_burst
        if burst is None:
            self.show_error(_ERROR_TITLE, "There is no Burst to export yet.")
            return False
        first = burst.readings[0].timestamp if burst.readings else 0.0
        items = (
            LoggedReading(taken.reading, taken.timestamp - first, taken.taken_at, taken.setup)
            for taken in burst.readings
        )
        try:
            write_csv(path, items)
        except OSError as error:
            self.show_error(_ERROR_TITLE, f"Could not export the Burst to {path}: {error}")
            return False
        return True

    def _ask_export_file(self) -> Path | None:
        name = filedialog.asksaveasfilename(
            parent=self._root,
            title="Export Burst as CSV",
            initialfile=_default_name(),
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv")],
        )
        return Path(name) if name else None

    def _show_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self._root)


def install_trigger(window: "MainWindow") -> TriggerTab:
    """Add the Trigger tab, the Single button and the File menu entry to `window`."""
    pause = weakref.WeakMethod(window.pause_continuous)  # not the window itself: a reference back would make a cycle

    def pause_continuous() -> None:
        method = pause()
        if method is not None:
            method()

    tab = TriggerTab(window.notebook, window.worker, window.controls, pause_continuous=pause_continuous)
    window.add_control(tab.single_button)
    window.add_tab(TITLE, tab.frame)
    window.add_event_handler(tab.handle)
    window.add_menu_command("File", "Export Burst as CSV…", tab.ask_export)
    return tab
