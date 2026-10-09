"""The SCPI console tab: type a raw command or query, read the Meter's reply, and recall earlier commands.

The tab never touches the Meter. It hands each command to `send`, which goes to the Worker (ADR-0002), and shows
what comes back through `handle_event`. Whether a command is a calibration write is decided by the Worker
(ADR-0006); the tab only passes on whether the override is ticked, and unticks it again after every command.
"""

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from agilent34401a.worker import (
    Connected,
    ConnectionFailed,
    Disconnected,
    Event,
    RawFailed,
    RawRefused,
    RawReplied,
    WorkerFailed,
)

TITLE = "SCPI console"
_HISTORY_LENGTH = 200
_ERROR_COLOUR = "#b00020"
_MUTED_COLOUR = "#666666"
_OVERRIDE_TEXT = "Allow calibration commands (this can invalidate the Meter's calibration)"


class ConsoleTab:
    """Raw SCPI to the Meter. `send(command, allow_calibration)` must pass the command to the Worker.

    This owns its `frame` rather than being one: a widget that keeps its children as attributes is a reference
    cycle, and a cycle would leave its Tk variables to be freed by whichever thread the garbage collector runs on.
    """

    def __init__(self, parent: tk.Widget, send: Callable[[str, bool], None]) -> None:
        self.frame = ttk.Frame(parent, padding=8)
        self._send = send
        self._history: list[str] = []
        self._position = 0  # where Up and Down are in the history; len(history) means a new command
        self._draft = ""  # what was being typed before Up went into the history

        self.output = tk.Text(self.frame, height=10, wrap="word", state="disabled", takefocus=False)
        self.output.tag_configure("command", font="TkFixedFont", foreground=_MUTED_COLOUR)
        self.output.tag_configure("reply", font="TkFixedFont")
        self.output.tag_configure("note", foreground=_MUTED_COLOUR)
        self.output.tag_configure("error", foreground=_ERROR_COLOUR)
        scrollbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=scrollbar.set)

        entry_row = ttk.Frame(self.frame)
        ttk.Label(entry_row, text="Command").pack(side="left", padx=(0, 4))
        self.entry = ttk.Entry(entry_row, font="TkFixedFont", state="disabled")
        self.entry.pack(side="left", fill="x", expand=True)
        self.send_button = ttk.Button(entry_row, text="Send", command=self.submit, state="disabled")
        self.send_button.pack(side="left", padx=(4, 0))
        self.clear_button = ttk.Button(entry_row, text="Clear", command=self.clear)
        self.clear_button.pack(side="left", padx=(4, 0))

        self.allow_calibration = tk.BooleanVar(value=False)
        self.allow_calibration_check = ttk.Checkbutton(self.frame, text=_OVERRIDE_TEXT, variable=self.allow_calibration)

        entry_row.pack(side="bottom", fill="x", pady=(6, 0))
        self.allow_calibration_check.pack(side="bottom", anchor="w", pady=(6, 0))
        scrollbar.pack(side="right", fill="y")
        self.output.pack(side="left", fill="both", expand=True)

        for sequence in ("<Return>", "<KP_Enter>"):
            self.entry.bind(sequence, self._on_enter)
        # Tk's Ctrl+K in an entry deletes to the end of the line; here it means "go to the console", where we are.
        self.entry.bind("<Control-k>", lambda _event: "break")
        self.entry.bind("<Control-K>", lambda _event: "break")
        self.entry.bind("<Up>", self._on_up)
        self.entry.bind("<Down>", self._on_down)

    @property
    def history(self) -> tuple[str, ...]:
        """The commands sent so far, oldest first."""
        return tuple(self._history)

    @property
    def text(self) -> str:
        """Everything shown in the console."""
        return str(self.output.get("1.0", "end-1c"))

    def submit(self) -> None:
        """Send what is in the entry, if there is anything and the Connection is up, and keep it in the history."""
        command = self.entry.get()
        if not command.strip() or str(self.send_button.cget("state")) == "disabled":
            return
        if not self._history or self._history[-1] != command:
            self._history.append(command)
            del self._history[:-_HISTORY_LENGTH]
        self._position = len(self._history)
        self._draft = ""
        self.entry.delete(0, "end")
        self._show(f"> {command}", "command")
        allowed = bool(self.allow_calibration.get())
        self.allow_calibration.set(value=False)  # the override is for this command only
        self._send(command, allowed)

    def clear(self) -> None:
        """Empty the output; the history stays."""
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def previous_command(self) -> None:
        """Put the command before the one shown into the entry (Up)."""
        if self._position == 0:
            return
        if self._position == len(self._history):
            self._draft = self.entry.get()
        self._position -= 1
        self._set_entry(self._history[self._position])

    def next_command(self) -> None:
        """Put the command after the one shown into the entry, or what was being typed before Up (Down)."""
        if self._position >= len(self._history):
            return
        self._position += 1
        self._set_entry(self._history[self._position] if self._position < len(self._history) else self._draft)

    def handle_event(self, event: Event) -> None:
        """Show what the Worker reports about a command, and follow the Connection's state."""
        match event:
            case Connected():
                self._set_enabled(enabled=True)
            case ConnectionFailed() | WorkerFailed() | Disconnected():
                self._set_enabled(enabled=False)
            case RawReplied(_command, reply, errors):
                if reply is not None:
                    self._show(reply, "reply")
                elif not errors:
                    self._show("(sent)", "note")
                for queued in errors:
                    self._show(f"Meter error {queued.code}: {queued.message}", "error")
            case RawRefused(_command, message):
                self._show(f"Refused: {message}. Tick the override above to send it anyway.", "error")
            case RawFailed(_command, message, errors):
                self._show(f"Failed: {message}", "error")
                for queued in errors:
                    self._show(f"Meter error {queued.code}: {queued.message}", "error")
            case _:
                pass

    def _on_enter(self, _event: tk.Event) -> None:
        self.submit()

    def _on_up(self, _event: tk.Event) -> str:
        self.previous_command()
        return "break"

    def _on_down(self, _event: tk.Event) -> str:
        self.next_command()
        return "break"

    def _set_entry(self, text: str) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self.entry.icursor("end")

    def _set_enabled(self, *, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
        self.send_button.configure(state=state)

    def _show(self, line: str, tag: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", f"{line}\n", tag)
        self.output.configure(state="disabled")
        self.output.see("end")
