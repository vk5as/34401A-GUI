"""The Math tab: choose the one Math Operation in effect, set its numbers, and see the Meter's Statistics.

The Meter does one Math Operation at a time, so the tab is a single choice (Off, Null, dB, dBm, Statistics or Limit
Test), each with the settings it needs beside it. Choosing an Operation, or pressing Return in one of its fields or its
Apply button, sends the Operation and its settings to the Meter through `request`; the window shows the answer.
"""

import tkinter as tk
from collections.abc import Callable
from dataclasses import replace
from tkinter import ttk

from agilent34401a.errors import InvalidSetupError
from agilent34401a.math_operations import MathOperation, MeterStatistics, offset_that_nulls
from agilent34401a.meter import Reading, Resolution, Setup, format_reading
from agilent34401a.worker import (
    Connected,
    ConnectionFailed,
    ConnectionLost,
    Disconnected,
    Event,
    ReadingTaken,
    StatisticsRead,
)

NOT_KNOWN = "—"
HINT = (
    "Only one Math Operation is active at a time: turning one on turns the others off. "
    "Changing the Function turns the Operation off."
)
NO_MATH_NOTE = "Continuity and Diode have no Math Operations."
_OFF = "OFF"
_STATISTICS = ("minimum", "maximum", "average", "count")
_STATISTICS_TITLES = {"minimum": "Minimum", "maximum": "Maximum", "average": "Average", "count": "Count"}
_ENTRY_WIDTH = 12


def _ignore(_change: Callable[[Setup], Setup]) -> None:
    """Stand in for the window's `request` once the window is gone."""


def _number_text(value: float) -> str:
    return f"{value:.10g}"


class MathTab:
    """Controls for the Math Operations. Feed it every event with `handle` and the Setup with `show`.

    `frame` is the widget to add to the window's notebook. Like the Sense tab it is a plain object that owns the
    frame, and it lets go of its Tk variable and of the window when the frame is destroyed, so that nothing is left
    for the cyclic garbage collector to free on the Worker thread (ADR-0008).
    """

    def __init__(
        self,
        parent: ttk.Notebook,
        request: Callable[[Callable[[Setup], Setup]], None],
        reset_statistics: Callable[[], None],
    ) -> None:
        self.frame = ttk.Frame(parent, padding=12)
        self._request = request
        self._reset_statistics = reset_statistics
        self._setup: Setup | None = None
        self._latest: ReadingTaken | None = None
        self._enabled = False
        self._problem = ""
        self._note = ""
        self._choice: tk.StringVar | None = tk.StringVar(master=self.frame, value=_OFF)
        self.frame.bind("<Destroy>", self._on_destroy)
        self.hint_label = ttk.Label(self.frame, text=HINT, wraplength=520, justify="left")
        self.hint_label.pack(anchor="w", pady=(0, 8))
        self.choice_buttons: dict[MathOperation | None, ttk.Radiobutton] = {}
        self.apply_buttons: dict[MathOperation, ttk.Button] = {}
        self._entries: list[ttk.Entry] = []
        self._unit_labels: list[ttk.Label] = []
        self._build_off()
        self._build_null()
        self._build_db()
        self._build_dbm()
        self._build_statistics()
        self._build_limit_test()
        self.message_label = ttk.Label(self.frame, text="", wraplength=520, justify="left")
        self.message_label.pack(anchor="w", pady=(8, 0))

    def _on_destroy(self, event: "tk.Event[tk.Misc]") -> None:
        if event.widget is self.frame:
            self._request = _ignore
            self._reset_statistics = lambda: None
            self._choice = None  # freed here, on the Tk thread

    # --- building

    def _row(self, operation: MathOperation | None, title: str) -> ttk.Frame:
        row = ttk.Frame(self.frame)
        row.pack(fill="x", pady=3)
        value = _OFF if operation is None else operation.value
        button = ttk.Radiobutton(
            row,
            text=title,
            value=value,
            variable=self._choice or "",  # the variable is only gone once the tab is
            width=11,
            command=lambda: self._chosen(operation),
            state="disabled",
        )
        button.pack(side="left")
        self.choice_buttons[operation] = button
        return row

    def _field(self, row: ttk.Frame, operation: MathOperation, title: str, unit: str) -> ttk.Entry:
        ttk.Label(row, text=title).pack(side="left", padx=(12, 4))
        entry = ttk.Entry(row, width=_ENTRY_WIDTH, state="disabled")
        entry.pack(side="left")
        entry.bind("<Return>", lambda _event: self._apply(operation))
        self._entries.append(entry)
        label = ttk.Label(row, text=unit, width=4)
        label.pack(side="left", padx=(2, 0))
        self._unit_labels.append(label)
        return entry

    def _apply_button(self, row: ttk.Frame, operation: MathOperation) -> ttk.Button:
        button = ttk.Button(row, text="Apply", command=lambda: self._apply(operation), state="disabled")
        button.pack(side="left", padx=(8, 0))
        self.apply_buttons[operation] = button
        return button

    def _build_off(self) -> None:
        self._row(None, "Off")

    def _build_null(self) -> None:
        row = self._row(MathOperation.NULL, "Null")
        self.null_offset_entry = self._field(row, MathOperation.NULL, "Offset", "")
        self.null_unit = self._unit_labels[-1]
        self._apply_button(row, MathOperation.NULL)
        self.capture_button = ttk.Button(
            row, text="Capture current Reading as offset", command=self._capture, state="disabled"
        )
        self.capture_button.pack(side="left", padx=(8, 0))

    def _build_db(self) -> None:
        row = self._row(MathOperation.DB, "dB")
        self.db_reference_entry = self._field(row, MathOperation.DB, "Relative to", "dBm")
        self._apply_button(row, MathOperation.DB)

    def _build_dbm(self) -> None:
        row = self._row(MathOperation.DBM, "dBm")
        self.dbm_resistance_entry = self._field(row, MathOperation.DBM, "Reference Resistance", "Ω")
        self._apply_button(row, MathOperation.DBM)

    def _build_statistics(self) -> None:
        row = self._row(MathOperation.STATISTICS, "Statistics")
        self.statistics_labels: dict[str, ttk.Label] = {}
        for name in _STATISTICS:
            ttk.Label(row, text=_STATISTICS_TITLES[name]).pack(side="left", padx=(12, 4))
            label = ttk.Label(row, text=NOT_KNOWN, width=13)
            label.pack(side="left")
            self.statistics_labels[name] = label
        self.reset_button = ttk.Button(row, text="Reset", command=self._reset, state="disabled")
        self.reset_button.pack(side="left", padx=(8, 0))

    def _build_limit_test(self) -> None:
        row = self._row(MathOperation.LIMIT_TEST, "Limit Test")
        self.limit_lower_entry = self._field(row, MathOperation.LIMIT_TEST, "Lower", "")
        self.limit_upper_entry = self._field(row, MathOperation.LIMIT_TEST, "Upper", "")
        self.limit_unit = self._unit_labels[-1]
        self._apply_button(row, MathOperation.LIMIT_TEST)

    # --- what the user does

    @property
    def selected(self) -> MathOperation | None:
        """The Math Operation the Meter last reported it is doing."""
        return None if self._setup is None else self._setup.math.operation

    def is_chosen(self, operation: MathOperation | None) -> bool:
        """Whether the choice shows `operation` (None for Off)."""
        return self._choice is not None and self._choice.get() == (_OFF if operation is None else operation.value)

    def _chosen(self, operation: MathOperation | None) -> None:
        if operation is None:
            self._problem = ""
            self._request(lambda setup: setup.with_math(setup.math.with_operation(None)))
            self._show_message()
        else:
            self._apply(operation)

    def _apply(self, operation: MathOperation) -> None:
        """Send `operation` with the numbers typed for it; say what is wrong with them instead if they will not do."""
        self._problem = ""
        try:
            changes = self._typed(operation)
            if self._setup is not None:
                self._setup.with_math(replace(self._setup.math, operation=operation, **changes))
        except (ValueError, InvalidSetupError) as error:
            self._problem = str(error)
            self._show_message()
            self._show_choice()
            return
        self._show_message()
        self._request(lambda setup: setup.with_math(replace(setup.math, operation=operation, **changes)))

    def _typed(self, operation: MathOperation) -> dict[str, float]:
        match operation:
            case MathOperation.NULL:
                return {"null_offset": self._number(self.null_offset_entry, "Null offset")}
            case MathOperation.DB:
                return {"db_reference": self._number(self.db_reference_entry, "dB reference")}
            case MathOperation.DBM:
                return {"dbm_reference_resistance": self._number(self.dbm_resistance_entry, "dBm Reference Resistance")}
            case MathOperation.LIMIT_TEST:
                return {
                    "limit_lower": self._number(self.limit_lower_entry, "Limit Test lower bound"),
                    "limit_upper": self._number(self.limit_upper_entry, "Limit Test upper bound"),
                }
            case MathOperation.STATISTICS:
                return {}

    @staticmethod
    def _number(entry: ttk.Entry, name: str) -> float:
        text = entry.get().strip()
        try:
            return float(text)
        except ValueError:
            message = f"{name} must be a number, not {text!r}"
            raise ValueError(message) from None

    def _capture(self) -> None:
        taken = self._latest
        if taken is None or not self._can_capture(taken):
            return
        reading = taken.reading
        nulled_by = taken.setup.math.null_offset if reading.math is MathOperation.NULL else None
        offset = offset_that_nulls(reading.value, nulled_by=nulled_by)
        set_entry(self.null_offset_entry, _number_text(offset))
        self._apply(MathOperation.NULL)

    def _can_capture(self, taken: ReadingTaken) -> bool:
        reading = taken.reading
        return (
            self._setup is not None
            and self._enabled
            and self._setup.function.has_math
            and reading.function is self._setup.function
            and not reading.is_overload
            and not (reading.math is not None and reading.math.in_decibels)
        )

    def _reset(self) -> None:
        self._reset_statistics()

    # --- what the window tells it

    def show(self, setup: Setup, *, enabled: bool) -> None:
        """Show the Math of `setup`. Controls are usable only when `enabled` and the Function has Math Operations."""
        previous = self._setup
        self._setup = setup
        self._enabled = enabled
        function = setup.function
        if previous is not None and previous.function is not function:
            self._problem = ""
        usable = enabled and function.has_math
        self._note = "" if function.has_math else NO_MATH_NOTE
        math_settings = setup.math
        self._show_choice()
        for operation, button in self.choice_buttons.items():
            applies = operation is None or (function.has_decibels or not operation.in_decibels)
            button.configure(state="normal" if usable and applies else "disabled")
        for operation, apply_button in self.apply_buttons.items():
            applies = function.has_decibels or not operation.in_decibels
            apply_button.configure(state="normal" if usable and applies else "disabled")
        unit = function.unit
        self.null_unit.configure(text=unit)
        self.limit_unit.configure(text=unit)
        fields = (
            (self.null_offset_entry, math_settings.null_offset, True),
            (self.db_reference_entry, math_settings.db_reference, function.has_decibels),
            (self.dbm_resistance_entry, math_settings.dbm_reference_resistance, function.has_decibels),
            (self.limit_lower_entry, math_settings.limit_lower, True),
            (self.limit_upper_entry, math_settings.limit_upper, True),
        )
        for entry, value, applies in fields:
            set_entry(entry, _number_text(value), editable=usable and applies)
        if math_settings.operation is not MathOperation.STATISTICS:
            self._clear_statistics()
        self.reset_button.configure(
            state="normal" if usable and math_settings.operation is MathOperation.STATISTICS else "disabled"
        )
        if previous is not None and (previous.function is not function or previous.math != math_settings):
            self._latest = None
        self._show_capture()
        self._show_message()

    def _show_choice(self) -> None:
        if self._choice is not None:
            operation = self.selected
            self._choice.set(_OFF if operation is None else operation.value)

    def _show_message(self) -> None:
        self.message_label.configure(text=self._problem or self._note)

    def _show_capture(self) -> None:
        usable = self._latest is not None and self._can_capture(self._latest)
        self.capture_button.configure(state="normal" if usable else "disabled")

    def handle(self, event: Event) -> None:
        """Keep up with the Worker: the latest Reading (for capturing an offset) and the Meter's Statistics."""
        match event:
            case ReadingTaken():
                if self._setup is not None and event.setup.function is self._setup.function:
                    self._latest = event
                    self._show_capture()
            case StatisticsRead(statistics):
                if self.selected is MathOperation.STATISTICS and self._setup is not None:
                    self._show_statistics(statistics, self._setup)
            case Connected() | ConnectionFailed() | ConnectionLost() | Disconnected():
                self._latest = None
                self._clear_statistics()
                self._show_capture()

    def _show_statistics(self, statistics: MeterStatistics, setup: Setup) -> None:
        def text(value: float) -> str:
            return format_reading(Reading(value, setup.function, ""), Resolution.SIX_HALF)

        shown = {
            "minimum": text(statistics.minimum),
            "maximum": text(statistics.maximum),
            "average": text(statistics.average),
            "count": str(statistics.count),
        }
        for name, label in self.statistics_labels.items():
            label.configure(text=shown[name])

    def _clear_statistics(self) -> None:
        for label in self.statistics_labels.values():
            label.configure(text=NOT_KNOWN)


def set_entry(entry: ttk.Entry, text: str, *, editable: bool = True) -> None:
    """Put `text` in `entry` and make it editable or not."""
    entry.configure(state="normal")
    entry.delete(0, "end")
    entry.insert(0, text)
    entry.configure(state="normal" if editable else "disabled")
