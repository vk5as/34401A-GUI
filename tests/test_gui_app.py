import os
import re
import threading
import time
import tkinter as tk
from collections.abc import Callable

import pytest

from agilent34401a import __version__
from agilent34401a.errors import TransportError
from agilent34401a.gui.app import create_window, main
from agilent34401a.gui.main_window import NO_READING, MainWindow
from agilent34401a.sim import AGILENT_IDENTITY, Simulator
from agilent34401a.transport import Transport

TIMEOUT_S = 10.0


class CountingSimulator(Simulator):
    """A Simulator that counts Readings and can garble chosen ones."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads = 0
        self.garbage_on: set[int] = set()
        self.hold_reading: tuple[int, threading.Event] | None = None

    def query(self, command: str) -> str:
        if command == "READ?":
            self.reads += 1
            if self.hold_reading and self.hold_reading[0] == self.reads:
                self.hold_reading[1].wait(TIMEOUT_S)
            if self.reads in self.garbage_on:
                return "not a number"
        return super().query(command)


def pump(window: MainWindow, until: Callable[[], bool], timeout: float = TIMEOUT_S) -> None:
    """Run Tk's event loop the way mainloop would, until `until` holds."""
    deadline = time.monotonic() + timeout
    while not until():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting for the window")
        window.root.update()
        time.sleep(0.002)


def pump_for(window: MainWindow, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    pump(window, lambda: time.monotonic() >= deadline)


@pytest.fixture
def make_window():
    windows: list[MainWindow] = []

    def make(simulator: Simulator | None = None, *, opener: Callable[[], Transport] | None = None) -> MainWindow:
        meter = simulator if simulator is not None else Simulator()
        try:
            window = create_window(opener or (lambda: meter), "Simulator")
        except tk.TclError:
            # CI always has a display (xvfb on Linux), so a missing one there is a failure, not a skip.
            if os.environ.get("CI"):
                raise
            pytest.skip("no display available")
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.close()


def connected(window: MainWindow) -> bool:
    return str(window.status_connection.cget("text")).startswith("Connected")


def test_window_is_titled_with_the_version(make_window):
    window = make_window()

    assert __version__ in window.root.title()


def test_status_bar_shows_the_connection_state_resource_and_identity(make_window):
    window = make_window(Simulator(identity=AGILENT_IDENTITY))
    assert window.status_connection.cget("text") == "Connecting…"

    pump(window, lambda: connected(window))

    assert window.status_connection.cget("text") == "Connected · Simulator"
    assert "Agilent Technologies" in window.status_identity.cget("text")
    assert "34401A" in window.status_identity.cget("text")


def test_continuous_readings_update_the_readout_as_soon_as_the_window_is_connected(make_window):
    window = make_window(Simulator(dc_voltage=1.234567))
    assert window.readout.cget("text") == NO_READING

    pump(window, lambda: window.readout.cget("text") != NO_READING)

    assert window.readout.cget("text") == "1.234567 V"
    assert window.function_label.cget("text") == "DC V"


def test_an_overload_reading_is_shown_as_ovld(make_window):
    window = make_window(Simulator(dc_voltage=-500.0))

    pump(window, lambda: window.readout.cget("text") != NO_READING)

    assert window.readout.cget("text") == "OVLD"


def test_pause_stops_the_readings_and_run_starts_them_again(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: simulator.reads > 0)
    assert window.run_button.cget("text") == "Pause"

    window.run_button.invoke()
    assert window.run_button.cget("text") == "Run"
    pump_for(window, 0.2)  # lets the Reading in progress finish
    paused_at = simulator.reads
    pump_for(window, 0.2)

    assert simulator.reads == paused_at

    window.run_button.invoke()
    assert window.run_button.cget("text") == "Pause"
    pump(window, lambda: simulator.reads > paused_at)


def test_run_and_pause_are_unavailable_until_the_meter_is_connected(make_window):
    window = make_window()

    assert str(window.run_button.cget("state")) == "disabled"

    pump(window, lambda: connected(window))

    assert str(window.run_button.cget("state")) == "normal"


def test_status_bar_shows_the_reading_rate_while_running_and_clears_it_when_paused(make_window):
    window = make_window(Simulator(time_scale=0.1, sleep=time.sleep))  # 40 ms per Reading
    assert window.status_rate.cget("text") == ""

    pump(window, lambda: re.fullmatch(r"\d+\.\d Readings/s", window.status_rate.cget("text")) is not None)

    window.run_button.invoke()
    pump_for(window, 0.3)  # Readings still in flight must not bring the rate back

    assert window.status_rate.cget("text") == ""


def test_window_stays_responsive_while_readings_are_slow(make_window):
    simulator = CountingSimulator(time_scale=1, sleep=time.sleep)  # 400 ms per Reading
    window = make_window(simulator)
    pump(window, lambda: connected(window))

    slowest_update = 0.0
    deadline = time.monotonic() + TIMEOUT_S
    while simulator.reads < 2 and time.monotonic() < deadline:
        started = time.monotonic()
        window.root.update()
        slowest_update = max(slowest_update, time.monotonic() - started)
        time.sleep(0.002)

    assert simulator.reads >= 2
    assert slowest_update < 0.25  # a window blocked on a Reading would stall for the full 400 ms


def test_pausing_while_a_slow_reading_is_in_progress_does_not_block_the_window(make_window):
    simulator = CountingSimulator(time_scale=1, sleep=time.sleep)  # 400 ms per Reading
    window = make_window(simulator)
    pump(window, lambda: simulator.reads > 0)

    started = time.monotonic()
    window.run_button.invoke()
    window.root.update()

    assert time.monotonic() - started < 0.25


def test_a_failed_connection_is_reported_and_leaves_the_window_usable(make_window):
    def refuse():
        message = "no such resource"
        raise TransportError(message)

    window = make_window(opener=refuse)

    pump(window, lambda: window.status_connection.cget("text").startswith("Connection failed"))

    assert window.status_connection.cget("text") == "Connection failed: no such resource"
    assert str(window.run_button.cget("state")) == "disabled"
    assert window.readout.cget("text") == NO_READING


def test_a_device_that_is_not_a_34401a_is_refused(make_window):
    window = make_window(Simulator(identity="Rigol Technologies,DM3058,DM3O123456789,01.01"))

    pump(window, lambda: window.status_connection.cget("text").startswith("Connection failed"))

    assert "Rigol Technologies" in window.status_connection.cget("text")


def test_a_lost_reading_is_reported_in_the_status_bar_and_cleared_by_the_next_one(make_window):
    simulator = CountingSimulator()
    simulator.garbage_on = {2}
    next_reading = threading.Event()
    simulator.hold_reading = (3, next_reading)  # keeps the third Reading back until the lost one has been seen
    window = make_window(simulator)

    pump(window, lambda: "not a number" in window.status_message.cget("text"))
    next_reading.set()
    pump(window, lambda: window.status_message.cget("text") == "")

    assert window.readout.cget("text") == "1.000000 V"


def test_an_unexpected_worker_failure_is_reported_and_stops_the_readings(make_window):
    class Exploding(Simulator):
        def query(self, command: str) -> str:
            if command == "READ?":
                message = "boom"
                raise RuntimeError(message)
            return super().query(command)

    window = make_window(Exploding())

    pump(window, lambda: window.status_connection.cget("text").startswith("Failed"))

    assert "boom" in window.status_connection.cget("text")
    assert str(window.run_button.cget("state")) == "disabled"


def test_closing_the_window_shuts_the_worker_down_and_closes_the_transport(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: simulator.reads > 0)

    window.close()

    assert window.worker_is_alive() is False
    with pytest.raises(TransportError):
        simulator.query("*IDN?")
    with pytest.raises(tk.TclError):
        window.root.winfo_exists()


def test_closing_twice_is_harmless(make_window):
    window = make_window()

    window.close()
    window.close()


def test_the_window_manager_close_button_closes_the_window_cleanly(make_window):
    simulator = CountingSimulator()
    window = make_window(simulator)
    pump(window, lambda: simulator.reads > 0)

    window.root.tk.call(window.root.protocol("WM_DELETE_WINDOW"))

    assert window.worker_is_alive() is False


def test_running_the_app_with_simulate_shows_a_window_and_returns_success_when_it_is_closed(monkeypatch):
    shown_titles = []

    def close_immediately(self, _n=0):
        shown_titles.append(self.title())
        self.destroy()

    try:
        tk.Tk().destroy()
    except tk.TclError:
        if os.environ.get("CI"):
            raise
        pytest.skip("no display available")
    monkeypatch.setattr(tk.Tk, "mainloop", close_immediately)

    assert main(["--simulate"]) == 0
    assert shown_titles == [f"Agilent 34401A {__version__}"]


def test_running_the_app_without_simulate_explains_that_connections_are_not_available_yet(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2
    assert "--simulate" in capsys.readouterr().err


def test_version_flag_prints_the_version_and_exits_successfully(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out
