"""A tooltip for a widget, whose text can change (an empty text shows nothing)."""

import tkinter as tk
from tkinter import ttk

_OFFSET_PX = 4


class Tooltip:
    """Shows `text` in a small window under `widget` while the pointer is over it (even when it is disabled).

    Set `text` to explain why a control is disabled, and to "" when it is not. `window` is the tooltip window while
    one is showing, otherwise None.
    """

    def __init__(self, widget: tk.Widget, text: str = "") -> None:
        self._widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<Destroy>", self._on_leave, add="+")

    def show(self) -> None:
        """Show the tooltip now, unless it has no text or is showing already."""
        if not self.text or self.window is not None:
            return
        window = tk.Toplevel(self._widget)
        window.wm_overrideredirect(boolean=True)
        ttk.Label(window, text=self.text, padding=4, relief="solid", wraplength=320).pack()
        x = self._widget.winfo_rootx() + _OFFSET_PX
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + _OFFSET_PX
        window.wm_geometry(f"+{x}+{y}")
        self.window = window

    def hide(self) -> None:
        """Take the tooltip away if it is showing."""
        window, self.window = self.window, None
        if window is not None:
            window.destroy()

    def _on_enter(self, _event: "tk.Event[tk.Misc]") -> None:
        self.show()

    def _on_leave(self, _event: "tk.Event[tk.Misc]") -> None:
        self.hide()
