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
from tkinter import ttk
from typing import TypeVar

from agilent34401a.backend import Backend, BackendStatus, resolve_backend
from agilent34401a.connection import BackendScan, ConnectionSettings
from agilent34401a.errors import BackendUnavailableError
from agilent34401a.settings import LastConnection

_LOG = logging.getLogger(__name__)

_POLL_MS = 20
_GPIB_RESOURCE = re.compile(r"GPIB(\d+)::(\d+)::INSTR", re.IGNORECASE)

Detect = Callable[[], Mapping[Backend, BackendStatus]]
"""Reports whether each Backend loads here; it may take a moment and runs off the Tk thread."""
Scan = Callable[[Backend], Mapping[Backend, BackendScan]]
"""Lists the resources the given Backend (every one, for Auto) can see; it runs off the Tk thread."""
OnConnect = Callable[[LastConnection, bool], None]
"""Receives the Connection the user chose, and whether to reconnect to it at the next start."""

_T = TypeVar("_T")


def _nobody_is_listening(_choice: LastConnection, _auto_reconnect: bool) -> None:  # noqa: FBT001
    """Stand in for `on_connect` once the dialog is closed."""


class _Background:
    """Runs functions on helper threads and hands their outcomes back to the Tk thread."""

    def __init__(self, root: tk.Misc) -> None:
        self._root = root
        self._outcomes: queue.Queue[Callable[[], None]] = queue.Queue()
        self._pending = 0  # helper threads whose outcome has not been handed back yet
        self._poll_id: str | None = None
        self._closed = False

    def run(self, work: Callable[[], _T], done: Callable[[_T], None], failed: Callable[[str], None]) -> None:
        def main() -> None:
            try:
                result = work()
            except Exception as error:  # noqa: BLE001 - whatever goes wrong is shown to the user, never lost
                _LOG.exception("A connection dialog lookup failed")
                message = f"{type(error).__name__}: {error}"
                outcome = lambda: failed(message)  # noqa: E731 - bound now, run later on the Tk thread
            else:
                outcome = lambda: done(result)  # noqa: E731
            if not self._closed:  # nobody is left to hear the answer, and keeping it would keep the dialog alive
                self._outcomes.put(outcome)

        self._pending += 1
        threading.Thread(target=main, name="agilent34401a-lookup", daemon=True).start()
        if self._poll_id is None:
            self._poll_id = self._root.after(_POLL_MS, self._poll)

    def close(self) -> None:
        self._closed = True
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
                outcome = self._outcomes.get_nowait()
            except queue.Empty:
                break
            self._pending -= 1
            outcome()
        if not self._closed and self._pending > 0:
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
    ) -> None:
        self._detect = detect
        self._scan = scan
        self._on_connect = on_connect
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
        self._background.close()
        if self.is_open:
            self.window.destroy()
        # Tk variables must be freed on the Tk thread. Left to the garbage collector they can be freed on whichever
        # thread happens to trigger it (a Worker's, say), which Tk rejects.
        with contextlib.suppress(AttributeError):
            del self._target
            del self.auto_reconnect_var

    def _build(self) -> None:
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        self._target = tk.StringVar(self.window, value="meter")
        targets = ttk.Frame(frame)
        targets.grid(row=0, column=0, columnspan=3, sticky="w")
        self.meter_radio = ttk.Radiobutton(
            targets, text="Meter", value="meter", variable=self._target, command=self._on_target
        )
        self.simulator_radio = ttk.Radiobutton(
            targets, text="Simulator", value="simulator", variable=self._target, command=self._on_target
        )
        self.meter_radio.pack(side="left", padx=(0, 12))
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

        self.auto_reconnect_var = tk.BooleanVar(self.window, value=False)
        self.auto_reconnect_check = ttk.Checkbutton(
            frame, text="Reconnect to this Meter when the application starts", variable=self.auto_reconnect_var
        )
        self.auto_reconnect_check.grid(row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self.error_label = ttk.Label(frame, text="", foreground="#b00020", wraplength=420)
        self.error_label.grid(row=10, column=0, columnspan=3, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, columnspan=3, sticky="e", pady=(10, 0))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.close)
        self.connect_button = ttk.Button(buttons, text="Connect", command=self._on_connect_clicked, default="active")
        self.cancel_button.pack(side="right")
        self.connect_button.pack(side="right", padx=(0, 8))
        self.window.bind("<Return>", lambda _event: self.connect_button.invoke())
        self.window.bind("<Escape>", lambda _event: self.cancel_button.invoke())

    def _fill_from(self, initial: LastConnection | None, *, auto_reconnect: bool) -> None:
        settings = initial.connection if initial is not None else ConnectionSettings()
        self._target.set("simulator" if initial is not None and initial.simulate else "meter")
        self.backend_box.current(list(Backend).index(settings.backend))
        self.board_box.set(str(settings.gpib_board))
        self.address_box.set(str(settings.gpib_address))
        if settings.resource is not None:
            self.resource_entry.insert(0, settings.resource)
        self.auto_reconnect_var.set(auto_reconnect)
        self._on_target()

    def _on_target(self) -> None:
        """Only a Meter needs a Backend, an address and a resource; the Simulator needs none of them."""
        meter = self._target.get() == "meter"
        self.backend_box.configure(state="readonly" if meter else "disabled")
        for widget in (self.board_box, self.address_box, self.resource_entry, self.scan_button):
            widget.configure(state="normal" if meter else "disabled")
        self.resource_list.configure(state="normal" if meter else "disabled")

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
        self._background.run(lambda: self._scan(backend), self._on_scanned, self._on_scan_failed)

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
            settings = self._settings()
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
