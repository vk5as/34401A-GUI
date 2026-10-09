"""GUI entry point (`agilent34401a-gui`)."""

import argparse
import gc
import tkinter as tk
from collections.abc import Sequence

from agilent34401a import __version__
from agilent34401a.cli import add_connection_options, connection_settings
from agilent34401a.cli_serial import serial_options_given
from agilent34401a.connection import ConnectionSettings
from agilent34401a.gui.connection_dialog import Detect, Scan
from agilent34401a.gui.main_window import MainWindow, MakeOpener
from agilent34401a.settings import LastConnection, Settings


def build_parser() -> argparse.ArgumentParser:
    """Build the GUI's command line: the same Connection options as the CLI, none of which is required."""
    parser = argparse.ArgumentParser(
        prog="agilent34401a-gui",
        description="Graphical remote control for the Agilent/HP 34401A digital multimeter. "
        "Without Connection options the window starts with the connection dialog.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    add_connection_options(parser)
    return parser


def startup_connection(
    args: argparse.Namespace, parser: argparse.ArgumentParser, settings: Settings
) -> LastConnection | None:
    """Decide what to connect to at startup, or None to ask: the command line first, then the remembered Connection.

    The remembered Connection is only used when the user has switched on reconnecting at startup.
    """
    given = (
        args.simulate
        or any(
            value is not None
            for value in (args.backend, args.gpib_board, args.gpib_address, args.resource, args.serial_port)
        )
        or bool(serial_options_given(args))
    )
    if given:
        chosen = connection_settings(args, parser)
        return LastConnection(simulate=chosen is None, connection=chosen or ConnectionSettings())
    return settings.last_connection if settings.auto_reconnect else None


def create_window(  # noqa: PLR0913 - the seams are what the tests replace
    settings: Settings,
    first: LastConnection | None,
    *,
    root: tk.Tk | tk.Toplevel | None = None,
    make_opener: MakeOpener | None = None,
    detect: Detect | None = None,
    scan: Scan | None = None,
) -> MainWindow:
    """Make the main window and start connecting to `first`, or ask which Meter to connect to when there is none."""
    window = MainWindow(root or tk.Tk(), settings=settings, make_opener=make_opener, detect=detect, scan=scan)
    if first is None:
        window.show_connection_dialog()
    else:
        window.connect(first)
    return window


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.load()
    first = startup_connection(args, parser, settings)
    # Python's cyclic garbage collector runs on whichever thread happens to allocate, and finalising a Tk object
    # on the Worker thread aborts the process. So it is off while the window lives and the window collects on the
    # Tk thread instead (ADR-0008).
    collector_was_on = gc.isenabled()
    gc.disable()
    try:
        window = create_window(settings, first)
        try:
            window.root.mainloop()
        finally:
            window.stop()
    finally:
        if collector_was_on:
            gc.enable()
    return 0
