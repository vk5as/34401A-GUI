"""The main window: Function, Range and Resolution controls, a VFD-style readout and a status bar, driven by the Worker."""

import gc
import logging
import queue
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Literal

from agilent34401a import __version__
from agilent34401a.connection import detect_all_backends, open_transport, scan_resources
from agilent34401a.errors import InvalidSetupError
from agilent34401a.gui.chart_tab import install_chart
from agilent34401a.gui.connection_dialog import ConnectionDialog, Detect, Scan, StartProbe
from agilent34401a.gui.console import TITLE as CONSOLE_TITLE
from agilent34401a.gui.console import ConsoleTab
from agilent34401a.gui.math_tab import MathTab
from agilent34401a.gui.presets_tab import install_presets
from agilent34401a.gui.recording import install_recording
from agilent34401a.gui.sense_tab import SenseTab
from agilent34401a.gui.shortcuts import Shortcuts
from agilent34401a.gui.system_tab import SystemTab
from agilent34401a.gui.themes import ERROR_STYLE, Palette, apply_theme, style_menu
from agilent34401a.gui.trigger_tab import install_trigger
from agilent34401a.meter import (
    NPLC_VALUES,
    Function,
    Resolution,
    Setup,
    Terminals,
    describe_setup,
    format_range,
    format_reading,
)
from agilent34401a.probe import start_probe
from agilent34401a.rate import ReadingRate
from agilent34401a.settings import LastConnection, Settings, Theme
from agilent34401a.sim import DEMO_SIGNALS, Simulator
from agilent34401a.transport import Transport
from agilent34401a.worker import (
    Connected,
    ConnectionFailed,
    ConnectionLost,
    Disconnected,
    ErrorsReported,
    Event,
    ReadingFailed,
    ReadingTaken,
    SetupChanged,
    SetupFailed,
    TerminalsChanged,
    Worker,
    WorkerFailed,
)

_LOG = logging.getLogger(__name__)

_POLL_MS = 50
# When automatic garbage collection is off (the application turns it off, see gui/app.py) the window collects
# cyclic garbage itself, here on the Tk thread, every this many polls.
_GC_EVERY_TICKS = 100
_MAX_EVENTS_PER_TICK = 200
NO_READING = "--------"
_AUTO_RANGE = "Auto"
_FIXED = "Fixed"
_NOT_APPLICABLE = "—"
_FUNCTION_COLUMNS = 6
_MENU_ORDER = ("File", "View", "Help")
_CONNECT = "Connect…"
_DISCONNECT = "Disconnect"
_RECONNECT_HINT = f"Choose File > {_CONNECT} to connect again."
_SIMULATOR = "Simulator"
# Shortcuts for features that arrive later; each shows as unavailable in Help until its feature registers it.
_PLANNED_SHORTCUTS = (
    ("Space", "Take a single Reading"),
    ("Ctrl+L", "Start or stop recording"),
    ("Ctrl+K", "Open the SCPI console"),
    ("Ctrl+,", "Open Settings"),
)

_VFD_BACKGROUND = "#06130f"
_VFD_FOREGROUND = "#4dffc3"
FAIL_COLOUR = "#ff4d4d"
"""The readout's colour while a Reading fails a Limit Test (HI or LO)."""
_READOUT_FONT_SIZE = 56
_FUNCTION_FONT_SIZE = 16
_SETUP_FONT_SIZE = 11


def _vfd_label(parent: tk.Frame, text: str, font: tkfont.Font, *, anchor: Literal["w", "center"]) -> tk.Label:
    label = tk.Label(parent, text=text, font=font, bg=_VFD_BACKGROUND, fg=_VFD_FOREGROUND, anchor=anchor)
    label.pack(fill="x")
    return label


MakeOpener = Callable[[LastConnection], Callable[[], Transport]]
"""Turns the Connection the user chose into the function that opens its Transport on the Worker's thread."""


def opener_for(choice: LastConnection) -> Callable[[], Transport]:
    """Open a Connection the real way: the in-process Simulator, or the Meter through the chosen Backend."""
    if choice.simulate:
        return lambda: Simulator(signals=DEMO_SIGNALS)
    return lambda: open_transport(choice.connection)


def describe_connection(choice: LastConnection) -> str:
    """Name the resource of `choice` as the status bar shows it."""
    if choice.simulate:
        return _SIMULATOR
    serial = choice.connection.serial
    return choice.connection.resource_name if serial is None else serial.describe()


@dataclass(frozen=True)
class _Connection:
    """A Connection waiting for its turn: how to open it, what to call it, and whether to remember it."""

    open_transport: Callable[[], Transport]
    resource: str
    choice: LastConnection | None


class MainWindow:
    """Shows Continuous Readings from one Meter. All Meter traffic goes through its Worker thread (ADR-0002).

    Each Connection gets a Worker of its own, so the Worker that owns a Transport is the only thing that ever
    touches it, and a new Connection starts only after the previous one has been returned to Local and closed.
    Pass `open_transport` to connect straight away; otherwise the window starts not connected.
    """

    def __init__(  # noqa: PLR0913 - the seams (opener, detection, Scan) are what the tests replace
        self,
        root: tk.Tk | tk.Toplevel,
        open_transport: Callable[[], Transport] | None = None,
        resource: str = "",
        *,
        settings: Settings | None = None,
        make_opener: MakeOpener | None = None,
        detect: Detect | None = None,
        scan: Scan | None = None,
        probe: StartProbe | None = None,
    ) -> None:
        self.root = root
        self.settings = settings if settings is not None else Settings.in_memory()
        self._make_opener = make_opener or opener_for
        self._detect = detect or detect_all_backends
        self._scan = scan or scan_resources
        self._probe = probe or start_probe
        self.connection_dialog: ConnectionDialog | None = None
        self._resource = resource
        self._events: queue.Queue[Event] = queue.Queue()
        self._worker = self._new_worker()
        self._has_connection = False  # the Worker has been asked to connect and has not yet said `Disconnected`
        self._retiring = False  # the Worker has been asked to return the Meter to Local and close
        self._next: _Connection | None = None  # what to connect to once the Worker has finished
        self._choice: LastConnection | None = None  # the Connection to remember once it has connected
        self._event_handlers: list[Callable[[Event], None]] = []
        self._rate = ReadingRate()
        self._running = False
        self._connected = False
        self._ended = False  # the Connection failed or ended, so the status bar keeps saying why
        self._busy = False  # a Setup change is with the Worker; the controls wait for the Meter's answer
        self._closed = False
        self._poll_id: str | None = None
        self._setup: Setup | None = None  # what the Meter last reported it is doing
        self._last: ReadingTaken | None = None  # the Reading on the readout
        self._reading_listeners: list[Callable[[ReadingTaken], None]] = []
        self._close_callbacks: list[Callable[[], None]] = []
        self._tabs: dict[str, tuple[tk.Widget, tk.Widget | None]] = {}  # title -> (tab, widget to focus on show)

        root.title(f"Agilent 34401A {__version__}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()

        self._worker.start()
        self._ticks = 0
        self._poll_id = root.after(_POLL_MS, self._drain)
        if open_transport is not None:
            self._connect(_Connection(open_transport, resource, None))

    def _build(self) -> None:
        self._theme_callbacks: list[Callable[[Palette], None]] = []
        self._build_display()
        self._build_function_buttons()
        self._build_controls()
        self.menubar = tk.Menu(
            self.root, tearoff=False
        )  # attached to the window by add_menu_command once it has an entry
        self._menus: dict[str, tk.Menu] = {}
        self.palette = apply_theme(self.root, self.settings.theme)
        style_menu(self.menubar, self.palette)
        self._build_connection_menu()
        self.notebook = ttk.Notebook(self.root)  # shown by add_tab once there is a tab to show
        self._has_tabs = False
        self._build_status_bar()  # before the tabs, so a feature can put an indicator in it
        self._build_tabs()
        self._build_view_menu()
        self._layout()
        self._build_shortcuts()

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
        self._display = display

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
        self._controls = controls
        self._control_items: list[tuple[tk.Widget, dict[str, Any], bool]] = []  # widget, pack options, in compact mode
        self.run_button = ttk.Button(controls, text="Run", command=self._toggle_run, state="disabled")
        self._add_control(self.run_button, {"side": "left", "padx": (0, 12)}, compact=True)
        self.range_box = self._combobox(controls, "Range", self._on_range, compact=True)
        self.resolution_box = self._combobox(controls, "Resolution", self._on_resolution, compact=True)
        self.nplc_box = self._combobox(controls, "Integration Time", self._on_nplc, compact=True)
        self._raw = tk.BooleanVar(value=False)
        self.raw_check = ttk.Checkbutton(controls, text="Raw Reading", variable=self._raw, command=self._render_readout)
        self._add_control(self.raw_check, {"side": "left", "padx": (12, 0)}, compact=False)

    def _add_control(self, widget: tk.Widget, options: dict[str, Any], *, compact: bool) -> None:
        self._control_items.append((widget, options, compact))
        widget.pack(**options)

    def _combobox(self, parent: ttk.Frame, title: str, handler: Callable[[], None], *, compact: bool) -> ttk.Combobox:
        self._add_control(ttk.Label(parent, text=title), {"side": "left", "padx": (0, 4)}, compact=compact)
        box = ttk.Combobox(parent, state="disabled", width=10)
        box.bind("<<ComboboxSelected>>", lambda _event: handler())
        self._add_control(box, {"side": "left", "padx": (0, 12)}, compact=compact)
        return box

    def _build_tabs(self) -> None:
        """Create the tabs below the controls. Each feature adds its own line here, built in its own module."""
        self.chart = install_chart(self)
        self.recording = install_recording(self)
        self.sense_tab = SenseTab(self.notebook, self._request)
        self.add_tab("Sense", self.sense_tab.frame)
        self.trigger_tab = install_trigger(self)
        self.math_tab = MathTab(self.notebook, self._request, self._worker.reset_statistics)
        self.add_tab("Math", self.math_tab.frame)
        self.add_event_handler(self.math_tab.handle)
        self.presets_tab = install_presets(self)
        self._add_system_tab()
        # The console holds the Worker, not the window: a reference cycle through the window would leave Tk variables
        # to be freed by whichever thread the garbage collector happens to run on.
        worker = self._worker
        self.console = ConsoleTab(
            self.notebook, lambda command, allow: worker.send_raw(command, allow_calibration=allow)
        )
        self.add_tab(CONSOLE_TITLE, self.console.frame, focus=self.console.entry)
        self.add_event_handler(self.console.handle_event)

    def add_reading_listener(self, listener: Callable[[ReadingTaken], None]) -> None:
        """Call `listener` with every Reading as it is taken, on the GUI thread. Keep it cheap."""
        self._reading_listeners.append(listener)

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """Call `callback` once, on the GUI thread, when the window is closed (before the Worker is shut down)."""
        self._close_callbacks.append(callback)

    @property
    def controls(self) -> ttk.Frame:
        """The row of controls under the Function buttons; the parent for a widget given to `add_control`."""
        return self._controls

    def add_control(self, widget: tk.Widget, *, compact: bool = False) -> None:
        """Put `widget` (a child of `controls`) at the end of the controls row, hidden in compact mode unless asked."""
        self._add_control(widget, {"side": "left", "padx": (12, 0)}, compact=compact)

    @property
    def status_bar(self) -> ttk.Frame:
        """The status bar along the bottom; the parent for an indicator a feature adds to it."""
        return self._status_bar

    def _add_system_tab(self) -> None:
        tab = self.system_tab = SystemTab(self.notebook, self._worker)
        self.add_event_handler(tab.handle)
        self.add_tab("System", tab)

    @property
    def worker(self) -> Worker:
        """The Worker that owns the Connection, whichever Connection that is; tabs send their requests to it."""
        return self._worker

    def add_event_handler(self, handler: Callable[[Event], None]) -> None:
        """Have `handler` called, on the Tk thread, with every event the Worker reports (after the window's own)."""
        self._event_handlers.append(handler)

    def menu(self, name: str) -> tk.Menu:
        """Return the menu called `name`, creating it in its usual place (File, View, Help, then any others)."""
        existing = self._menus.get(name)
        if existing is not None:
            return existing
        menu = tk.Menu(self.menubar, tearoff=False)
        rank = _MENU_ORDER.index(name) if name in _MENU_ORDER else len(_MENU_ORDER)
        position = sum(
            1
            for other in self._menus
            if (_MENU_ORDER.index(other) if other in _MENU_ORDER else len(_MENU_ORDER)) <= rank
        )
        style_menu(menu, self.palette)
        self.menubar.insert_cascade(position, label=name, menu=menu)
        self._menus[name] = menu
        self.root.configure(menu=self.menubar)
        return menu

    def add_menu_command(self, menu: str, label: str, command: Callable[[], None]) -> None:
        """Add an entry to the menubar, creating the menu if needed. Entries keep the order they are added in."""
        self.menu(menu).add_command(label=label, command=command)

    def _build_view_menu(self) -> None:
        themes = tk.Menu(self.menu("View"), tearoff=False)
        self._theme_var = tk.StringVar(value=self.settings.theme.value)
        for theme in (Theme.LIGHT, Theme.DARK, Theme.SYSTEM):
            themes.add_radiobutton(
                label=theme.value.capitalize(),
                value=theme.value,
                variable=self._theme_var,
                command=partial(self.set_theme, theme),
            )
        self._theme_menu = themes
        style_menu(themes, self.palette)
        self._compact_var = tk.BooleanVar(value=self.settings.compact_mode)
        self.menu("View").add_checkbutton(
            label="Compact mode",
            variable=self._compact_var,
            command=lambda: self.set_compact(compact=self._compact_var.get()),
        )
        self.menu("View").add_cascade(label="Theme", menu=themes)

    def set_compact(self, *, compact: bool) -> None:
        """Show only the readout, Function buttons, Run/Pause and the Range, Resolution and Integration Time controls.

        Everything else (the setup line, Raw Reading, the tabs) comes back when `compact` is False. The choice is
        remembered.
        """
        self.settings.compact_mode = compact
        self._compact_var.set(compact)
        self.settings.save()
        self._layout()
        self.root.geometry("")  # let the window shrink or grow to fit what is shown

    def _layout(self) -> None:
        """Show the parts of the window that belong to the current mode, each in its usual place."""
        compact = self.settings.compact_mode
        for widget, _options, _in_compact in self._control_items:
            widget.pack_forget()
        for widget, options, in_compact in self._control_items:
            if in_compact or not compact:
                widget.pack(**options)
        self.setup_label.pack_forget()
        if not compact:
            self.setup_label.pack(before=self.readout, fill="x")
        self.notebook.pack_forget()
        if self._has_tabs and not compact:
            self.notebook.pack(after=self._controls, fill="both", expand=True, padx=8, pady=(0, 8))

    def set_theme(self, theme: Theme) -> None:
        """Switch to `theme` now, remember the choice, and tell the `on_theme_changed` callbacks."""
        self.palette = apply_theme(self.root, theme)
        self.settings.theme = theme
        self._theme_var.set(theme.value)
        self.settings.save()
        for menu in (*self._menus.values(), self._theme_menu, self.menubar):
            style_menu(menu, self.palette)
        for callback in self._theme_callbacks:
            callback(self.palette)

    def on_theme_changed(self, callback: Callable[[Palette], None]) -> None:
        """Call `callback` with the current Palette now and again whenever the theme changes.

        ttk widgets follow the theme through their styles; use this for anything that does not, such as a chart.
        """
        self._theme_callbacks.append(callback)
        callback(self.palette)

    def _build_shortcuts(self) -> None:
        self.shortcuts = Shortcuts(self.root)
        for index, function in enumerate(Function, start=1):
            self.register_shortcut(f"F{index}", f"Select {function.label}", partial(self._on_function, function))
        self.register_shortcut("R", "Run or pause Continuous Readings", self._shortcut_run)
        self.register_shortcut("Space", "Take a single Reading", self.trigger_tab.single)
        for sequence, description in _PLANNED_SHORTCUTS:
            self.shortcuts.plan(sequence, description)
        self.register_shortcut("Ctrl+K", "Open the SCPI console", partial(self.show_tab, CONSOLE_TITLE))
        self.register_shortcut("Ctrl+L", "Start or stop recording", self.recording.toggle)
        self.add_menu_command("Help", "Shortcuts", self.show_shortcuts)
        self.shortcuts_dialog: tk.Toplevel | None = None

    def register_shortcut(self, sequence: str, description: str, handler: Callable[[], None]) -> None:
        """Bind a key (written like "F5", "Space", "R", "Ctrl+L") to `handler` and list it in Help → Shortcuts.

        A planned shortcut (Space, Ctrl+L, Ctrl+K, Ctrl+,) becomes available when its feature registers it. Keys
        without Ctrl or Alt do nothing while a text field has focus.
        """
        self.shortcuts.register(sequence, description, handler)

    def show_shortcuts(self) -> None:
        """Open (or raise) the window listing every shortcut, marking the ones not available yet."""
        if self.shortcuts_dialog is not None and self.shortcuts_dialog.winfo_exists():
            self.shortcuts_dialog.lift()
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Shortcuts")
        dialog.transient(self.root)
        table = ttk.Treeview(dialog, columns=("shortcut", "action", "status"), show="headings", height=16)
        for column, title, width in (("shortcut", "Shortcut", 90), ("action", "Action", 280), ("status", "", 110)):
            table.heading(column, text=title)
            table.column(column, width=width, anchor="w")
        for shortcut in self.shortcuts.entries():
            status = "" if shortcut.available else "planned, not available yet"
            table.insert("", "end", values=(shortcut.sequence, shortcut.description, status))
        table.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(0, 8))
        self.shortcuts_table = table
        self.shortcuts_dialog = dialog

    def _shortcut_run(self) -> None:
        if str(self.run_button.cget("state")) != "disabled":
            self._toggle_run()

    def add_tab(self, title: str, tab: tk.Widget, *, focus: tk.Widget | None = None) -> None:
        """Add a tab to the strip under the controls; `tab` must be a child of `self.notebook`.

        `focus` is the widget that takes the keyboard focus when `show_tab` brings the tab forward.
        """
        self._has_tabs = True
        self.notebook.add(tab, text=title)
        self._tabs[title] = (tab, focus)
        self._layout()

    def show_tab(self, title: str) -> None:
        """Bring the tab called `title` to the front and give its main widget the keyboard focus.

        Raises `KeyError` if there is no such tab. A keyboard shortcut or menu entry calls this, e.g.
        `window.show_tab("SCPI console")`.
        """
        tab, focus = self._tabs[title]
        self.notebook.select(tab)  # type: ignore[no-untyped-call]  # ttk.Notebook.select is untyped in typeshed
        (focus or tab).focus_set()

    def _build_status_bar(self) -> None:
        status = ttk.Frame(self.root, relief="sunken", padding=(6, 2))
        status.pack(fill="x", side="bottom")
        self._status_bar = status
        self.status_connection = ttk.Label(status, text="Not connected")
        self.status_identity = ttk.Label(status, text="")
        self.status_terminals = ttk.Label(status, text="")
        self.status_message = ttk.Label(status, text="")
        self.status_error = ttk.Label(status, text="", style=ERROR_STYLE)
        self.status_rate = ttk.Label(status, text="", anchor="e")
        self.status_connection.pack(side="left", padx=(0, 12))
        self.status_identity.pack(side="left", padx=(0, 12))
        self.status_terminals.pack(side="left", padx=(0, 12))
        self.status_rate.pack(side="right")
        self.status_error.pack(side="right", padx=(0, 12))
        self.status_message.pack(side="left", fill="x", expand=True)

    def _build_connection_menu(self) -> None:
        self.add_menu_command("File", _CONNECT, self.show_connection_dialog)
        self.add_menu_command("File", _DISCONNECT, self.disconnect)
        self._update_connection_menu()

    def _update_connection_menu(self) -> None:
        active = self._has_connection and not self._retiring
        self.menu("File").entryconfigure(_DISCONNECT, state="normal" if active else "disabled")

    def show_connection_dialog(self) -> None:
        """Ask which Meter (or the Simulator) to connect to. Only one dialog is open at a time."""
        if self.connection_dialog is not None and self.connection_dialog.is_open:
            self.connection_dialog.window.lift()
            return
        self.connection_dialog = ConnectionDialog(
            self.root,
            detect=self._detect,
            scan=self._scan,
            on_connect=self._on_dialog_connect,
            initial=self.settings.last_connection,
            auto_reconnect=self.settings.auto_reconnect,
            probe=self._probe,
            probe_allowed=self._can_probe,
        )

    def _can_probe(self) -> bool:
        """Probe opens the serial port itself, so it waits until the window has no Connection (ADR-0002)."""
        return not self._has_connection

    def _on_dialog_connect(self, choice: LastConnection, auto_reconnect: bool) -> None:  # noqa: FBT001
        self.settings.auto_reconnect = auto_reconnect
        self._save_settings()
        self.connect(choice)

    def connect(self, choice: LastConnection) -> None:
        """Connect to the Meter (or Simulator) `choice` names, first finishing with any Connection already open."""
        self._connect(_Connection(self._make_opener(choice), describe_connection(choice), choice))

    def disconnect(self) -> None:
        """Return the Meter to Local and close the Connection, without waiting for a Reading in progress."""
        if not self._has_connection or self._retiring:
            return
        self._next = None
        self._retire()

    def _connect(self, connection: _Connection) -> None:
        self._next = connection
        if self._has_connection:
            self._retire()
        else:
            self._launch()

    def _retire(self) -> None:
        """Ask the current Worker to give the Meter back to its front panel and close; `Disconnected` says when."""
        if self._retiring:
            return
        self._retiring = True
        self._end("Disconnecting…")
        self._worker.disconnect()

    def _new_worker(self) -> Worker:
        return Worker(None, self._events, error_check_interval_s=self.settings.error_check_interval_s)

    def _launch(self) -> None:
        """Start a Worker for the Connection that is waiting, with the window cleared of the previous one."""
        connection, self._next = self._next, None
        if connection is None:
            return
        self._resource = connection.resource
        self._choice = connection.choice
        self._connected = False
        self._ended = False
        self._busy = False
        self._running = False
        self._setup = None
        self._last = None
        self._rate.reset()
        self._clear_display()
        self.status_connection.configure(text="Connecting…")
        self._has_connection = True
        if not self._worker.is_alive():  # its thread ended after reporting `WorkerFailed`, so serve this one afresh
            self._worker = self._new_worker()
            self._worker.start()
        self._worker.connect(connection.open_transport)
        self._update_connection_menu()

    def _clear_display(self) -> None:
        self.readout.configure(text=NO_READING, fg=_VFD_FOREGROUND)
        self.function_label.configure(text=Function.DC_VOLTAGE.label)
        self.setup_label.configure(text="")
        self._function_var.set("")
        self.run_button.configure(text="Run", state="disabled")
        for button in self.function_buttons.values():
            button.configure(state="disabled")
        for box in (self.range_box, self.resolution_box, self.nplc_box):
            self._fill(box, [], "", applicable=False)
        for label in (self.status_identity, self.status_message, self.status_error, self.status_rate):
            label.configure(text="")

    def _save_settings(self) -> None:
        try:
            self.settings.save()
        except OSError as error:
            _LOG.warning("Could not save the settings: %s", error)
            self.status_error.configure(text=f"Could not save the settings: {error}")

    def close(self) -> None:
        """Close the dialog, return the Meter to Local and destroy the window. Safe to call more than once."""
        if self._closed:
            return
        if self.connection_dialog is not None:
            self.connection_dialog.close()
            self.connection_dialog = None
        self.stop()
        self._release_tk_variables()
        self.root.destroy()

    def _release_tk_variables(self) -> None:
        """Free the window's Tk variables here, on the Tk thread, while their interpreter is still alive.

        Left to the garbage collector they are freed on whichever thread it happens to run on (a Worker's, say), and
        Tkinter raises when a variable is freed anywhere but the Tk thread.
        """
        for name, value in list(vars(self).items()):
            if isinstance(value, tk.Variable):
                delattr(self, name)

    def worker_is_alive(self) -> bool:
        return self._worker.is_alive()

    def stop(self) -> None:
        """Shut the Worker down and stop polling it, leaving the window itself alone."""
        self._closed = True
        for callback in self._close_callbacks:
            callback()
        self._close_callbacks.clear()
        # Handlers are usually bound methods of this window; dropping them frees the window (and its Tk variables) as
        # soon as the last reference goes, in this thread, rather than whenever the cycle collector runs in another.
        self.shortcuts.clear()
        self._theme_callbacks.clear()
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

    def pause_continuous(self) -> None:
        """Pause Continuous Readings if they are running (Single and a Burst do, so that nothing else uses the Meter)."""
        if self._running:
            self._set_running(running=False)

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

    @property
    def current_setup(self) -> Setup | None:
        """The Setup the Meter last reported, or None before it has reported one."""
        return self._setup

    def request_setup(self, setup: Setup) -> bool:
        """Ask the Worker to put the Meter in `setup`, locking the controls until it answers.

        Returns False, having sent nothing, when there is no Connection or the Meter is busy with another change.
        """
        if self._setup is None or self._busy or self._ended or not self._connected:
            return False
        self._begin_change()
        self._worker.apply_setup(setup)
        return True

    def _on_function(self, function: Function) -> None:
        if self._setup is None or self._busy or self._ended:
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
        self._ticks += 1
        if self._ticks % _GC_EVERY_TICKS == 0 and not gc.isenabled():
            gc.collect()
        self._poll_id = self.root.after(_POLL_MS, self._drain)

    def _handle(self, event: Event) -> None:
        if not self._retiring or isinstance(event, Disconnected):
            self._handle_in_window(event)  # a Connection being closed has nothing more to tell the window
        for handler in self._event_handlers:
            handler(event)

    def _handle_in_window(self, event: Event) -> None:
        match event:
            case ReadingTaken(timestamp=timestamp):
                self._last = event
                for listener in self._reading_listeners:
                    listener(event)
                self._render_readout()
                self.status_message.configure(text="")
                if self._running:
                    self._rate.add(timestamp)
                    rate = self._rate.per_second()
                    self.status_rate.configure(text="" if rate is None else f"{rate:.1f} Readings/s")
            case ReadingFailed(message):
                self.status_message.configure(text=f"Reading lost: {message}")
            case TerminalsChanged(terminals):
                self._show_terminals(terminals)
            case _:
                self._handle_connection_event(event)

    def _handle_connection_event(self, event: Event) -> None:
        """Handle what changes the Connection or the Setup, as opposed to a Reading."""
        match event:
            case Connected(identity, setup, terminals):
                self.status_connection.configure(text=f"Connected · {self._resource}")
                self.status_identity.configure(
                    text=f"{identity.manufacturer} {identity.model} · firmware {identity.firmware}"
                )
                self._show_terminals(terminals)
                self._connected = True
                self._remember_connection()
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
                self._end(f"Connection failed: {message}", hint=_RECONNECT_HINT)
            case ConnectionLost(message):
                self._end(f"Connection lost: {message}", hint=_RECONNECT_HINT)
            case WorkerFailed(message):
                self._end(f"Failed: {message}", hint=_RECONNECT_HINT)
            case Disconnected():
                self._on_disconnected()

    def _on_disconnected(self) -> None:
        """Move on once the Worker has returned the Meter to Local and closed the Connection: to the next, or to rest."""
        self._has_connection = False
        if self._retiring:
            self._retiring = False
            if self._next is not None:
                self._launch()
            else:
                self.status_connection.configure(text="Disconnected")
                self.status_identity.configure(text="")
        elif not self._ended:
            self._end("Disconnected", hint=_RECONNECT_HINT)
        self._update_connection_menu()

    def _remember_connection(self) -> None:
        """Keep the Connection that just worked as the one to offer, and to reconnect to, next time."""
        if self._choice is not None:
            self.settings.last_connection = self._choice
            self._save_settings()

    def _show_terminals(self, terminals: Terminals) -> None:
        self.status_terminals.configure(text=f"Terminals: {terminals.label}")

    def _render_readout(self) -> None:
        taken = self._last
        limit = None if taken is None else taken.reading.limit
        failed = limit is not None and limit.failed
        if taken is None:
            text = NO_READING
        elif self._raw.get():
            text = taken.reading.raw.strip()
        else:
            text = format_reading(taken.reading, taken.setup.resolution)
        if failed and limit is not None:
            text = f"{text}  {limit.label}"
        self.readout.configure(text=text, fg=FAIL_COLOUR if failed else _VFD_FOREGROUND)

    def _show_setup(self, setup: Setup) -> None:
        """Show the Setup the Meter reported on the readout and in the controls."""
        if self._setup is not None and (self._setup.function is not setup.function or self._setup.math != setup.math):
            self._last = None  # a Reading of the old Function or Math Operation means nothing under the new one
            self._render_readout()
        self._setup = setup
        self._function_var.set(setup.function.value)
        operation = setup.math.operation
        self.function_label.configure(
            text=setup.function.label if operation is None else f"{setup.function.label} · {operation.label}"
        )
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
        self.sense_tab.show(setup, enabled=enabled)
        self.math_tab.show(setup, enabled=enabled)

    @staticmethod
    def _fill(box: ttk.Combobox, values: list[str], shown: str, *, applicable: bool) -> None:
        box.configure(values=values, state="readonly" if applicable else "disabled")
        box.set(shown)

    def _end(self, state: str, *, hint: str = "") -> None:
        self._ended = True
        self.status_connection.configure(text=state)
        if hint:
            self.status_message.configure(text=hint)
        self._update_connection_menu()
        self.run_button.configure(state="disabled")
        self._set_running(running=False)
        if self._setup is not None:
            self._sync_controls(self._setup)
        else:
            for button in self.function_buttons.values():
                button.configure(state="disabled")
