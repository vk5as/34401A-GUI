"""The connection dialog: choose the Backend, the resource (or the Simulator) and connect.

Looking at what VISA can see (detecting Backends, Scanning for resources) can take seconds, so both run on a
short-lived helper thread that never touches Tk or a Meter's Transport; the answer is picked up with `after()`.
"""

import contextlib
import logging
import queue
import re
import threading
import tkinter as tk
from collections.abc import Callable, Mapping
from functools import partial
from tkinter import ttk
from typing import Any, TypeVar

from agilent34401a.backend import Backend, BackendStatus, resolve_backend
from agilent34401a.connection import BackendScan, ConnectionSettings
from agilent34401a.errors import BackendUnavailableError
from agilent34401a.probe import ProbeJob, ProbeProgress, ProbeResult, start_probe
from agilent34401a.serial_config import (
    BAUD_RATES,
    FlowControl,
    Framing,
    Parity,
    SerialSettings,
    Terminator,
)
from agilent34401a.settings import LastConnection

_LOG = logging.getLogger(__name__)

_POLL_MS = 20
_GPIB_RESOURCE = re.compile(r"GPIB(\d+)::(\d+)::INSTR", re.IGNORECASE)
_SERIAL_RESOURCE = re.compile(r"ASRL(.+?)(?:::INSTR)?", re.IGNORECASE)
_AUTOMATIC, _ASSERTED, _UNASSERTED = "Automatic", "Asserted", "Unasserted"
_LINE_CHOICES = {_AUTOMATIC: None, _ASSERTED: True, _UNASSERTED: False}
_PROBE_BLOCKED = "Disconnect from the Meter first (File, Disconnect): Probe needs the serial port to itself."

Detect = Callable[[], Mapping[Backend, BackendStatus]]
"""Reports whether each Backend loads here; it may take a moment and runs off the Tk thread."""
Scan = Callable[[Backend], Mapping[Backend, BackendScan]]
"""Lists the resources the given Backend (every one, for Auto) can see; it runs off the Tk thread."""
StartProbe = Callable[[SerialSettings, Backend, bool], ProbeJob]
"""Makes a Probe of the given port through the given Backend, optionally including Flow Control; not yet started."""
OnConnect = Callable[[LastConnection, bool], None]
"""Receives the Connection the user chose, and whether to reconnect to it at the next start."""

_T = TypeVar("_T")


def _always() -> bool:
    return True


def _nobody_is_listening(_choice: LastConnection, _auto_reconnect: bool) -> None:  # noqa: FBT001
    """Stand in for `on_connect` once the dialog is closed."""


class _Background:
    """Runs functions on helper threads and hands their outcomes back to the Tk thread.

    A helper thread never holds the callbacks that answer (they are the dialog's bound methods): it only holds a
    number, and the callbacks wait here until the Tk thread collects them, or `close` drops them on the Tk thread.
    Otherwise the last reference to a closed dialog could be dropped by a helper thread that finishes late, and the
    dialog's Tk variables would be finalised on the wrong thread (ADR-0008).
    """

    def __init__(self, root: tk.Misc) -> None:
        self._root = root
        self._outcomes: queue.Queue[tuple[int, bool, object]] = queue.Queue()  # (job, worked, result or message)
        self._callbacks: dict[int, tuple[Callable[[Any], None], Callable[[str], None]]] = {}
        self._jobs = 0
        self._poll_id: str | None = None
        self._closed = False

    def run(self, work: Callable[[], _T], done: Callable[[_T], None], failed: Callable[[str], None]) -> None:
        """Run `work` on a helper thread, then call `done` with its result or `failed` with why it failed, on Tk's thread.

        `work` runs on the helper thread, so it must not hold the dialog.
        """
        self._jobs += 1
        job = self._jobs
        self._callbacks[job] = (done, failed)
        outcomes = self._outcomes

        def main() -> None:
            try:
                result = work()
            except Exception as error:  # noqa: BLE001 - whatever goes wrong is shown to the user, never lost
                _LOG.exception("A connection dialog lookup failed")
                outcomes.put((job, False, f"{type(error).__name__}: {error}"))
            else:
                outcomes.put((job, True, result))

        threading.Thread(target=main, name="agilent34401a-lookup", daemon=True).start()
        if self._poll_id is None:
            self._poll_id = self._root.after(_POLL_MS, self._poll)

    def close(self) -> None:
        self._closed = True
        self._callbacks.clear()
        while not self._outcomes.empty():
            self._outcomes.get_nowait()
        if self._poll_id is not None:
            with contextlib.suppress(tk.TclError):  # the whole application may already be gone
                self._root.after_cancel(self._poll_id)
            self._poll_id = None

    def _poll(self) -> None:
        self._poll_id = None
        while not self._closed:
            try:
                job, worked, payload = self._outcomes.get_nowait()
            except queue.Empty:
                break
            done, failed = self._callbacks.pop(job)
            if worked:
                done(payload)
            else:
                failed(str(payload))
        if not self._closed and self._callbacks:
            self._poll_id = self._root.after(_POLL_MS, self._poll)


class ConnectionDialog:
    """Asks which Meter to connect to, and calls `on_connect` with the answer.

    `detect` and `scan` are the seams for what VISA can see; `initial` and `auto_reconnect` pre-fill the dialog
    from the settings. The dialog closes itself when the user connects or cancels.
    """

    def __init__(  # noqa: PLR0913 - each one is something the window decides and the dialog only shows
        self,
        parent: tk.Tk | tk.Toplevel,
        *,
        detect: Detect,
        scan: Scan,
        on_connect: OnConnect,
        initial: LastConnection | None = None,
        auto_reconnect: bool = False,
        probe: StartProbe = start_probe,
        probe_allowed: Callable[[], bool] = _always,
    ) -> None:
        self._detect = detect
        self._scan = scan
        self._on_connect = on_connect
        self._start_probe = probe
        self._probe_allowed = probe_allowed
        self._job: ProbeJob | None = None
        self._probe_poll_id: str | None = None
        self._cancelling = False
        self._statuses: Mapping[Backend, BackendStatus] = {}
        self._detected = False
        self._found: list[tuple[Backend, str]] = []
        self.window = tk.Toplevel(parent)
        self.window.title("Connect to Meter")
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._background = _Background(self.window)
        self._build()
        self._fill_from(initial, auto_reconnect=auto_reconnect)
        self._background.run(self._detect, self._on_detected, self._on_detection_failed)

    @property
    def detected(self) -> bool:
        """Whether the Backends have been checked (successfully or not)."""
        return self._detected

    @property
    def is_open(self) -> bool:
        return bool(self.window.winfo_exists())

    def close(self) -> None:
        """Close the dialog without connecting. Safe to call more than once."""
        self._on_connect = _nobody_is_listening  # the window's bound method would keep the window and dialog alive
        self._probe_allowed = _always
        self._stop_probe()
        self._background.close()
        if self.is_open:
            self.window.destroy()
        # Tk variables must be freed on the Tk thread. Left to the garbage collector they can be freed on whichever
        # thread happens to trigger it (a Worker's, say), which Tk rejects.
        with contextlib.suppress(AttributeError):
            del self._target
            del self.auto_reconnect_var
            del self.include_flow_var

    def _build(self) -> None:
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        self._target = tk.StringVar(self.window, value="meter")
        targets = ttk.Frame(frame)
        targets.grid(row=0, column=0, columnspan=3, sticky="w")
        self.meter_radio = ttk.Radiobutton(
            targets, text="Meter (GPIB)", value="meter", variable=self._target, command=self._on_target
        )
        self.serial_radio = ttk.Radiobutton(
            targets, text="Meter (RS-232)", value="serial", variable=self._target, command=self._on_target
        )
        self.simulator_radio = ttk.Radiobutton(
            targets, text="Simulator", value="simulator", variable=self._target, command=self._on_target
        )
        self.meter_radio.pack(side="left", padx=(0, 12))
        self.serial_radio.pack(side="left", padx=(0, 12))
        self.simulator_radio.pack(side="left")

        ttk.Label(frame, text="Backend").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.backend_box = ttk.Combobox(frame, state="readonly", values=[b.label for b in Backend])
        self.backend_box.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        self.backend_box.bind("<<ComboboxSelected>>", lambda _event: self._show_backend_status())
        self.backend_status = ttk.Label(frame, text="Checking which Backends are installed…", wraplength=420)
        self.backend_status.grid(row=2, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="GPIB board").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.board_box = ttk.Spinbox(frame, from_=0, to=9, width=6)
        self.board_box.grid(row=3, column=1, sticky="w", pady=(10, 0))
        ttk.Label(frame, text="GPIB address").grid(row=4, column=0, sticky="w")
        self.address_box = ttk.Spinbox(frame, from_=0, to=30, width=6)
        self.address_box.grid(row=4, column=1, sticky="w")

        ttk.Label(frame, text="Resource string").grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.resource_entry = ttk.Entry(frame)
        self.resource_entry.grid(row=5, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(frame, text="Optional: used instead of the GPIB board and address.").grid(
            row=6, column=1, columnspan=2, sticky="w"
        )

        self.scan_button = ttk.Button(frame, text="Scan", command=self._on_scan)
        self.scan_button.grid(row=7, column=0, sticky="nw", pady=(10, 0))
        self.resource_list = tk.Listbox(frame, height=5, exportselection=False)
        self.resource_list.grid(row=7, column=1, columnspan=2, sticky="nsew", pady=(10, 0))
        self.resource_list.bind("<<ListboxSelect>>", lambda _event: self._on_resource_chosen())
        self.scan_status = ttk.Label(frame, text="", wraplength=420)
        self.scan_status.grid(row=8, column=1, columnspan=2, sticky="w")

        self._build_serial(frame).grid(row=9, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        self.auto_reconnect_var = tk.BooleanVar(self.window, value=False)
        self.auto_reconnect_check = ttk.Checkbutton(
            frame, text="Reconnect to this Meter when the application starts", variable=self.auto_reconnect_var
        )
        self.auto_reconnect_check.grid(row=10, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self.error_label = ttk.Label(frame, text="", foreground="#b00020", wraplength=420)
        self.error_label.grid(row=11, column=0, columnspan=3, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=12, column=0, columnspan=3, sticky="e", pady=(10, 0))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.close)
        self.connect_button = ttk.Button(buttons, text="Connect", command=self._on_connect_clicked, default="active")
        self.cancel_button.pack(side="right")
        self.connect_button.pack(side="right", padx=(0, 8))
        self.window.bind("<Return>", lambda _event: self.connect_button.invoke())
        self.window.bind("<Escape>", lambda _event: self.cancel_button.invoke())

    def _build_serial(self, parent: ttk.Frame) -> ttk.LabelFrame:
        """Build the RS-232 section: the serial parameters, and Probe with its progress bar and Cancel."""
        group = ttk.LabelFrame(parent, text="RS-232", padding=8)
        group.columnconfigure(1, weight=1)
        group.columnconfigure(3, weight=1)

        def choice(row: int, column: int, label: str, values: list[str], width: int = 12) -> ttk.Combobox:
            ttk.Label(group, text=label).grid(row=row, column=column, sticky="w", padx=(0 if column == 0 else 12, 6))
            box = ttk.Combobox(group, state="readonly", values=values, width=width)
            box.grid(row=row, column=column + 1, sticky="ew", pady=2)
            return box

        ttk.Label(group, text="Port").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.port_box = ttk.Combobox(group, values=[])
        self.port_box.grid(row=0, column=1, columnspan=3, sticky="ew", pady=2)
        self.baud_box = choice(1, 0, "Baud rate", [str(rate) for rate in BAUD_RATES])
        self.data_bits_box = choice(1, 2, "Data bits", ["7", "8"])
        self.parity_box = choice(2, 0, "Parity", [parity.label for parity in Parity])
        self.stop_bits_box = choice(2, 2, "Stop bits", ["1", "2"])
        self.flow_box = choice(3, 0, "Flow Control", [flow.label for flow in FlowControl])
        self.terminator_box = choice(3, 2, "Terminator", [terminator.label for terminator in Terminator])
        self.dtr_box = choice(4, 0, "DTR line", list(_LINE_CHOICES))
        self.rts_box = choice(4, 2, "RTS line", list(_LINE_CHOICES))

        probing = ttk.Frame(group)
        probing.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        probing.columnconfigure(2, weight=1)
        self.probe_button = ttk.Button(probing, text="Probe", command=self._on_probe)
        self.probe_button.grid(row=0, column=0, sticky="w")
        self.probe_cancel_button = ttk.Button(probing, text="Cancel Probe", command=self._on_probe_cancel)
        self.probe_cancel_button.grid(row=0, column=1, sticky="w", padx=(6, 12))
        self.include_flow_var = tk.BooleanVar(self.window, value=False)
        self.include_flow_check = ttk.Checkbutton(
            probing, text="Include Flow Control (slower)", variable=self.include_flow_var
        )
        self.include_flow_check.grid(row=0, column=2, sticky="w")
        self.probe_progress = ttk.Progressbar(probing, mode="determinate", maximum=1, value=0)
        self.probe_progress.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.probe_status = ttk.Label(probing, text="", wraplength=420)
        self.probe_status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        return group

    def _fill_from(self, initial: LastConnection | None, *, auto_reconnect: bool) -> None:
        settings = initial.connection if initial is not None else ConnectionSettings()
        if initial is not None and initial.simulate:
            target = "simulator"
        else:
            target = "meter" if settings.serial is None else "serial"
        self._target.set(target)
        self._show_serial(settings.serial if settings.serial is not None else SerialSettings(port="9600"))
        self.port_box.set("" if settings.serial is None else settings.serial.port)
        self.backend_box.current(list(Backend).index(settings.backend))
        self.board_box.set(str(settings.gpib_board))
        self.address_box.set(str(settings.gpib_address))
        if settings.resource is not None:
            self.resource_entry.insert(0, settings.resource)
        self.auto_reconnect_var.set(auto_reconnect)
        self._on_target()

    def _on_target(self) -> None:
        """Only a Meter needs a Backend and a way to find it; the Simulator needs neither.

        A GPIB Meter takes an address or a resource string, an RS-232 Meter a port and its serial parameters.
        """
        target = self._target.get()
        meter = target != "simulator"
        gpib = target == "meter"
        serial = target == "serial"
        self.backend_box.configure(state="readonly" if meter else "disabled")
        for widget in (self.board_box, self.address_box, self.resource_entry):
            widget.configure(state="normal" if gpib else "disabled")
        self.scan_button.configure(state="normal" if meter else "disabled")
        self.resource_list.configure(state="normal" if meter else "disabled")
        self.port_box.configure(state="normal" if serial else "disabled")
        for box in (self.baud_box, self.data_bits_box, self.parity_box, self.stop_bits_box):
            box.configure(state="readonly" if serial else "disabled")
        for box in (self.flow_box, self.terminator_box, self.dtr_box, self.rts_box):
            box.configure(state="readonly" if serial else "disabled")
        self.include_flow_check.configure(state="normal" if serial else "disabled")
        self.probe_button.configure(state="normal" if serial and self._job is None else "disabled")
        self.probe_cancel_button.configure(state="normal" if self._job is not None else "disabled")

    def _show_serial(self, serial: SerialSettings) -> None:
        """Put `serial`'s parameters (everything but the port) in the RS-232 section."""
        self.baud_box.set(str(serial.baud))
        self.data_bits_box.set(str(serial.framing.data_bits))
        self.parity_box.set(serial.framing.parity.label)
        self.stop_bits_box.set(str(serial.framing.stop_bits))
        self.flow_box.set(serial.flow_control.label)
        self.terminator_box.set(serial.terminator.label)
        lines = {value: label for label, value in _LINE_CHOICES.items()}
        self.dtr_box.set(lines[serial.dtr])
        self.rts_box.set(lines[serial.rts])

    def _on_detected(self, statuses: Mapping[Backend, BackendStatus]) -> None:
        self._statuses = statuses
        self._detected = True
        selected = self.backend_box.current()
        values = [
            (
                backend.label
                if backend is Backend.AUTO
                else f"{backend.label} ({'available' if self._is_available(backend) else 'not available'})"
            )
            for backend in Backend
        ]
        self.backend_box.configure(values=values)
        self.backend_box.current(selected)
        self._show_backend_status()

    def _is_available(self, backend: Backend) -> bool:
        status = self._statuses.get(backend)
        return status is not None and status.available

    def _on_detection_failed(self, message: str) -> None:
        self._detected = True
        self.backend_status.configure(text=f"Could not check which Backends are installed: {message}")

    def _selected_backend(self) -> Backend:
        return list(Backend)[self.backend_box.current()]

    def _show_backend_status(self) -> None:
        if not self._statuses:
            return
        backend = self._selected_backend()
        if backend is Backend.AUTO:
            try:
                text = f"Auto will use {resolve_backend(backend, self._statuses).label}."
            except BackendUnavailableError as error:
                text = str(error)
        else:
            status = self._statuses[backend]
            text = f"{backend.label} was detected." if status.available else f"{backend.label}: {status.reason}"
        self.backend_status.configure(text=text)

    def _on_scan(self) -> None:
        self.scan_button.configure(state="disabled")
        self.scan_status.configure(text="Scanning…")
        backend = self._selected_backend()
        self._background.run(partial(self._scan, backend), self._on_scanned, self._on_scan_failed)

    def _on_scanned(self, scans: Mapping[Backend, BackendScan]) -> None:
        self.scan_button.configure(state="normal")
        found: list[tuple[Backend, str]] = []
        problems: list[str] = []
        for backend, scanned in scans.items():
            found.extend((backend, name) for name in scanned.resources)
            if scanned.problem:
                problems.append(f"{backend.label}: {scanned.problem}")
        self._found = found
        self.resource_list.delete(0, "end")
        for backend, name in found:
            self.resource_list.insert("end", f"{name}  ({backend.label})")
        ports = [port for _, name in found if (port := _serial_port(name)) is not None]
        self.port_box.configure(values=list(dict.fromkeys(ports)))
        count = len(found)
        summary = "No resources found." if count == 0 else f"Found {count} resource{'' if count == 1 else 's'}."
        self.scan_status.configure(text=" ".join([summary, *problems]))

    def _on_scan_failed(self, message: str) -> None:
        self.scan_button.configure(state="normal")
        self.scan_status.configure(text=f"Scan failed: {message}")

    def _on_resource_chosen(self) -> None:
        selection = self.resource_list.curselection()  # type: ignore[no-untyped-call]  # typeshed leaves it untyped
        if not selection:
            return
        backend, name = self._found[selection[0]]
        self.backend_box.current(list(Backend).index(backend))
        self._show_backend_status()
        port = _serial_port(name)
        if port is not None:
            self._target.set("serial")
            self._on_target()
            self.port_box.set(port)
            return
        self._target.set("meter")
        self._on_target()
        self.resource_entry.delete(0, "end")
        gpib = _GPIB_RESOURCE.fullmatch(name)
        if gpib is None:
            self.resource_entry.insert(0, name)
        else:
            self.board_box.set(gpib.group(1))
            self.address_box.set(gpib.group(2))

    def _on_connect_clicked(self) -> None:
        simulate = self._target.get() == "simulator"
        try:
            settings = self._serial_connection() if self._target.get() == "serial" else self._settings()
        except ValueError as error:
            if not simulate:
                self.error_label.configure(text=str(error))
                return
            settings = ConnectionSettings()  # the Simulator ignores the Meter fields, so they cannot be wrong
        self.error_label.configure(text="")
        choice = LastConnection(simulate=simulate, connection=settings)
        auto_reconnect = bool(self.auto_reconnect_var.get())
        on_connect = self._on_connect
        self.close()
        on_connect(choice, auto_reconnect)

    def _settings(self) -> ConnectionSettings:
        try:
            board = int(self.board_box.get())
            address = int(self.address_box.get())
        except ValueError:
            message = "The GPIB board and address must be whole numbers"
            raise ValueError(message) from None
        return ConnectionSettings(
            backend=self._selected_backend(),
            resource=self.resource_entry.get().strip() or None,
            gpib_board=board,
            gpib_address=address,
        )

    def _serial_connection(self) -> ConnectionSettings:
        return ConnectionSettings(backend=self._selected_backend(), serial=self._serial_settings())

    def _serial_settings(self) -> SerialSettings:
        """Read the RS-232 section; `ValueError` says what is wrong, such as a blank port."""
        port = self.port_box.get().strip()
        parity = next(parity for parity in Parity if parity.label == self.parity_box.get())
        return SerialSettings(
            port=port,
            baud=int(self.baud_box.get()),
            framing=Framing(int(self.data_bits_box.get()), parity, int(self.stop_bits_box.get())),
            flow_control=next(flow for flow in FlowControl if flow.label == self.flow_box.get()),
            terminator=next(terminator for terminator in Terminator if terminator.label == self.terminator_box.get()),
            dtr=_LINE_CHOICES[self.dtr_box.get()],
            rts=_LINE_CHOICES[self.rts_box.get()],
        )

    def _on_probe(self) -> None:
        """Start a Probe of the port on its own thread, which tries the settings the Meter might answer at."""
        if self._job is not None:
            return
        if not self._probe_allowed():
            self.probe_status.configure(text=_PROBE_BLOCKED)
            return
        try:
            base = self._serial_settings()
        except ValueError as error:
            self.probe_status.configure(text=str(error))
            return
        try:
            job = self._start_probe(base, self._selected_backend(), bool(self.include_flow_var.get()))
            job.start()
        except Exception as error:  # noqa: BLE001 - whatever goes wrong is shown to the user, never lost
            _LOG.exception("Could not start the Probe")
            self.probe_status.configure(text=f"Could not start Probe: {type(error).__name__}: {error}")
            return
        self._job = job
        self._cancelling = False
        self.probe_progress.configure(value=0, maximum=1)
        self.probe_status.configure(text="Starting Probe…")
        self.connect_button.configure(state="disabled")  # Probe has the port until it is done
        self._on_target()
        self._probe_poll_id = self.window.after(_POLL_MS, self._poll_probe)

    def _on_probe_cancel(self) -> None:
        if self._job is None or self._cancelling:
            return
        self._cancelling = True
        self._job.cancel()
        self.probe_status.configure(text="Cancelling…")

    def _poll_probe(self) -> None:
        self._probe_poll_id = None
        job = self._job
        while job is not None:
            try:
                event = job.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, ProbeResult):
                self._on_probe_done(event)
                return
            self._on_probe_progress(event)
        if self._job is not None:
            self._probe_poll_id = self.window.after(_POLL_MS, self._poll_probe)

    def _on_probe_progress(self, progress: ProbeProgress) -> None:
        if self._cancelling:
            return
        self.probe_progress.configure(maximum=progress.total, value=progress.attempt)
        self.probe_status.configure(text=progress.message)

    def _on_probe_done(self, result: ProbeResult) -> None:
        self._job = None
        self._cancelling = False
        if result.found is not None:
            self._show_serial(result.found)
            self.probe_progress.configure(value=result.total)
        self.probe_status.configure(text=result.message)
        self.connect_button.configure(state="normal")
        self._on_target()

    def _stop_probe(self) -> None:
        """Cancel a Probe still running and stop listening to it (the dialog is closing)."""
        if self._probe_poll_id is not None:
            with contextlib.suppress(tk.TclError):  # the whole application may already be gone
                self.window.after_cancel(self._probe_poll_id)
            self._probe_poll_id = None
        if self._job is not None:
            self._job.cancel()
            self._job = None


def _serial_port(resource: str) -> str | None:
    """Return the port a scanned `ASRL` resource stands for (`ASRL/dev/ttyUSB0::INSTR` is `/dev/ttyUSB0`), or None."""
    match = _SERIAL_RESOURCE.fullmatch(resource)
    if match is None:
        return None
    name = match.group(1)
    return resource if name.isdigit() else name
