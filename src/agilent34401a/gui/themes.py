"""Light, Dark and System themes for the ttk widgets.

Native ttk themes (vista, aqua) ignore colour options because the OS draws them, so a real Light/Dark palette needs a
theme that draws itself: `clam`. Light and Dark are separate themes derived from `clam` rather than `clam` repainted,
because `clam` can be what System falls back to on a platform with no native theme.

Widgets should take their colours from ttk styles so they follow the theme. A Tk widget that is not themed by ttk
(a matplotlib canvas, a menu) learns the colours from the `Palette` that `apply_theme` returns.
"""

import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any

from agilent34401a.settings import Theme

ERROR_STYLE = "Error.TLabel"

# Native ttk themes in order of preference for System; `default` and `clam` exist everywhere.
_SYSTEM_PREFERENCE = ("vista", "xpnative", "winnative", "aqua", "default", "clam")


@dataclass(frozen=True)
class Palette:
    """The colours of a theme, for widgets that ttk styles cannot reach."""

    name: str
    background: str
    surface: str
    foreground: str
    accent: str
    field_background: str
    trough: str
    select_foreground: str
    border: str
    warning: str


LIGHT = Palette(
    name="Light",
    background="#f0f0f0",
    surface="#ffffff",
    foreground="#1a1a1a",
    accent="#2563eb",
    field_background="#ffffff",
    trough="#d9d9d9",
    select_foreground="#ffffff",
    border="#b0b0b0",
    warning="#b00020",
)

DARK = Palette(
    name="Dark",
    background="#232629",
    surface="#2b2f33",
    foreground="#e6e6e6",
    accent="#4d9dff",
    field_background="#33373b",
    trough="#3a3f44",
    select_foreground="#0d0d0d",
    border="#4b5157",
    warning="#ff6b5b",
)


def apply_theme(root: tk.Misc, theme: Theme) -> Palette:
    """Switch every ttk widget in the application to `theme` and return the colours now in force."""
    style = ttk.Style(root)
    if theme is Theme.SYSTEM:
        palette = _use_system(style, root)
    else:
        palette = LIGHT if theme is Theme.LIGHT else DARK
        _use_palette(style, palette)
    style.configure(ERROR_STYLE, foreground=palette.warning)
    root.winfo_toplevel().configure(background=palette.background)
    # The drop-down list of a combobox is a plain Tk listbox that only the option database can reach.
    root.option_add("*TCombobox*Listbox.background", palette.field_background)
    root.option_add("*TCombobox*Listbox.foreground", palette.foreground)
    root.option_add("*TCombobox*Listbox.selectBackground", palette.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", palette.select_foreground)
    return palette


def style_menu(menu: tk.Menu, palette: Palette) -> None:
    """Colour a Tk menu (they are not ttk widgets) with `palette`."""
    menu.configure(
        background=palette.surface,
        foreground=palette.foreground,
        activebackground=palette.accent,
        activeforeground=palette.select_foreground,
        selectcolor=palette.foreground,
    )


def _as_hex(widget: tk.Misc, colour: object, fallback: str) -> str:
    """Return `colour` as `#rrggbb`, or `fallback` if Tk cannot resolve it.

    A native ttk theme answers with whatever the OS calls its colours (`SystemButtonFace` on Windows). Tk knows
    those names but matplotlib does not, so every colour is resolved to plain hex here.
    """
    try:
        red, green, blue = widget.winfo_rgb(str(colour))
    except tk.TclError:
        return fallback
    return f"#{red >> 8:02x}{green >> 8:02x}{blue >> 8:02x}"


def _use_system(style: ttk.Style, root: tk.Misc) -> Palette:
    available = style.theme_names()
    for name in _SYSTEM_PREFERENCE:
        if name in available:
            style.theme_use(name)
            break

    def looked_up(style_name: str, option: str, fallback: str) -> str:
        return _as_hex(root, style.lookup(style_name, option), fallback)

    return Palette(
        name="System",
        background=looked_up("TFrame", "background", LIGHT.background),
        surface=looked_up("TButton", "background", LIGHT.surface),
        foreground=looked_up("TLabel", "foreground", LIGHT.foreground),
        accent=LIGHT.accent,
        field_background=looked_up("TEntry", "fieldbackground", LIGHT.field_background),
        trough=looked_up("TScrollbar", "troughcolor", LIGHT.trough),
        select_foreground=LIGHT.select_foreground,
        border=looked_up("TFrame", "bordercolor", LIGHT.border),
        warning=LIGHT.warning,
    )


def _use_palette(style: ttk.Style, palette: Palette) -> None:
    name = f"agilent34401a_{palette.name.lower()}"
    settings = _theme_settings(palette)
    if name in style.theme_names():
        style.theme_settings(name, settings)
    else:
        style.theme_create(name, parent="clam", settings=settings)
    style.theme_use(name)


def _theme_settings(p: Palette) -> dict[str, Any]:
    selected = {
        "background": [("selected", p.accent), ("active", p.accent)],
        "foreground": [("selected", p.select_foreground), ("active", p.select_foreground)],
    }
    field = {"fieldbackground": p.field_background, "foreground": p.foreground, "bordercolor": p.border}
    return {
        ".": {
            "configure": {
                "background": p.background,
                "foreground": p.foreground,
                "fieldbackground": p.field_background,
                "bordercolor": p.border,
                "lightcolor": p.background,
                "darkcolor": p.background,
                "troughcolor": p.trough,
                "arrowcolor": p.foreground,
                "insertcolor": p.foreground,
            },
            "map": {"foreground": [("disabled", "#8a8a8a")]},
        },
        "TButton": {
            "configure": {"background": p.surface},
            "map": {
                "background": [("pressed", p.accent), ("active", p.accent)],
                "foreground": [("pressed", p.select_foreground), ("active", p.select_foreground)],
            },
        },
        "Toolbutton": {"configure": {"background": p.surface}, "map": selected},
        "TNotebook": {"configure": {"background": p.background}},
        "TNotebook.Tab": {
            "configure": {"background": p.surface},
            "map": {
                "background": [("selected", p.accent)],
                "foreground": [("selected", p.select_foreground)],
            },
        },
        "TEntry": {"configure": field},
        "TSpinbox": {"configure": {**field, "background": p.surface}},
        "TCombobox": {
            "configure": {**field, "background": p.surface},
            "map": {
                "fieldbackground": [("readonly", p.field_background)],
                "foreground": [("readonly", p.foreground)],
            },
        },
        "Treeview": {
            "configure": {"fieldbackground": p.field_background, "background": p.field_background},
            "map": {
                "background": [("selected", p.accent)],
                "foreground": [("selected", p.select_foreground)],
            },
        },
        "TScrollbar": {"configure": {"background": p.surface}},
    }
