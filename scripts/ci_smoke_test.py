"""CI smoke test: construct the real GUI against the Simulator, wait for a Reading, shut down cleanly.

Unlike the unit tests this runs outside pytest, so the same script also runs against an installed wheel: the
Release workflow does that on Windows with ``--installed``, from a checkout that holds no source tree, which
additionally requires the package to come from site-packages and to ship its ``py.typed`` marker. It exits
non-zero if the window never shows a Reading, or if closing the window leaves the Worker running.
"""

import argparse
import importlib.resources
import sys
import time
import tkinter as tk
from collections.abc import Sequence
from pathlib import Path

import agilent34401a
from agilent34401a.gui.main_window import NO_READING, MainWindow
from agilent34401a.sim import Simulator

_TIMEOUT_S = 15.0


def check_installed() -> str | None:
    """Return why this is not an installed wheel (None if it is)."""
    location = Path(agilent34401a.__file__)
    if "site-packages" not in location.parts:
        return f"agilent34401a was imported from {location}, not from an installed wheel"
    if not (importlib.resources.files("agilent34401a") / "py.typed").is_file():
        return "The installed wheel does not ship py.typed"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true", help="Also require an installed wheel, not a source tree")
    args = parser.parse_args(argv)
    if args.installed:
        problem = check_installed()
        if problem is not None:
            sys.stderr.write(f"{problem}\n")
            return 1
    root = tk.Tk()
    window = MainWindow(root, lambda: Simulator(time_scale=0), "Simulator")
    try:
        deadline = time.monotonic() + _TIMEOUT_S
        while window.readout.cget("text") == NO_READING:
            if time.monotonic() > deadline:
                sys.stderr.write(
                    f"No Reading appeared within {_TIMEOUT_S:g} s: {window.status_connection.cget('text')}\n"
                )
                return 1
            root.update()
            time.sleep(0.01)
        reading = window.readout.cget("text")
    finally:
        window.close()  # exercises the real shutdown path: the Worker stops and the Transport closes
    if window.worker_is_alive():
        sys.stderr.write("The Worker was still running after the window closed\n")
        return 1
    sys.stdout.write(
        f"GUI construction OK on Python {sys.version.split()[0]}, "
        f"agilent34401a {agilent34401a.__version__}, readout showed {reading}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
