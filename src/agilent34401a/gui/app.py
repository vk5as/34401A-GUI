"""GUI entry point (`agilent34401a-gui`)."""

import argparse
import tkinter as tk
from collections.abc import Callable, Sequence

from agilent34401a import __version__
from agilent34401a.gui.main_window import MainWindow
from agilent34401a.sim import Simulator
from agilent34401a.transport import Transport


def create_window(open_transport: Callable[[], Transport], resource: str) -> MainWindow:
    return MainWindow(tk.Tk(), open_transport, resource)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agilent34401a-gui",
        description="Graphical remote control for the Agilent/HP 34401A digital multimeter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--simulate", action="store_true", help="use the built-in Simulator instead of a Meter")
    args = parser.parse_args(argv)
    if not args.simulate:
        # The connection dialog (Backend, resource, serial parameters) arrives with its own issue.
        parser.error("only the Simulator is available so far; pass --simulate")
    window = create_window(Simulator, "Simulator")
    try:
        window.root.mainloop()
    finally:
        window.stop()
    return 0
