"""The System tab: the Meter's identity, Reset and self-test, beeper and display, calibration, Lockout and error log."""

import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from tkinter import messagebox, ttk

from agilent34401a.driver import DISPLAY_TEXT_LIMIT, Identity, QueuedError, SystemInfo
from agilent34401a.error_log import ErrorLog
from agilent34401a.worker import (
    AdminFailed,
    Connected,
    ConnectionFailed,
    Disconnected,
    ErrorsReported,
    Event,
    LockoutChanged,
    ResetDone,
    SelfTestFinished,
    SystemRead,
    Worker,
    WorkerFailed,
)

NOT_KNOWN = "—"
RESET_QUESTION = (
    "Reset the Meter to its power-on state?\n\n"
    "Its Function, Range, Resolution, Integration Time and trigger settings all go back to their defaults. "
    "The application never does this on its own."
)
SELF_TEST_RUNNING = "Running self-test, about 10 s…"

Confirm = Callable[[str, str], bool]


def ask_yes_no(title: str, message: str) -> bool:
    return bool(messagebox.askyesno(title, message, icon="warning", default="no"))


class SystemTab(ttk.Frame):
    """Administers the Meter through the Worker. Feed it every event with `handle`; it never touches the Transport."""

    def __init__(
        self,
        parent: tk.Misc,
        worker: Worker,
        *,
        confirm: Confirm = ask_yes_no,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        super().__init__(parent, padding=8)
        self._worker = worker
        self.confirm = confirm
        self._now = now
        self._log = ErrorLog()
        self._info: SystemInfo | None = None
        self._connected = False
        self._self_testing = False
        self._controls: list[ttk.Button | ttk.Checkbutton | ttk.Entry] = []
        self._build_identity()
        self._build_maintenance()
        self._build_beeper_and_display()
        self._build_calibration()
        self._build_error_log()
        self._sync_controls()

    # -- building ------------------------------------------------------------------------------------------------

    def _group(self, title: str, column: int, row: int) -> ttk.LabelFrame:
        group = ttk.LabelFrame(self, text=title, padding=8)
        group.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)
        return group

    @staticmethod
    def _value_row(group: ttk.LabelFrame, row: int, title: str) -> ttk.Label:
        ttk.Label(group, text=title).grid(row=row, column=0, sticky="w", padx=(0, 8))
        value = ttk.Label(group, text=NOT_KNOWN)
        value.grid(row=row, column=1, sticky="w")
        return value

    def _build_identity(self) -> None:
        group = self._group("Meter", column=0, row=0)
        self.manufacturer_label = self._value_row(group, 0, "Manufacturer")
        self.model_label = self._value_row(group, 1, "Model")
        self.serial_label = self._value_row(group, 2, "Serial number")
        self.firmware_label = self._value_row(group, 3, "Firmware revision")
        self.scpi_label = self._value_row(group, 4, "SCPI version")

    def _build_maintenance(self) -> None:
        group = self._group("Reset and self-test", column=1, row=0)
        self.reset_button = ttk.Button(group, text="Reset…", command=self._on_reset)
        self.self_test_button = ttk.Button(group, text="Self-test", command=self._on_self_test)
        self.reset_button.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=2)
        self.self_test_button.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=2)
        self.reset_result = ttk.Label(group, text="")
        self.self_test_result = ttk.Label(group, text="")
        self.reset_result.grid(row=0, column=1, sticky="w")
        self.self_test_result.grid(row=1, column=1, sticky="w")
        self.lockout_check = ttk.Checkbutton(
            group, text="Lock out the front panel's Local key", command=self._on_lockout
        )
        self.lockout_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._controls.extend([self.reset_button, self.self_test_button, self.lockout_check])

    def _build_beeper_and_display(self) -> None:
        group = self._group("Beeper and display", column=0, row=1)
        self.beeper_check = ttk.Checkbutton(group, text="Beeper on", command=self._on_beeper)
        self.beep_button = ttk.Button(group, text="Test beep", command=self._worker.beep)
        self.beeper_check.grid(row=0, column=0, sticky="w")
        self.beep_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.display_check = ttk.Checkbutton(group, text="Display on", command=self._on_display)
        self.display_check.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(group, text=f"Message (up to {DISPLAY_TEXT_LIMIT} characters)").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        self.message_entry = ttk.Entry(
            group,
            width=DISPLAY_TEXT_LIMIT + 2,
            validate="key",
            validatecommand=(self.register(_fits_the_display), "%P"),
        )
        self.show_message_button = ttk.Button(group, text="Show", command=self._on_show_message)
        self.clear_message_button = ttk.Button(group, text="Clear", command=self._on_clear_message)
        self.message_entry.grid(row=3, column=0, sticky="ew")
        self.show_message_button.grid(row=3, column=1, padx=(8, 0))
        self.clear_message_button.grid(row=3, column=2, padx=(4, 0))
        self.message_entry.bind("<Return>", lambda _event: self._on_show_message())
        self._controls.extend(
            [
                self.beeper_check,
                self.beep_button,
                self.display_check,
                self.message_entry,
                self.show_message_button,
                self.clear_message_button,
            ]
        )

    def _build_calibration(self) -> None:
        group = self._group("Calibration (read-only)", column=1, row=1)
        self.calibration_count_label = self._value_row(group, 0, "Times calibrated")
        self.calibration_message_label = self._value_row(group, 1, "Calibration message")

    def _build_error_log(self) -> None:
        group = ttk.LabelFrame(self, text="Meter errors", padding=8)
        group.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        self.error_tree = ttk.Treeview(group, columns=("time", "code", "message"), show="headings", height=6)
        for column, title, width in (("time", "Time", 150), ("code", "Code", 60), ("message", "Message", 300)):
            self.error_tree.heading(column, text=title)
            self.error_tree.column(column, width=width, anchor="w", stretch=column == "message")
        scroll = ttk.Scrollbar(group, orient="vertical", command=self.error_tree.yview)
        self.error_tree.configure(yscrollcommand=scroll.set)
        self.error_tree.grid(row=0, column=0, columnspan=4, sticky="nsew")
        scroll.grid(row=0, column=4, sticky="ns")
        self.check_errors_button = ttk.Button(group, text="Check now", command=self._worker.check_errors)
        self.copy_log_button = ttk.Button(group, text="Copy", command=self._on_copy_log)
        self.clear_log_button = ttk.Button(group, text="Clear log", command=self._on_clear_log)
        self.check_errors_button.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.copy_log_button.grid(row=1, column=1, sticky="w", padx=4, pady=(6, 0))
        self.clear_log_button.grid(row=1, column=2, sticky="w", pady=(6, 0))
        group.columnconfigure(2, weight=1)
        group.rowconfigure(0, weight=1)
        self._controls.append(self.check_errors_button)
        self.problem_label = ttk.Label(self, text="", foreground="#b00020")
        self.problem_label.grid(row=3, column=0, columnspan=2, sticky="w", padx=4)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

    # -- what the user does --------------------------------------------------------------------------------------

    def _on_reset(self) -> None:
        if not self.confirm("Reset the Meter", RESET_QUESTION):
            return
        self.reset_result.configure(text="Resetting…")
        self._worker.reset_meter()

    def _on_self_test(self) -> None:
        self._self_testing = True
        self.self_test_result.configure(text=SELF_TEST_RUNNING)
        self.problem_label.configure(text="")
        self._sync_controls()
        self._worker.run_self_test()

    def _on_beeper(self) -> None:
        self._worker.set_beeper(enabled=is_checked(self.beeper_check))

    def _on_display(self) -> None:
        self._worker.set_display(on=is_checked(self.display_check))

    def _on_show_message(self) -> None:
        self._worker.show_display_text(self.message_entry.get() or None)

    def _on_clear_message(self) -> None:
        self.message_entry.delete(0, "end")
        self._worker.show_display_text(None)

    def _on_lockout(self) -> None:
        self._worker.set_lockout(locked=is_checked(self.lockout_check))

    def _on_clear_log(self) -> None:
        self._log.clear()
        self.error_tree.delete(*self.error_tree.get_children())

    def _on_copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._log.text())

    # -- what the Worker reports ---------------------------------------------------------------------------------

    def handle(self, event: Event) -> None:
        """React to one event from the Worker."""
        match event:
            case Connected(identity=identity):
                self._connected = True
                self._show_identity(identity)
                self._sync_controls()
                self._worker.read_system()
            case SystemRead() | ResetDone() | SelfTestFinished() | LockoutChanged() | AdminFailed():
                self._handle_result(event)
            case ErrorsReported(errors):
                self._add_to_log(errors)
            case ConnectionFailed() | WorkerFailed() | Disconnected():
                self._connected = False
                self._self_testing = False
                self._sync_controls()
            case _:
                pass

    def _handle_result(self, event: Event) -> None:
        """Show how a System request turned out."""
        match event:
            case SystemRead(info):
                self._info = info
                self.problem_label.configure(text="")
                self._show_info(info)
            case ResetDone():
                self.reset_result.configure(text="Meter reset")
                self.self_test_result.configure(text="")
            case SelfTestFinished(passed):
                self._self_testing = False
                self.self_test_result.configure(text="Self-test passed" if passed else "Self-test FAILED")
                self._sync_controls()
            case LockoutChanged(locked):
                set_checked(self.lockout_check, checked=locked)
            case AdminFailed(message):
                self._self_testing = False
                self.reset_result.configure(text="")
                if self.self_test_result.cget("text") == SELF_TEST_RUNNING:
                    self.self_test_result.configure(text="")
                if self._info is not None:
                    self._show_info(self._info)  # take the checkbuttons back to what the Meter last said
                self.problem_label.configure(text=message)
                self._sync_controls()
            case _:
                pass

    def _show_identity(self, identity: Identity) -> None:
        self.manufacturer_label.configure(text=identity.manufacturer)
        self.model_label.configure(text=identity.model)
        self.serial_label.configure(text=identity.serial)
        self.firmware_label.configure(text=identity.firmware)

    def _show_info(self, info: SystemInfo) -> None:
        self.scpi_label.configure(text=info.scpi_version)
        self.calibration_count_label.configure(text=str(info.calibration_count))
        self.calibration_message_label.configure(text=info.calibration_message or NOT_KNOWN)
        set_checked(self.beeper_check, checked=info.beeper_enabled)
        set_checked(self.display_check, checked=info.display_on)

    def _add_to_log(self, errors: tuple[QueuedError, ...]) -> None:
        for entry in self._log.add(errors, self._now()):
            row = self.error_tree.insert(
                "", "end", values=(f"{entry.timestamp:%Y-%m-%d %H:%M:%S}", entry.code, entry.message)
            )
            self.error_tree.see(row)

    def _sync_controls(self) -> None:
        """Enable the controls while connected, except Reset and self-test during a self-test."""
        for control in self._controls:
            busy = self._self_testing and control in (self.reset_button, self.self_test_button)
            control.configure(state="normal" if self._connected and not busy else "disabled")


def _fits_the_display(proposed: str) -> bool:
    return len(proposed) <= DISPLAY_TEXT_LIMIT


def is_checked(button: ttk.Checkbutton) -> bool:
    """Whether a check button is ticked. The tab keeps this in the widget, not in Tk variables (see `set_checked`)."""
    return bool(button.instate(["selected"]))


def set_checked(button: ttk.Checkbutton, *, checked: bool) -> None:
    """Tick or untick a check button without running its command.

    A Tk variable object that is garbage collected on the Worker's thread makes Tkinter raise there, so the tab
    uses no variables.
    """
    button.state(["selected" if checked else "!selected"])
