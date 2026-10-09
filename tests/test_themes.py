import re
import tkinter as tk
from tkinter import ttk

import pytest
from matplotlib.colors import to_rgba

from agilent34401a.gui import themes
from agilent34401a.gui.themes import DARK, LIGHT, Palette, apply_theme
from agilent34401a.settings import Theme

HEX_COLOUR = re.compile(r"#[0-9a-f]{6}")


def colours(palette: Palette) -> dict[str, str]:
    return {
        field: getattr(palette, field)
        for field in (
            "background",
            "surface",
            "foreground",
            "accent",
            "field_background",
            "trough",
            "select_foreground",
            "border",
            "warning",
        )
    }


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_the_built_in_palettes_are_plain_hex_colours_that_matplotlib_accepts(palette):
    for name, colour in colours(palette).items():
        assert HEX_COLOUR.fullmatch(colour), name
        to_rgba(colour)


def test_the_system_palette_is_plain_hex_colours_that_matplotlib_accepts(tk_root):
    palette = apply_theme(tk_root, Theme.SYSTEM)

    for name, colour in colours(palette).items():
        assert HEX_COLOUR.fullmatch(colour), f"{name} is {colour!r}"
        to_rgba(colour)


def test_windows_style_colour_names_are_turned_into_hex_so_matplotlib_can_use_them(tk_root, monkeypatch):
    # On Windows the native ttk theme answers with Tk system colour names that Tk understands and matplotlib does
    # not, which made the chart refuse to draw ("Invalid RGBA argument: 'SystemButtonFace'").
    known = {"SystemButtonFace": (0xF000, 0xF100, 0xF200), "SystemWindowText": (0, 0, 0), "SystemWindow": (65535,) * 3}

    def winfo_rgb(colour: str) -> tuple[int, int, int]:
        if colour not in known:
            message = f'unknown color name "{colour}"'
            raise tk.TclError(message)
        return known[colour]

    monkeypatch.setattr(tk_root, "winfo_rgb", winfo_rgb)
    monkeypatch.setattr(ttk.Style, "lookup", lambda _self, _style, option: _NATIVE.get(option, ""))

    palette = apply_theme(tk_root, Theme.SYSTEM)

    assert palette.background == "#f0f1f2"
    assert palette.surface == "#f0f1f2"
    assert palette.foreground == "#000000"
    assert palette.field_background == "#ffffff"
    for colour in colours(palette).values():
        to_rgba(colour)


_NATIVE = {
    "background": "SystemButtonFace",
    "foreground": "SystemWindowText",
    "fieldbackground": "SystemWindow",
    "troughcolor": "SystemButtonFace",
    "bordercolor": "",
}


def test_a_colour_tk_cannot_resolve_falls_back_to_the_light_palette(tk_root, monkeypatch):
    monkeypatch.setattr(ttk.Style, "lookup", lambda _self, _style, _option: "no-such-colour")

    palette = apply_theme(tk_root, Theme.SYSTEM)

    assert palette.background == LIGHT.background
    assert palette.foreground == LIGHT.foreground
    assert palette.border == LIGHT.border


def test_an_empty_answer_from_the_theme_falls_back_to_the_light_palette(tk_root, monkeypatch):
    monkeypatch.setattr(ttk.Style, "lookup", lambda _self, _style, _option: "")

    palette = apply_theme(tk_root, Theme.SYSTEM)

    assert palette.trough == LIGHT.trough


def test_hex_conversion_keeps_the_high_byte_of_each_16_bit_component():
    class Widget:
        def winfo_rgb(self, _colour: str) -> tuple[int, int, int]:
            return (0xABCD, 0x0100, 0xFFFF)

    assert themes._as_hex(Widget(), "anything", "#000000") == "#ab01ff"  # type: ignore[arg-type]
