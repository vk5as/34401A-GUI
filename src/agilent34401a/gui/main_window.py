"""The main window: Function, Range and Resolution controls, a VFD-style readout and a status bar, driven by the Worker."""

import logging
import queue
import tkinter as tk
from collections.abc import Callable
from functools import partial
from tkinter import font as tkfont
from tkinter import ttk
from typing import Literal

from agilent34401a import __version__
from agilent34401a.errors import InvalidSetupError
from agilent34401a.meter import NPLC_VALUES, Function, Resolution, Setup, describe_setup, format_range, format_reading
from agilent34401a.rate import ReadingRate
from agilent34401a.transport import Transport
from agilent34401a.worker import (
    Connected,
    ConnectionFailed,
    Disconnected,
    ErrorsReported,
    Event,
    ReadingFailed,
    ReadingTaken,
    SetupChanged,
    SetupFailed,
    Worker,
    WorkerFailed,
)

_LOG = logging.getLogger(__name__)

_POLL_MS = 50
_MAX_EVENTS_PER_TICK = 200
NO_READING = "--------"
_AUTO_RANGE = "Auto"
_FIXED = "Fixed"
_NOT_APPLICABLE = "—"
_FUNCTION_COLUMNS = 6

_VFD_BACKGROUND = "#06130f"
_VFD_FOREGROUND = "#4dffc3"
_READOUT_FONT_SIZE = 56
_FUNCTION_FONT_SIZE = 16
_SETUP_FONT_SIZE = 11


def _vfd_label(parent: tk.Frame, text: str, font: tkfont.Font, *, anchor: Literal["w", "center"]) -> tk.Label:
    label = tk.Label(parent, text=text, font=font, bg=_VFD_BACKGROUND, fg=_VFD_FOREGROUND, anchor=anchor)
    label.pack(fill="x")
    return label


class MainWindow:
    """Shows Continuous Readings from one Meter. All Meter traffic goes through its Worker thread (ADR-0002)."""

    def __init__(self, root: tk.Tk, open_transport: Callable[[], Transport], resource: str) -> None:
        self.root = root
        self._resource = resource
        self._events: queue.Queue[Event] = queue.Queue()
        self._worker = Worker(open_transport, self._events)
        self._rate = ReadingRate()
        self._running = False
        self._connected = False
        self._ended = False  # the Connection failed or ended, so the status bar keeps saying why
        self._busy = False  # a Setup change is with the Worker; the controls wait for the Meter's answer
        self._closed = False
        self._poll_id: str | None = None
        self._setup: Setup | None = None  # what the Meter last reported it is doing
        self._last: ReadingTaken | None = None  # the Reading on the readout

        root.title(f"Agilent 34401A {__version__}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()

        self._worker.start()
        self._poll_id = root.after(_POLL_MS, self._drain)

    def _build(self) -> None:
        self._build_display()
        self._build_function_buttons()
        self._build_controls()
        self._build_status_bar()

    def _build_display(self) -> None:
        display = tk.Frame(self.root, bg=_VFD_BACKGROUND, padx=24, pady=12)
        display.pack(fill="x")
        fonts = {}
        for name, size in (
            ("readout", _READOUT_FONT_SIZE),
            ("function", _FUNCTION_FONT_SIZE),
            ("setup", _SETUP_FONT_SIZE),
        ):
            fonts[name] = tkfont.nametofont("TkFixedFont").copy()
            fonts[name].configure(size=size, weight="bold")
        self._fonts = fonts  # Tk drops a font when the last reference goes
        self.function_label = _vfd_label(display, Function.DC_VOLTAGE.label, fonts["function"], anchor="w")
        self.setup_label = _vfd_label(display, "", fonts["setup"], anchor="w")
        self.readout = _vfd_label(display, NO_READING, fonts["readout"], anchor="center")

    def _build_function_buttons(self) -> None:
        frame = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        frame.pack(fill="x")
        self._function_var = tk.StringVar()
        self.function_buttons: dict[Function, ttk.Radiobutton] = {}
        for index, function in enumerate(Function):
            button = ttk.Radiobutton(
                frame,
                text=function.label,
                value=function.value,
                variable=self._function_var,
                style="Toolbutton",
                command=partial(self._on_function, function),
                state="disabled",
            )
            button.grid(row=index // _FUNCTION_COLUMNS, column=index % _FUNCTION_COLUMNS, sticky="ew", padx=2, pady=2)
            self.function_buttons[function] = button
        for column in range(_FUNCTION_COLUMNS):
            frame.columnconfigure(column, weight=1)

    def _build_controls(self) -> None:
        controls = ttk.Frame(self.root, padding=8)
        controls.pack(fill="x")
        self.run_button = ttk.Button(controls, text="Run", command=self._toggle_run, state="disabled")
        self.run_button.pack(side="left", padx=(0, 12))
        self.range_box = self._combobox(controls, "Range", self._on_range)
        self.resolution_box = self._combobox(controls, "Resolution", self._on_resolution)
        self.nplc_box = self._combobox(controls, "Integration Time", self._on_nplc)
        self._raw = tk.BooleanVar(value=False)
        self.raw_check = ttk.Checkbutton(controls, text="Raw Reading", variable=self._raw, command=self._render_readout)
        self.raw_check.pack(side="left", padx=(12, 0))

    def _combobox(self, parent: ttk.Frame, title: str, handler: Callable[[], None]) -> ttk.Combobox:
        ttk.Label(parent, text=title).pack(side="left", padx=(0, 4))
        box = ttk.Combobox(parent, state="disabled", width=10)
        box.bind("<<ComboboxSelected>>", lambda _event: handler())
        box.pack(side="left", padx=(0, 12))
        return box

    def _build_status_bar(self) -> None:
        status = ttk.Frame(self.root, relief="sunken", padding=(6, 2))
        status.pack(fill="x", side="bottom")
        self.status_connection = ttk.Label(status, text="Connecting…")
        self.status_identity = ttk.Label(status, text="")
        self.status_message = ttk.Label(status, text="")
        self.status_error = ttk.Label(status, text="", foreground="#b00020")
        self.status_rate = ttk.Label(status, text="", anchor="e")
        self.status_connection.pack(side="left", padx=(0, 12))
        self.status_identity.pack(side="left", padx=(0, 12))
        self.status_rate.pack(side="right")
        self.status_error.pack(side="right", padx=(0, 12))
        self.status_message.pack(side="left", fill="x", expand=True)

    def close(self) -> None:
        """Shut the Worker down, then destroy the window. Safe to call more than once."""
        if self._closed:
            return
        self.stop()
        self.root.destroy()

    def worker_is_alive(self) -> bool:
        return self._worker.is_alive()

    def stop(self) -> None:
        """Shut the Worker down and stop polling it, leaving the window itself alone."""
        self._closed = True
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
            self._poll_id = None
        if not self._worker.shutdown():
            _LOG.warning("The Worker is still finishing a Reading; it will close the Transport when it is done")

    def _toggle_run(self) -> None:
        self._set_running(running=not self._running)

    def _set_running(self, *, running: bool) -> None:
        if running:
            self._worker.start_continuous()
        else:
            self._worker.pause()
        self._running = running
        self.run_button.configure(text="Pause" if running else "Run")
        if not running:
            self._rate.reset()
            self.status_rate.configure(text="")

    def _begin_change(self) -> None:
        """Lock the controls until the Worker reports the Setup the Meter ended up in."""
        self._busy = True
        if self._setup is not None:
            self._sync_controls(self._setup)

    def _request(self, change: Callable[[Setup], Setup]) -> None:
        """Ask the Worker to put the Meter in the Setup `change` makes of the one it last reported."""
        if self._setup is None or self._busy:
            return
        try:
            wanted = change(self._setup)
        except InvalidSetupError as error:
            self.status_error.configure(text=str(error))
            return
        self._begin_change()
        self._worker.apply_setup(wanted)

    def _on_function(self, function: Function) -> None:
        if self._setup is None or self._busy:
            return
        self._begin_change()
        self._worker.select_function(function)

    def _on_range(self) -> None:
        index = self.range_box.current()
        self._request(lambda setup: setup.with_range(None if index <= 0 else setup.function.ranges[index - 1]))

    def _on_resolution(self) -> None:
        index = self.resolution_box.current()
        if index >= 0:
            self._request(lambda setup: setup.with_resolution(list(Resolution)[index]))

    def _on_nplc(self) -> None:
        index = self.nplc_box.current()
        if index >= 0:
            self._request(lambda setup: setup.with_nplc(NPLC_VALUES[index]))

    def _drain(self) -> None:
        for _ in range(_MAX_EVENTS_PER_TICK):
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._handle(event)
        self._poll_id = self.root.after(_POLL_MS, self._drain)

    def _handle(self, event: Event) -> None:
        match event:
            case ReadingTaken(timestamp=timestamp):
                self._last = event
                self._render_readout()
                self.status_message.configure(text="")
                if self._running:
                    self._rate.add(timestamp)
                    rate = self._rate.per_second()
                    self.status_rate.configure(text="" if rate is None else f"{rate:.1f} Readings/s")
            case ReadingFailed(message):
                self.status_message.configure(text=f"Reading lost: {message}")
            case _:
                self._handle_connection_event(event)

    def _handle_connection_event(self, event: Event) -> None:
        """Handle what changes the Connection or the Setup, as opposed to a Reading."""
        match event:
            case Connected(identity, setup):
                self.status_connection.configure(text=f"Connected · {self._resource}")
                self.status_identity.configure(
                    text=f"{identity.manufacturer} {identity.model} · firmware {identity.firmware}"
                )
                self._connected = True
                self.run_button.configure(state="normal")
                self._show_setup(setup)
                self._set_running(running=True)  # a window that connects starts showing Readings straight away
            case SetupChanged(setup):
                self._busy = False
                self.status_error.configure(text="")
                self._show_setup(setup)
            case ErrorsReported(errors):
                first = errors[0]
                more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
                self.status_error.configure(text=f"Meter error {first.code}: {first.message}{more}")
            case SetupFailed(message):
                self._busy = False
                self.status_error.configure(text=f"Setup change failed: {message}")
                if self._setup is not None:
                    self._show_setup(self._setup)  # take the controls back to what the Meter last reported
            case ConnectionFailed(message):
                self._end(f"Connection failed: {message}")
            case WorkerFailed(message):
                self._end(f"Failed: {message}")
            case Disconnected():
                if not self._ended:
                    self._end("Disconnected")

    def _render_readout(self) -> None:
        taken = self._last
        if taken is None:
            text = NO_READING
        elif self._raw.get():
            text = taken.reading.raw.strip()
        else:
            text = format_reading(taken.reading, taken.setup.resolution)
        self.readout.configure(text=text)

    def _show_setup(self, setup: Setup) -> None:
        """Show the Setup the Meter reported on the readout and in the controls."""
        if self._setup is not None and self._setup.function is not setup.function:
            self._last = None  # a Reading of the old Function means nothing under the new one
            self._render_readout()
        self._setup = setup
        self._function_var.set(setup.function.value)
        self.function_label.configure(text=setup.function.label)
        self.setup_label.configure(text=describe_setup(setup))
        self._sync_controls(setup)

    def _sync_controls(self, setup: Setup) -> None:
        function = setup.function
        enabled = self._connected and not self._ended and not self._busy
        for button in self.function_buttons.values():
            button.configure(state="normal" if enabled else "disabled")
        if function.ranges:
            ranges = [format_range(function, value) for value in function.ranges]
            self._fill(
                self.range_box,
                [_AUTO_RANGE, *ranges],
                _AUTO_RANGE if setup.range is None else format_range(function, setup.range),
                applicable=enabled,
            )
        else:
            self._fill(self.range_box, [], _FIXED, applicable=False)
        self._fill(
            self.resolution_box,
            [resolution.label for resolution in Resolution],
            setup.resolution.label,
            applicable=enabled and function.fixed_resolution is None,
        )
        if function.has_integration_time:
            self._fill(
                self.nplc_box,
                [f"{nplc:g} NPLC" for nplc in NPLC_VALUES],
                f"{setup.nplc:g} NPLC",
                applicable=enabled,
            )
        else:
            self._fill(self.nplc_box, [], _NOT_APPLICABLE, applicable=False)

    @staticmethod
    def _fill(box: ttk.Combobox, values: list[str], shown: str, *, applicable: bool) -> None:
        box.configure(values=values, state="readonly" if applicable else "disabled")
        box.set(shown)

    def _end(self, state: str) -> None:
        self._ended = True
        self.status_connection.configure(text=state)
        self.run_button.configure(state="disabled")
        self._set_running(running=False)
        if self._setup is not None:
            self._sync_controls(self._setup)
        else:
            for button in self.function_buttons.values():
                button.configure(state="disabled")
