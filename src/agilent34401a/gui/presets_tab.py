"""The Presets tab: save the current Setup under a name, apply it again, rename, delete, export and import.

Below the list is a separate Meter Memory section: the Meter's own few numbered, unnamed Setups, which it stores and
recalls itself (`*SAV` and `*RCL`) and which only an explicit, confirmed click ever touches (ADR-0004).

Applying a Preset reports how it went in the panel under the list: which settings the Meter did not take, and what
its error queue said. The tab never touches the Meter; it hands a Setup to `request_setup`, which goes through the
Worker (ADR-0002), and learns the outcome from the Worker's events. It never holds the window (ADR-0008).

Every dialog is a replaceable attribute (`ask_name`, `confirm`, `choose_export_file`, `choose_import_file`,
`choose_collision`), which is how tests answer them.
"""

import tkinter as tk
import weakref
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import TYPE_CHECKING

from agilent34401a.driver import RECALL_LOCATIONS, STORE_LOCATIONS
from agilent34401a.errors import PresetError
from agilent34401a.gui.themes import ERROR_STYLE
from agilent34401a.gui.tooltip import Tooltip
from agilent34401a.meter import Setup, describe_setup
from agilent34401a.preset_apply import ApplyReport, PresetApplier
from agilent34401a.preset_store import Collision, ImportReport, PresetStore
from agilent34401a.worker import (
    AdminFailed,
    Connected,
    ConnectionFailed,
    ConnectionLost,
    Disconnected,
    ErrorsReported,
    Event,
    MeterMemoryStored,
    SetupChanged,
    SetupFailed,
    TerminalsChanged,
    WorkerFailed,
)

if TYPE_CHECKING:
    from agilent34401a.gui.main_window import MainWindow

TITLE = "Presets"
PRESETS_FILE_NAME = "34401A-presets.json"
_NO_SETUP = "Connect to a Meter first, so that there is a Setup to save."
_NOT_CONNECTED = "Connect to a Meter first."
_NOTHING_SELECTED = "Select a Preset in the list."
_BUSY = "The Meter is busy with another change. Try again in a moment."
_JSON_FILES = [("Preset files", "*.json"), ("All files", "*")]
_NORMAL_STYLE = "TLabel"
_LIST_ROWS = 8
_MEMORY_NOTE = (
    "The Meter's own memory holds a few numbered, unnamed Setups, written and read back only over the remote "
    "interface (its front panel cannot). Unlike Presets they are not named, not kept on this computer, and cannot "
    "be exported."
)
_NOT_STORABLE = (
    "Location 0 is the Meter's power-down state, which only the Meter writes. Choose location 1 to 3 to store."
)
_MEMORY_PENDING = "The Meter is busy with the last Meter Memory request."


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def describe_import(report: ImportReport) -> str:
    """Say what an import did, and list each Preset in the file that could not be used."""
    count = len(report.added) + len(report.replaced)
    extras = []
    if report.renamed:
        extras.append(f"renamed {len(report.renamed)}")
    if report.replaced:
        extras.append(f"replaced {len(report.replaced)}")
    if report.skipped:
        extras.append(f"skipped {len(report.skipped)}")
    text = f"Imported {_plural(count, 'Preset')}" + (f" ({', '.join(extras)})." if extras else ".")
    return "\n".join([text, *report.problems])


class PresetsTab:
    """The list of Presets and the buttons that act on it. Feed it every Worker event with `handle`.

    `current_setup` gives the Setup the Meter last reported (or None), `request_setup` sends a Setup to the Meter and
    says whether it was sent, `store_in_meter` and `recall_from_meter` do the same for a Meter Memory location, and
    `pause_continuous` stops Continuous, which a Preset with a Burst trigger needs
    because every Continuous Reading puts the trigger settings back to one Reading at a time.
    """

    def __init__(  # noqa: PLR0913 - the window's actions are what tests replace
        self,
        parent: ttk.Notebook,
        store: PresetStore,
        *,
        current_setup: Callable[[], Setup | None],
        request_setup: Callable[[Setup], bool],
        pause_continuous: Callable[[], None] = lambda: None,
        store_in_meter: Callable[[int], bool] = lambda _location: False,
        recall_from_meter: Callable[[int], bool] = lambda _location: False,
    ) -> None:
        self.frame = ttk.Frame(parent, padding=12)
        self.store = store
        self._current_setup = current_setup
        self._pause_continuous = pause_continuous
        self._connected = False
        self._store_in_meter = store_in_meter
        self._recall_from_meter = recall_from_meter
        self._memory_request: tuple[str, int] | None = None  # ("store" or "recall", location) awaiting the Meter
        self._recalled: int | None = None  # the location just recalled, whose errors (if any) follow its Setup
        self.last_report: ApplyReport | None = None
        root = parent.winfo_toplevel()
        self.ask_name: Callable[[str, str], str | None] = partial(_ask_name, root)
        self.confirm: Callable[[str, str], bool] = partial(_confirm, root)
        self.choose_export_file: Callable[[str], Path | None] = partial(_choose_export_file, root)
        self.choose_import_file: Callable[[], Path | None] = partial(_choose_import_file, root)
        self.choose_collision: Callable[[tuple[str, ...]], Collision | None] = partial(_choose_collision, root)
        self.tooltips: dict[str, Tooltip] = {}
        # The applier is held by the tab and calls back into it: weakly, so that the two are not a reference cycle.
        me = weakref.ref(self)

        def report_to_me(report: ApplyReport) -> None:
            target = me()
            if target is not None:
                target.show_report(report)

        self._applier = PresetApplier(request_setup, report_to_me)
        self._build()
        self.refresh()
        if store.problems:
            self._say("\n".join(store.problems), error=True)

    # --- building ----------------------------------------------------------------------------------------------

    def _build(self) -> None:
        frame = self.frame
        frame.columnconfigure(0, weight=1)
        listing = ttk.Frame(frame)
        listing.grid(row=0, column=0, sticky="nsew")
        listing.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            listing, columns=("name", "setup"), show="headings", selectmode="browse", height=_LIST_ROWS
        )
        self.tree.heading("name", text="Preset")
        self.tree.heading("setup", text="Setup")
        self.tree.column("name", width=160, stretch=False)
        self.tree.column("setup", width=360)
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        listing.rowconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        saving = ttk.Frame(frame)
        saving.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(saving, text="Name").pack(side="left", padx=(0, 4))
        self.name_entry = ttk.Entry(saving, width=28)
        self.name_entry.pack(side="left", padx=(0, 8))
        self.name_entry.bind("<Return>", lambda _event: self.save_current())
        self.save_button = ttk.Button(saving, text="Save current Setup", command=self.save_current)
        self.save_button.pack(side="left")
        self.tooltips["save"] = Tooltip(self.save_button)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.apply_button = self._button(buttons, "Apply", self.apply_selected)
        self.rename_button = self._button(buttons, "Rename…", self.rename_selected)
        self.delete_button = self._button(buttons, "Delete", self.delete_selected)
        self.export_button = self._button(buttons, "Export…", self.export_selected)
        self.export_all_button = self._button(buttons, "Export all…", self.export_all)
        self.import_button = self._button(buttons, "Import…", self.import_presets)
        self.tooltips["apply"] = Tooltip(self.apply_button)

        self.summary_label = ttk.Label(frame, text="", wraplength=520, justify="left")
        self.summary_label.grid(row=3, column=0, sticky="w", pady=(12, 0))
        self.details_label = ttk.Label(frame, text="", wraplength=520, justify="left", style=ERROR_STYLE)
        self.details_label.grid(row=4, column=0, sticky="w")
        self.message_label = ttk.Label(frame, text="", wraplength=520, justify="left")
        self.message_label.grid(row=5, column=0, sticky="w", pady=(8, 0))
        self._build_meter_memory()

    def _build_meter_memory(self) -> None:
        """Build the Meter Memory section: a location, Store on Meter and Recall from Meter, and what happened."""
        self.memory_frame = ttk.LabelFrame(self.frame, text="Meter Memory", padding=8)
        self.memory_frame.grid(row=6, column=0, sticky="ew", pady=(16, 0))
        self.memory_note = ttk.Label(self.memory_frame, text=_MEMORY_NOTE, wraplength=520, justify="left")
        self.memory_note.grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(self.memory_frame, text="Location").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.memory_labels = {
            location: f"{location} (power-down state)" if location == 0 else str(location)
            for location in RECALL_LOCATIONS
        }
        self.memory_box = ttk.Combobox(
            self.memory_frame, values=list(self.memory_labels.values()), state="readonly", width=20
        )
        self.memory_box.set(self.memory_labels[STORE_LOCATIONS[0]])
        self.memory_box.grid(row=1, column=1, sticky="w", padx=(8, 8), pady=(8, 0))
        self.memory_box.bind("<<ComboboxSelected>>", lambda _event: self._sync())
        self.store_button = ttk.Button(self.memory_frame, text="Store on Meter", command=self.store_selected)
        self.store_button.grid(row=1, column=2, padx=(0, 8), pady=(8, 0))
        self.recall_button = ttk.Button(self.memory_frame, text="Recall from Meter", command=self.recall_selected)
        self.recall_button.grid(row=1, column=3, pady=(8, 0))
        self.tooltips["store"] = Tooltip(self.store_button)
        self.tooltips["recall"] = Tooltip(self.recall_button)
        self.memory_message = ttk.Label(self.memory_frame, text="", wraplength=520, justify="left")
        self.memory_message.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

    @staticmethod
    def _button(parent: ttk.Frame, text: str, command: Callable[[], None]) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side="left", padx=(0, 8))
        return button

    # --- the list ----------------------------------------------------------------------------------------------

    @property
    def selected_name(self) -> str | None:
        """The name of the Preset selected in the list, or None."""
        selection = self.tree.selection()
        return str(self.tree.set(selection[0], "name")) if selection else None

    def select(self, name: str) -> None:
        """Select the Preset called `name` in the list."""
        self.tree.selection_set(self._item(name))
        self._sync()  # the selection event only comes when Tk next goes idle

    def refresh(self) -> None:
        """Show the store's Presets, keeping the selection if the Preset is still there."""
        selected = self.selected_name
        self.tree.delete(*self.tree.get_children())
        for preset in self.store.presets:
            self.tree.insert("", "end", iid=self._item(preset.name), values=(preset.name, describe_setup(preset.setup)))
        if selected is not None and selected in self.store:
            self.select(selected)
        self._sync()

    @staticmethod
    def _item(name: str) -> str:
        return name.casefold()

    def _on_select(self, _event: "tk.Event[tk.Misc]") -> None:
        self._sync()

    def _sync(self) -> None:
        """Enable the buttons that can be used now, and say in a tooltip why the others cannot."""
        selected = self.selected_name is not None
        has_setup = self._current_setup() is not None
        _enable(self.save_button, has_setup)
        self.tooltips["save"].text = "" if has_setup else _NO_SETUP
        can_apply = selected and self._connected and not self._applier.busy
        _enable(self.apply_button, can_apply)
        self.tooltips["apply"].text = (
            ""
            if can_apply
            else (
                _NOTHING_SELECTED
                if not selected
                else _NOT_CONNECTED if not self._connected else "A Preset is being applied."
            )
        )
        for button in (self.rename_button, self.delete_button, self.export_button):
            _enable(button, selected)
        _enable(self.export_all_button, bool(self.store.presets))
        self._sync_meter_memory()

    @property
    def memory_location(self) -> int:
        """The Meter Memory location chosen in the box."""
        chosen = self.memory_box.get()
        return next((location for location, label in self.memory_labels.items() if label == chosen), STORE_LOCATIONS[0])

    def _sync_meter_memory(self) -> None:
        """Enable Store and Recall when there is a Connection and nothing else is pending, and say why if not."""
        waiting = self._memory_request is not None or self._applier.busy
        usable = self._connected and not waiting
        storable = self.memory_location in STORE_LOCATIONS
        reason = _NOT_CONNECTED if not self._connected else _MEMORY_PENDING if waiting else ""
        _enable(self.store_button, usable and storable)
        self.tooltips["store"].text = reason or ("" if storable else _NOT_STORABLE)
        _enable(self.recall_button, usable)
        self.tooltips["recall"].text = reason

    # --- events ------------------------------------------------------------------------------------------------

    def handle(self, event: Event) -> None:
        """Take in a Worker event: the outcome of a Preset being applied, and whether there is a Connection."""
        self._applier.handle(event)
        self._handle_meter_memory(event)
        match event:
            case Connected():
                self._connected = True
            case ConnectionFailed() | ConnectionLost() | WorkerFailed() | Disconnected():
                self._connected = False
                self._memory_request = None
            case SetupChanged():
                pass
            case _:
                return
        self._sync()

    def _handle_meter_memory(self, event: Event) -> None:
        """Follow a store or recall: say how it went when the Meter answers."""
        request, recalled = self._memory_request, self._recalled
        if not isinstance(event, ErrorsReported | TerminalsChanged):
            self._recalled = None  # the errors that go with a recalled Setup follow it at once, or not at all
        match event:
            case MeterMemoryStored(location) if request == ("store", location):
                self._memory_request = None
                self._say_memory(f"Stored the Meter's Setup in Meter Memory location {location}.")
            case SetupChanged(setup) if request is not None and request[0] == "recall":
                self._memory_request = None
                self._recalled = request[1]
                self._say_memory(
                    f"Recalled Meter Memory location {request[1]}. The Meter is now in: {describe_setup(setup)}."
                )
            case ErrorsReported(errors) if request is not None or recalled is not None:
                self._memory_request = None
                where = request[1] if request is not None else recalled
                first = errors[0]
                text = f"Meter Memory location {where}: Meter error {first.code}: {first.message}"
                self._say_memory(text, error=True)
            case SetupFailed(message) | AdminFailed(message) if request is not None:
                self._memory_request = None
                self._say_memory(f"Meter Memory location {request[1]} was not confirmed: {message}", error=True)
            case ConnectionFailed() | ConnectionLost() | WorkerFailed() | Disconnected() if request is not None:
                self._say_memory("The Connection ended before the Meter answered.", error=True)

    def show_report(self, report: ApplyReport) -> None:
        """Show how applying a Preset went. Called as the Meter answers, and again if its errors follow."""
        self.last_report = report
        self.summary_label.configure(text=report.summary)
        self.details_label.configure(text="\n".join(report.details))
        self._sync()

    # --- actions -----------------------------------------------------------------------------------------------

    def save_current(self) -> None:
        """Save the Setup the Meter last reported under the name in the entry, asking before replacing a Preset."""
        setup = self._current_setup()
        if setup is None:
            self._say(_NO_SETUP, error=True)
            return
        name = self.name_entry.get()
        try:
            existing = self.store.get(name).name if name.strip() and name in self.store else None
            if existing is not None and not self.confirm(
                "Replace Preset", f"There is already a Preset called '{existing}'. Replace it with the current Setup?"
            ):
                return
            saved = self.store.save(name, setup, replace=existing is not None)
        except PresetError as error:
            self._say(str(error), error=True)
            return
        self.name_entry.delete(0, "end")
        self.refresh()
        self.select(saved.name)
        self._say(f"Saved Preset '{saved.name}'.")

    def apply_selected(self) -> None:
        """Send the selected Preset to the Meter. The outcome appears in the panel when the Meter has answered."""
        name = self.selected_name
        if name is None:
            return
        try:
            preset = self.store.get(name)
        except PresetError as error:
            self._say(str(error), error=True)
            return
        if not preset.setup.trigger.is_single_immediate:
            self._pause_continuous()
        self.last_report = None
        if not self._applier.apply(preset):
            self._say(_BUSY, error=True)
            return
        self._say("")
        self.summary_label.configure(text=f"Applying Preset '{preset.name}'…")
        self.details_label.configure(text="")
        self._sync()

    def store_selected(self) -> None:
        """Store the Meter's Setup in the chosen Meter Memory location, after asking: it overwrites what is there."""
        location = self.memory_location
        if location not in STORE_LOCATIONS or self._memory_request is not None:
            return
        question = (
            f"Store the Meter's current Setup in Meter Memory location {location}? "
            "This overwrites what is stored there now."
        )
        if not self.confirm("Store on Meter", question):
            return
        if not self._store_in_meter(location):
            self._say_memory(_BUSY, error=True)
            return
        self._memory_request = ("store", location)
        self._say_memory(f"Storing in Meter Memory location {location}…")
        self._sync()

    def recall_selected(self) -> None:
        """Replace the Meter's Setup with the one in the chosen Meter Memory location, after asking."""
        location = self.memory_location
        if self._memory_request is not None:
            return
        question = (
            f"Recall Meter Memory location {location}? This replaces the Setup the Meter is using now "
            "(the Meter is not reset, and nothing is saved first)."
        )
        if not self.confirm("Recall from Meter", question):
            return
        if not self._recall_from_meter(location):
            self._say_memory(_BUSY, error=True)
            return
        self._memory_request = ("recall", location)
        self._say_memory(f"Recalling Meter Memory location {location}…")
        self._sync()

    def _say_memory(self, text: str, *, error: bool = False) -> None:
        self.memory_message.configure(text=text, style=ERROR_STYLE if error else _NORMAL_STYLE)

    def rename_selected(self) -> None:
        name = self.selected_name
        if name is None:
            return
        new_name = self.ask_name("Rename Preset", name)
        if new_name is None:
            return
        try:
            self.store.rename(name, new_name)
        except PresetError as error:
            self._say(str(error), error=True)
            return
        self.refresh()
        self.select(new_name.strip())
        self._say(f"Renamed Preset '{name}' to '{new_name.strip()}'.")

    def delete_selected(self) -> None:
        name = self.selected_name
        if name is None or not self.confirm("Delete Preset", f"Delete the Preset '{name}'?"):
            return
        try:
            self.store.delete(name)
        except PresetError as error:
            self._say(str(error), error=True)
            return
        self.refresh()
        self._say(f"Deleted Preset '{name}'.")

    def export_selected(self) -> None:
        name = self.selected_name
        if name is not None:
            self._export(f"{name}.json", [name], f"Exported Preset '{name}'")

    def export_all(self) -> None:
        self._export(PRESETS_FILE_NAME, None, f"Exported {_plural(len(self.store.presets), 'Preset')}")

    def _export(self, default_name: str, names: list[str] | None, done: str) -> None:
        path = self.choose_export_file(default_name)
        if path is None:
            return
        try:
            self.store.export_file(path, names)
        except PresetError as error:
            self._say(str(error), error=True)
            return
        self._say(f"{done} to {path}.")

    def import_presets(self) -> None:
        """Add the Presets in a file, asking what to do about names already in use."""
        path = self.choose_import_file()
        if path is None:
            return
        try:
            colliding = self.store.collisions_with(path)
            collision = self.choose_collision(colliding) if colliding else Collision.RENAME
            if collision is None:
                return
            report = self.store.import_file(path, collision)
        except PresetError as error:
            self._say(str(error), error=True)
            return
        self.refresh()
        self._say(describe_import(report), error=bool(report.problems))

    def _say(self, text: str, *, error: bool = False) -> None:
        self.message_label.configure(text=text, style=ERROR_STYLE if error else _NORMAL_STYLE)


def _enable(widget: ttk.Button, enabled: bool) -> None:  # noqa: FBT001 - a flag at its only call sites, reading clearly
    widget.configure(state="normal" if enabled else "disabled")


# --- the dialogs ---------------------------------------------------------------------------------------------------


def _ask_name(root: tk.Misc, title: str, initial: str) -> str | None:
    answer = simpledialog.askstring(title, "Name", initialvalue=initial, parent=root)
    return None if answer is None else str(answer)


def _confirm(root: tk.Misc, title: str, message: str) -> bool:
    return bool(messagebox.askyesno(title, message, parent=root))


def _choose_export_file(root: tk.Misc, default_name: str) -> Path | None:
    name = filedialog.asksaveasfilename(
        parent=root,
        title="Export Presets",
        initialfile=default_name,
        defaultextension=".json",
        filetypes=_JSON_FILES,
    )
    return Path(name) if name else None


def _choose_import_file(root: tk.Misc) -> Path | None:
    name = filedialog.askopenfilename(parent=root, title="Import Presets", filetypes=_JSON_FILES)
    return Path(name) if name else None


def _choose_collision(root: tk.Misc, names: tuple[str, ...]) -> Collision | None:
    answer = messagebox.askyesnocancel(
        "Import Presets",
        f"These Presets are already here: {', '.join(names)}.\n\n"
        "Yes: replace them with the imported ones.\n"
        "No: keep both, giving the imported ones a number after their name.\n"
        "Cancel: import nothing.",
        parent=root,
    )
    if answer is None:
        return None
    return Collision.REPLACE if answer else Collision.RENAME


def install_presets(window: "MainWindow") -> PresetsTab:
    """Add the Presets tab to `window`, keeping the Presets beside its settings."""
    path = window.settings.path
    store = PresetStore.in_memory() if path is None else PresetStore.load(path.parent)
    me = weakref.ref(window)  # not the window itself: a reference back to it would make a cycle

    def current_setup() -> Setup | None:
        target = me()
        return None if target is None else target.current_setup

    def request_setup(setup: Setup) -> bool:
        target = me()
        return False if target is None else target.request_setup(setup)

    def pause_continuous() -> None:
        target = me()
        if target is not None:
            target.pause_continuous()

    def store_in_meter(location: int) -> bool:
        target = me()
        return False if target is None else target.store_in_meter(location)

    def recall_from_meter(location: int) -> bool:
        target = me()
        return False if target is None else target.recall_from_meter(location)

    tab = PresetsTab(
        window.notebook,
        store,
        current_setup=current_setup,
        request_setup=request_setup,
        pause_continuous=pause_continuous,
        store_in_meter=store_in_meter,
        recall_from_meter=recall_from_meter,
    )
    window.add_tab(TITLE, tab.frame)
    window.add_event_handler(tab.handle)
    return tab
