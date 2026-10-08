"""Take the README's screenshots: the real GUI, driven against the Simulator, under a virtual X display.

Run it from a checkout that has the development install, on Linux with Xvfb and Tk installed
(``sudo apt install xvfb python3-tk``)::

    .venv/bin/python scripts/take_screenshots.py            # writes docs/images/*.png
    .venv/bin/python scripts/take_screenshots.py -o /tmp/x  # or somewhere else, to compare

It starts itself again under ``xvfb-run`` with a fixed 1440x1000 screen at 96 dpi, so the images do not depend on the
desktop, the theme or the display scaling of the machine that takes them. Nothing here touches the user's settings
or Presets: the window gets in-memory ones, and no real Meter or VISA library is used.

What is fixed, so that a re-run gives the same pictures: the Simulator's Applied Signals (``DEMO_SIGNALS``) and its
random seed, the window size, the fonts Xvfb offers, and the number of Readings in the History. What is not: the first
few Readings of the Simulator's signal are taken before the script can pause it, so the sine on the Chart may be
shifted a little, and the time axis follows the wall clock. The connection dialog is shown with Backend detection and
Scan answers made up for the picture (Keysight/NI VISA found, a Meter at GPIB address 22), because what a machine really
has installed would change the picture. This is a maintainer's tool: it is not run by CI.
"""

import argparse
import gc
import os
import shutil
import subprocess  # nosec B404 - only to start this same script under xvfb-run
import sys
import time
import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PIL import Image, ImageGrab

from agilent34401a.backend import Backend, BackendStatus
from agilent34401a.connection import BackendScan
from agilent34401a.gui.main_window import NO_READING, MainWindow
from agilent34401a.math_operations import MathOperation
from agilent34401a.meter import AcFilter, Function, Resolution, Setup
from agilent34401a.settings import Settings, Theme
from agilent34401a.sim import DEMO_SIGNALS, Simulator
from agilent34401a.transport import Transport
from agilent34401a.worker import ReadingTaken

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "images"
_SCREEN = "1440x1000x24"
_DPI = 96
_SEED = 34401
_HISTORY_READINGS = 150
_TIMEOUT_S = 30.0
_COLOURS = 256  # the images are palette PNGs: much smaller, and the GUI uses few colours


class ScreenshotError(Exception):
    """A screenshot could not be taken; the message says what the GUI did not do."""


@dataclass
class _Counter:
    """Counts the Readings the window shows, so the script can wait for the next one."""

    count: int = 0

    def __call__(self, _taken: ReadingTaken) -> None:
        self.count += 1


@dataclass
class _Session:
    root: tk.Tk
    window: MainWindow
    simulators: list[Simulator]
    readings: _Counter
    output: Path
    display: str
    saved: list[Path]


# -- waiting and capturing ---------------------------------------------------------------------------------------


def _pump(session: _Session, seconds: float) -> None:
    """Let the window run for `seconds`, so the Worker's events, redraws and timers happen."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        session.root.update()
        time.sleep(0.01)


def _wait_until(session: _Session, condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _TIMEOUT_S
    while not condition():
        if time.monotonic() > deadline:
            message = f"Gave up waiting for {what} after {_TIMEOUT_S:g} s"
            raise ScreenshotError(message)
        session.root.update()
        time.sleep(0.01)


def _more_than(readings: _Counter, before: int) -> bool:
    return readings.count > before


def _take_readings(session: _Session, count: int) -> None:
    """Take `count` Single Readings, one after the other, each as soon as the previous one has arrived."""
    for _ in range(count):
        before = session.readings.count
        session.window.trigger_tab.single()
        _wait_until(session, partial(_more_than, session.readings, before), "a Reading")


def _save(session: _Session, name: str, widget: tk.Misc) -> None:
    """Save the screen area `widget` covers as `name`.png, after letting everything settle."""
    _pump(session, 0.6)
    widget.update_idletasks()
    left, top = widget.winfo_rootx(), widget.winfo_rooty()
    box = (left, top, left + widget.winfo_width(), top + widget.winfo_height())
    picture = ImageGrab.grab(bbox=box, xdisplay=session.display)
    paletted = picture.quantize(colors=_COLOURS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    path = session.output / f"{name}.png"
    paletted.save(path, optimize=True)
    session.saved.append(path)
    sys.stdout.write(f"{path.name}: {picture.width}x{picture.height}, {path.stat().st_size // 1024} KiB\n")


def _screenshot(session: _Session, name: str) -> None:
    _save(session, name, session.root)


# -- the scenes --------------------------------------------------------------------------------------------------


def _canned_detection() -> Mapping[Backend, BackendStatus]:
    return {Backend.VENDOR: BackendStatus(available=True), Backend.PYVISA_PY: BackendStatus(available=True)}


def _canned_scan(_requested: Backend) -> Mapping[Backend, BackendScan]:
    return {Backend.VENDOR: BackendScan(resources=("GPIB0::22::INSTR",)), Backend.PYVISA_PY: BackendScan()}


def _start(session: _Session) -> None:
    """Wait for the first Reading, stop Continuous, make the Simulator instant, and fill the Chart with Readings."""
    window = session.window
    _wait_until(session, lambda: window.readout.cget("text") != NO_READING, "the first Reading")
    window.pause_continuous()
    _pump(session, 1.0)  # a Reading that was already under way arrives
    session.simulators[0].time_scale = 0.0  # from here on a Reading takes no time, so 150 of them take seconds
    window.chart.clear()
    _take_readings(session, _HISTORY_READINGS)
    # The Chart switches its own autoscaling off when matplotlib first draws it empty (see the report on issue #20),
    # so it is asked to fit the Readings, as the Autoscale button does.
    window.chart.fit_view()


def _themes(session: _Session) -> None:
    window = session.window
    for theme in (Theme.LIGHT, Theme.DARK, Theme.SYSTEM):
        window.set_theme(theme)
        _screenshot(session, f"theme_{theme.value}")
    window.set_theme(Theme.LIGHT)


def _select_function(session: _Session, function: Function) -> None:
    """Press the Function's button, wait for the Meter to change, and take a Reading in it."""
    window = session.window
    window.function_buttons[function].invoke()
    _wait_until(
        session,
        lambda: window.current_setup is not None and window.current_setup.function is function,
        f"the Meter to select {function.label}",
    )
    _take_readings(session, 1)


def _sense(session: _Session) -> None:
    """Show the sense options on AC V, where the AC Filter applies, then go back to DC V."""
    _select_function(session, Function.AC_VOLTAGE)
    session.window.show_tab("Sense")
    _screenshot(session, "tab_sense")
    _select_function(session, Function.DC_VOLTAGE)


def _trigger(session: _Session) -> None:
    window = session.window
    window.show_tab("Trigger")
    tab = window.trigger_tab
    tab.sample_count_var.set("25")
    session.root.update()
    before = session.readings.count
    tab.start_burst()
    _wait_until(session, lambda: tab.last_burst is not None, "the Burst")
    _wait_until(session, lambda: session.readings.count >= before + 25, "the Burst's Readings")
    _screenshot(session, "tab_trigger")
    tab.sample_count_var.set("1")


def _math(session: _Session) -> None:
    """Switch on a Limit Test that the Applied Signal's sine sometimes breaks, and take Readings until one does."""
    window = session.window
    window.show_tab("Math")
    tab = window.math_tab
    tab.limit_lower_entry.delete(0, "end")
    tab.limit_lower_entry.insert(0, "0.9985")
    tab.limit_upper_entry.delete(0, "end")
    tab.limit_upper_entry.insert(0, "1.0015")
    tab.apply_buttons[MathOperation.LIMIT_TEST].invoke()
    _wait_until(session, lambda: tab.selected is MathOperation.LIMIT_TEST, "the Limit Test to be on")
    for _ in range(60):
        _take_readings(session, 1)
        if "HI" in window.readout.cget("text") or "LO" in window.readout.cget("text"):
            break
    _screenshot(session, "tab_math")
    tab.choice_buttons[None].invoke()
    _wait_until(session, lambda: tab.selected is None, "the Limit Test to be off")
    _take_readings(session, 1)


def _system(session: _Session) -> None:
    window = session.window
    window.show_tab("System")
    tab = window.system_tab
    tab.self_test_button.invoke()
    _wait_until(session, lambda: tab.self_test_result.cget("text") != "", "the self-test")
    # A mistake made at the front panel puts an error in the Meter's queue behind the application's back. The Worker is
    # idle (Continuous is paused), so the Simulator can be written to here, and "Check now" then finds the error.
    session.simulators[0].write("VOLT:DC:RANG 5000")  # out of range: error -222
    tab.check_errors_button.invoke()
    _wait_until(session, lambda: bool(tab.error_tree.get_children()), "the error log to show the error")
    _screenshot(session, "tab_system")


def _console(session: _Session) -> None:
    window = session.window
    window.show_tab("SCPI console")
    console = window.console
    _wait_until(session, lambda: str(console.send_button.cget("state")) != "disabled", "the console to be usable")
    for command in ("*IDN?", "SYST:VERS?", "VOLT:DC:NPLC?", "VOLT:DC:RANG 5000", "SYST:ERR?"):
        console.entry.delete(0, "end")
        console.entry.insert(0, command)
        console.submit()
        _pump(session, 0.5)
    console.entry.focus_set()
    _screenshot(session, "tab_console")


def _presets(session: _Session) -> None:
    window = session.window
    window.show_tab("Presets")
    tab = window.presets_tab
    for name, setup in (
        ("Bench supply 10 V", Setup.default(Function.DC_VOLTAGE).with_range(10.0).with_resolution(Resolution.SIX_HALF)),
        ("Mains monitor", Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.FAST)),
        ("Resistor sort", Setup.default(Function.RESISTANCE_4W).with_range(1000.0).with_nplc(10)),
        ("Quick continuity", Setup.default(Function.CONTINUITY)),
    ):
        tab.store.save(name, setup)
    tab.refresh()
    tab.select("Resistor sort")
    tab.apply_selected()
    _wait_until(session, lambda: tab.last_report is not None, "the Preset to be applied")
    _take_readings(session, 1)
    _screenshot(session, "tab_presets")
    _select_function(session, Function.DC_VOLTAGE)


def _connection_dialog(session: _Session) -> None:
    window = session.window
    window.show_connection_dialog()
    dialog = window.connection_dialog
    if dialog is None:
        message = "The connection dialog did not open"
        raise ScreenshotError(message)
    dialog.window.geometry("+60+40")
    _wait_until(session, lambda: dialog.detected, "Backend detection")
    dialog.scan_button.invoke()
    _wait_until(session, lambda: dialog.resource_list.size() > 0, "the Scan")
    dialog.resource_list.selection_set(0)
    dialog.resource_list.event_generate("<<ListboxSelect>>")
    _save(session, "connection_dialog", dialog.window)
    dialog.serial_radio.invoke()
    dialog.port_box.set("COM3")
    dialog.flow_box.set("DTR/DSR")
    dialog.include_flow_check.invoke()
    _save(session, "connection_dialog_rs232", dialog.window)
    dialog.close()
    window.connection_dialog = None


def _compact(session: _Session) -> None:
    window = session.window
    window.show_tab("Chart")
    window.set_compact(compact=True)
    _pump(session, 0.3)
    _screenshot(session, "compact_mode")
    window.set_compact(compact=False)


_SCENES: tuple[Callable[[_Session], None], ...] = (
    _themes,
    _sense,
    _trigger,
    _math,
    _console,
    _system,
    _presets,
    _connection_dialog,
    _compact,
)


# -- running -----------------------------------------------------------------------------------------------------


def take_screenshots(output: Path) -> list[Path]:
    """Build the window against the Simulator and save every screenshot into `output`; return the files written."""
    output.mkdir(parents=True, exist_ok=True)
    simulators: list[Simulator] = []

    def open_simulator() -> Transport:
        simulator = Simulator(signals=DEMO_SIGNALS, seed=_SEED, time_scale=1.0)
        simulators.append(simulator)
        return simulator

    settings = Settings.in_memory()
    settings.theme = Theme.LIGHT
    # Python's cyclic collector must not free Tk objects on the Worker thread, so, as in the application, it is off and
    # the window collects on the Tk thread (ADR-0008).
    collector_was_on = gc.isenabled()
    gc.disable()
    root = tk.Tk()
    root.tk.call("tk", "scaling", _DPI / 72)
    root.geometry("+0+0")
    window = MainWindow(
        root, open_simulator, "Simulator", settings=settings, detect=_canned_detection, scan=_canned_scan
    )
    readings = _Counter()
    window.add_reading_listener(readings)
    session = _Session(root, window, simulators, readings, output, os.environ["DISPLAY"], [])
    try:
        _start(session)
        for scene in _SCENES:
            scene(session)
    finally:
        window.close()
        if collector_was_on:
            gc.enable()
    return session.saved


def _relaunch_under_xvfb(argv: Sequence[str]) -> int:
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run is None:
        sys.stderr.write("take_screenshots.py needs xvfb-run (Debian/Ubuntu: sudo apt install xvfb)\n")
        return 1
    command = [
        xvfb_run,
        "-a",
        "-s",
        f"-screen 0 {_SCREEN} -dpi {_DPI}",
        sys.executable,
        __file__,
        "--inside-xvfb",
        *argv,
    ]
    return subprocess.run(command, check=False).returncode  # noqa: S603 # nosec B603 - our own interpreter and script


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="the folder for the PNGs")
    parser.add_argument("--inside-xvfb", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    if not args.inside_xvfb:
        return _relaunch_under_xvfb(arguments)
    try:
        take_screenshots(args.output)
    except ScreenshotError as error:
        sys.stderr.write(f"take_screenshots.py: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
