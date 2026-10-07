"""GUI entry point (`agilent34401a-gui`)."""

import argparse
import gc
import tkinter as tk
from collections.abc import Callable, Sequence

from agilent34401a import __version__
from agilent34401a.gui.main_window import MainWindow
from agilent34401a.settings import Settings
from agilent34401a.sim import Simulator
from agilent34401a.transport import Transport


def create_window(
    open_transport: Callable[[], Transport], resource: str, *, settings: Settings | None = None
) -> MainWindow:
    return MainWindow(tk.Tk(), open_transport, resource, settings=settings)


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
    # Python's cyclic garbage collector runs on whichever thread happens to allocate, and finalising a Tk object
    # on the Worker thread aborts the process. So it is off while the window lives and the window collects on the
    # Tk thread instead (ADR-0008).
    collector_was_on = gc.isenabled()
    gc.disable()
    try:
        window = create_window(Simulator, "Simulator", settings=Settings.load())
        try:
            window.root.mainloop()
        finally:
            window.stop()
    finally:
        if collector_was_on:
            gc.enable()
    return 0
