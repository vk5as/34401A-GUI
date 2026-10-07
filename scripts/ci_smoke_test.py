"""CI smoke test: construct the real GUI against the Simulator, wait for a Reading, shut down cleanly.

Unlike the unit tests this runs outside pytest, so the same script can also be pointed at an installed wheel
(the Release workflow will run it on Windows). It exits non-zero if the window never shows a Reading, or if
closing the window leaves the Worker running.
"""

import sys
import time
import tkinter as tk

from agilent34401a.gui.main_window import NO_READING, MainWindow
from agilent34401a.sim import Simulator

_TIMEOUT_S = 15.0


def main() -> int:
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
    sys.stdout.write(f"GUI construction OK on Python {sys.version.split()[0]}, readout showed {reading}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
