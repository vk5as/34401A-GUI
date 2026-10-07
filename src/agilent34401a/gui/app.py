"""GUI entry point (`agilent34401a-gui`)."""

import argparse
import tkinter as tk
from collections.abc import Sequence
from tkinter import ttk

from agilent34401a import __version__


def create_window() -> tk.Tk:
    root = tk.Tk()
    root.title(f"Agilent 34401A {__version__}")
    ttk.Label(root, text="Agilent 34401A remote control").pack(padx=24, pady=24)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agilent34401a-gui",
        description="Graphical remote control for the Agilent/HP 34401A digital multimeter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    create_window().mainloop()
    return 0
