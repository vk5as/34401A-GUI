"""Keyboard shortcuts: one table of what is bound, what each does, and which are planned but not yet available.

Sequences are written the way the Help menu shows them ("F1", "Space", "R", "Ctrl+L", "Ctrl+,") and translated to Tk
bindings here. Shortcuts without Ctrl or Alt are keys a text field needs, so they stay quiet while one has focus.
"""

import re
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from tkinter import ttk

_MODIFIERS = {"ctrl": "Control", "shift": "Shift", "alt": "Alt"}
_NAMED_KEYS = {"space": "space", ",": "comma", ".": "period", "/": "slash", "+": "plus", "-": "minus"}
_FUNCTION_KEY = re.compile(r"F([1-9]|1[0-9]|2[0-4])")

# Widgets whose own key bindings use the plain keys: typing text, or Space pressing the focused button.
_TEXT_CLASSES = frozenset({"Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox"})
_BUTTON_CLASSES = frozenset({"Button", "TButton", "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton"})


def tk_sequences(sequence: str) -> list[str]:
    """Return the Tk event patterns that make up `sequence`; letters are bound in both cases for Caps Lock."""
    *modifiers, key = re.split(r"\+(?=.)", sequence) if sequence else [""]
    prefix = ""
    seen: set[str] = set()
    for modifier in modifiers:
        name = _MODIFIERS.get(modifier.lower())
        if name is None or name in seen:
            message = f"Not a shortcut: {sequence!r} (unknown or repeated modifier {modifier!r})"
            raise ValueError(message)
        seen.add(name)
        prefix += f"{name}-"
    if _FUNCTION_KEY.fullmatch(key):
        return [f"<{prefix}Key-{key}>"]
    named = _NAMED_KEYS.get(key.lower())
    if named is not None:
        return [f"<{prefix}Key-{named}>"]
    if len(key) == 1 and key.isalnum():
        if "Shift" in seen:
            return [f"<{prefix}Key-{key.upper()}>"]
        return (
            [f"<{prefix}Key-{key.lower()}>", f"<{prefix}Key-{key.upper()}>"]
            if key.isalpha()
            else [f"<{prefix}Key-{key}>"]
        )
    message = f"Not a shortcut: {sequence!r} (unknown key {key!r})"
    raise ValueError(message)


@dataclass
class Shortcut:
    """One row of the shortcut table; it is unavailable (planned) until it has a handler."""

    sequence: str
    description: str
    handler: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        return self.handler is not None


class Shortcuts:
    """The shortcut table of one window."""

    def __init__(self, window: tk.Misc) -> None:
        self._window = window
        self._table: dict[str, Shortcut] = {}

    def plan(self, sequence: str, description: str) -> None:
        """List a shortcut as planned: shown in Help, but it does nothing until someone registers it."""
        tk_sequences(sequence)
        self._table.setdefault(sequence, Shortcut(sequence, description))

    def register(self, sequence: str, description: str, handler: Callable[[], None]) -> None:
        """Bind `sequence` to `handler`, taking over a planned shortcut's place in the table if there is one."""
        existing = self._table.get(sequence)
        if existing is not None and existing.available:
            message = f"The shortcut {sequence} is already used for: {existing.description}"
            raise ValueError(message)
        patterns = tk_sequences(sequence)
        shortcut = Shortcut(sequence, description, handler)
        self._table[sequence] = shortcut
        plain = "Control" not in patterns[0] and "Alt" not in patterns[0]
        for pattern in patterns:
            self._window.bind(pattern, partial(self._fire, shortcut, plain=plain), add=True)

    def clear(self) -> None:
        """Forget every handler, which breaks the reference cycle with the window that owns them."""
        self._table.clear()

    def entries(self) -> list[Shortcut]:
        """Every shortcut in the table, in the order it was first listed."""
        return list(self._table.values())

    @staticmethod
    def _fire(shortcut: Shortcut, event: "tk.Event[tk.Misc]", *, plain: bool) -> str | None:
        if shortcut.handler is None or (plain and _is_quiet_for(event.widget, shortcut.sequence)):
            return None
        shortcut.handler()
        return "break"


def _is_quiet_for(focus: tk.Misc, sequence: str) -> bool:
    """Whether a plain-key shortcut must stay out of the way of the widget that has focus."""
    widget_class = focus.winfo_class()
    if widget_class in _TEXT_CLASSES:
        return not (isinstance(focus, ttk.Combobox) and str(focus.cget("state")) == "readonly")
    return widget_class in _BUTTON_CLASSES and sequence == "Space"
