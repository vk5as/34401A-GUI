"""The main window: a VFD-style readout, Run/Pause and a status bar, driven by the Worker."""

import logging
import queue
import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont
from tkinter import ttk

from agilent34401a import __version__
from agilent34401a.meter import format_reading
from agilent34401a.rate import ReadingRate
from agilent34401a.transport import Transport
from agilent34401a.worker import (
    Connected,
    ConnectionFailed,
    Disconnected,
    Event,
    ReadingFailed,
    ReadingTaken,
    Worker,
    WorkerFailed,
)

_LOG = logging.getLogger(__name__)

_POLL_MS = 50
_MAX_EVENTS_PER_TICK = 200
NO_READING = "--------"

_VFD_BACKGROUND = "#06130f"
_VFD_FOREGROUND = "#4dffc3"
_READOUT_FONT_SIZE = 56
_FUNCTION_FONT_SIZE = 16


class MainWindow:
    """Shows Continuous Readings from one Meter. All Meter traffic goes through its Worker thread (ADR-0002)."""

    def __init__(self, root: tk.Tk, open_transport: Callable[[], Transport], resource: str) -> None:
        self.root = root
        self._resource = resource
        self._events: queue.Queue[Event] = queue.Queue()
        self._worker = Worker(open_transport, self._events)
        self._rate = ReadingRate()
        self._running = False
        self._ended = False  # the Connection failed or ended, so the status bar keeps saying why
        self._closed = False
        self._poll_id: str | None = None

        root.title(f"Agilent 34401A {__version__}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()

        self._worker.start()
        self._poll_id = root.after(_POLL_MS, self._drain)

    def _build(self) -> None:
        display = tk.Frame(self.root, bg=_VFD_BACKGROUND, padx=24, pady=12)
        display.pack(fill="both", expand=True)
        readout_font = tkfont.nametofont("TkFixedFont").copy()
        readout_font.configure(size=_READOUT_FONT_SIZE, weight="bold")
        function_font = tkfont.nametofont("TkFixedFont").copy()
        function_font.configure(size=_FUNCTION_FONT_SIZE, weight="bold")
        self._fonts = (readout_font, function_font)  # Tk drops a font when the last reference goes
        self.function_label = tk.Label(
            display, text="DC V", font=function_font, bg=_VFD_BACKGROUND, fg=_VFD_FOREGROUND, anchor="w"
        )
        self.function_label.pack(fill="x")
        self.readout = tk.Label(display, text=NO_READING, font=readout_font, bg=_VFD_BACKGROUND, fg=_VFD_FOREGROUND)
        self.readout.pack(fill="x")

        controls = ttk.Frame(self.root, padding=8)
        controls.pack(fill="x")
        self.run_button = ttk.Button(controls, text="Run", command=self._toggle_run, state="disabled")
        self.run_button.pack(side="left")

        status = ttk.Frame(self.root, relief="sunken", padding=(6, 2))
        status.pack(fill="x", side="bottom")
        self.status_connection = ttk.Label(status, text="Connecting…")
        self.status_identity = ttk.Label(status, text="")
        self.status_message = ttk.Label(status, text="")
        self.status_rate = ttk.Label(status, text="", anchor="e")
        self.status_connection.pack(side="left", padx=(0, 12))
        self.status_identity.pack(side="left", padx=(0, 12))
        self.status_rate.pack(side="right")
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
            case Connected(identity):
                self.status_connection.configure(text=f"Connected · {self._resource}")
                self.status_identity.configure(
                    text=f"{identity.manufacturer} {identity.model} · firmware {identity.firmware}"
                )
                self.run_button.configure(state="normal")
                self._set_running(running=True)  # a window that connects starts showing Readings straight away
            case ConnectionFailed(message):
                self._end(f"Connection failed: {message}")
            case WorkerFailed(message):
                self._end(f"Failed: {message}")
            case Disconnected():
                if not self._ended:
                    self._end("Disconnected")
            case ReadingTaken(reading, timestamp):
                self.readout.configure(text=format_reading(reading))
                self.status_message.configure(text="")
                if self._running:
                    self._rate.add(timestamp)
                    rate = self._rate.per_second()
                    self.status_rate.configure(text="" if rate is None else f"{rate:.1f} Readings/s")
            case ReadingFailed(message):
                self.status_message.configure(text=f"Reading lost: {message}")

    def _end(self, state: str) -> None:
        self._ended = True
        self.status_connection.configure(text=state)
        self.run_button.configure(state="disabled")
        self._set_running(running=False)
