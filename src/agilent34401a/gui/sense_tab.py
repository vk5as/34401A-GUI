"""The Sense tab: AC Filter, Gate Time, Autozero and Input Impedance, each enabled only for the Functions it applies to.

A control the Function cannot use is disabled, and its tooltip says why, as in the Trigger and Preset tabs.
"""

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from agilent34401a.gui.tooltip import Tooltip
from agilent34401a.meter import AcFilter, Autozero, GateTime, InputImpedance, Setup

_NOT_APPLICABLE = "—"
_NOT_CONNECTED = "Connect to a Meter first."
_NO_AC_FILTER = "AC Filter applies to AC voltage and AC current only."
_NO_GATE_TIME = "Gate Time applies to Frequency and Period only."
_NO_AUTOZERO = "Autozero applies to the Functions that integrate."
_NO_INPUT_IMPEDANCE = "Input Impedance applies to DC voltage only."
_AUTOZERO_ONCE_HINT = "Once takes a single offset measurement and then leaves Autozero off."


def _ignore(_change: Callable[[Setup], Setup]) -> None:
    """Stand in for the window's `request` once the window is gone."""


class SenseTab:
    """Controls for the sense options of the Setup. Changes go out through `request`; the window shows the answer.

    `frame` is the widget to add to the window's notebook. The tab is a plain object that owns the frame rather than
    a Frame itself: a widget that keeps its children as attributes is a reference cycle that outlives the window, and
    the cyclic garbage collector may then free Tk objects on the Worker thread, which crashes.
    """

    def __init__(self, parent: ttk.Notebook, request: Callable[[Callable[[Setup], Setup]], None]) -> None:
        self.frame = ttk.Frame(parent, padding=12)
        self._request = request
        self.frame.bind("<Destroy>", self._on_destroy)
        self.tooltips: dict[str, Tooltip] = {}
        self.ac_filter_box = self._row(0, "AC Filter", self._on_ac_filter, "ac_filter")
        self.gate_time_box = self._row(1, "Gate Time", self._on_gate_time, "gate_time")
        self.autozero_box = self._row(2, "Autozero", self._on_autozero, "autozero")
        self.input_impedance_box = self._row(3, "Input Impedance", self._on_input_impedance, "input_impedance")
        ttk.Label(self.frame, text=_AUTOZERO_ONCE_HINT).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _on_destroy(self, event: "tk.Event[tk.Misc]") -> None:
        if event.widget is self.frame:
            # The window owns this tab and `request` is the window's, so drop it: left in place, the pair would only
            # be freed by the cyclic garbage collector, which may run on the Worker thread and must not touch Tk.
            self._request = _ignore

    def _row(self, row: int, title: str, handler: Callable[[], None], key: str) -> ttk.Combobox:
        ttk.Label(self.frame, text=title).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        box = ttk.Combobox(self.frame, state="disabled", width=10)
        box.bind("<<ComboboxSelected>>", lambda _event: handler())
        box.grid(row=row, column=1, sticky="w", pady=4)
        self.tooltips[key] = Tooltip(box, _NOT_CONNECTED)
        return box

    def show(self, setup: Setup, *, enabled: bool) -> None:
        """Show the options of `setup`. Controls are usable only when `enabled` and the Function has the option.

        A control that is disabled gets a tooltip saying why: the Function it does not apply to, or the missing
        Connection.
        """
        function = setup.function
        self._fill(
            self.ac_filter_box,
            self.tooltips["ac_filter"],
            [ac_filter.label for ac_filter in AcFilter],
            setup.ac_filter and setup.ac_filter.label,
            applicable=enabled and function.has_ac_filter,
            reason=_NO_AC_FILTER,
        )
        self._fill(
            self.gate_time_box,
            self.tooltips["gate_time"],
            [gate_time.label for gate_time in GateTime],
            setup.gate_time and setup.gate_time.label,
            applicable=enabled and function.has_gate_time,
            reason=_NO_GATE_TIME,
        )
        self._fill(
            self.autozero_box,
            self.tooltips["autozero"],
            [autozero.label for autozero in Autozero],
            setup.autozero and setup.autozero.label,
            applicable=enabled and function.has_autozero,
            reason=_NO_AUTOZERO,
        )
        self._fill(
            self.input_impedance_box,
            self.tooltips["input_impedance"],
            [impedance.label for impedance in InputImpedance],
            setup.input_impedance and setup.input_impedance.label,
            applicable=enabled and function.has_input_impedance,
            reason=_NO_INPUT_IMPEDANCE,
        )

    @staticmethod
    def _fill(  # noqa: PLR0913 - the box, its tooltip, its values and why it is off are each one argument
        box: ttk.Combobox,
        tooltip: Tooltip,
        values: list[str],
        shown: str | None,
        *,
        applicable: bool,
        reason: str,
    ) -> None:
        if shown is None:
            box.configure(values=[], state="disabled")
            box.set(_NOT_APPLICABLE)
        else:
            box.configure(values=values, state="readonly" if applicable else "disabled")
            box.set(shown)
        tooltip.text = "" if applicable else (_NOT_CONNECTED if shown is not None else reason)

    def _on_ac_filter(self) -> None:
        index = self.ac_filter_box.current()
        if index >= 0:
            self._request(lambda setup: setup.with_ac_filter(list(AcFilter)[index]))

    def _on_gate_time(self) -> None:
        index = self.gate_time_box.current()
        if index >= 0:
            self._request(lambda setup: setup.with_gate_time(list(GateTime)[index]))

    def _on_autozero(self) -> None:
        index = self.autozero_box.current()
        if index >= 0:
            self._request(lambda setup: setup.with_autozero(list(Autozero)[index]))

    def _on_input_impedance(self) -> None:
        index = self.input_impedance_box.current()
        if index >= 0:
            self._request(lambda setup: setup.with_input_impedance(list(InputImpedance)[index]))
