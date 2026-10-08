"""The window survives a misbehaving Meter or Connection, says so, and lets the user connect again."""

import tkinter as tk
from collections.abc import Callable, Iterator

import pytest

from agilent34401a.errors import TransportTimeoutError
from agilent34401a.gui.main_window import MainWindow
from agilent34401a.sim import Simulator
from agilent34401a.worker import Worker
from tests.test_gui_app import pump
from tests.test_gui_connection import SIMULATOR, Meters, RecordingSimulator, detect, scan, status


@pytest.fixture
def make_window(tk_root: tk.Tk) -> Iterator[Callable[[Meters], MainWindow]]:
    windows: list[MainWindow] = []

    def make(meters: Meters) -> MainWindow:
        window = MainWindow(tk.Toplevel(tk_root), make_opener=meters.opener_for, detect=detect, scan=scan)
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.close()


class GarblingSimulator(RecordingSimulator):
    """Answers every Reading with nonsense, once told to."""

    garbling = False

    def query(self, command: str) -> str:
        if command == "READ?" and self.garbling:
            return "\x00 not a reading"
        return super().query(command)


class SilentSimulator(RecordingSimulator):
    """Takes in whatever it is sent and, once told to, says nothing back."""

    silent = False

    def write(self, command: str) -> None:
        if not self.silent:
            super().write(command)

    def read(self) -> str:
        if self.silent:
            message = "no reply"
            raise TransportTimeoutError(message)
        return super().read()


def reconnects_to_a_good_meter(window: MainWindow) -> None:
    window.connect(SIMULATOR)
    pump(window, lambda: window.readout.cget("text") == "4.000000 V")


def test_a_worker_failure_is_reported_in_the_window_which_can_connect_again(make_window):
    class Exploding(RecordingSimulator):
        def query(self, command: str) -> str:
            if command == "READ?":
                message = "boom"
                raise RuntimeError(message)
            return super().query(command)

    window = make_window(Meters(Exploding(), RecordingSimulator(dc_voltage=4.0)))
    window.connect(SIMULATOR)

    pump(window, lambda: status(window).startswith("Failed"))

    assert "RuntimeError: boom" in status(window)
    assert "File" in window.status_message.cget("text")
    assert str(window.run_button.cget("state")) == "disabled"
    reconnects_to_a_good_meter(window)


def test_a_run_of_garbled_replies_ends_in_a_lost_connection_the_window_can_recover_from(make_window):
    garbling = GarblingSimulator()
    window = make_window(Meters(garbling, RecordingSimulator(dc_voltage=4.0)))
    window.connect(SIMULATOR)
    pump(window, lambda: garbling.reads >= 2)

    garbling.garbling = True
    pump(window, lambda: status(window).startswith("Connection lost"))

    assert "replies in a row" in status(window)
    assert garbling.closed
    reconnects_to_a_good_meter(window)


def test_a_meter_that_goes_silent_is_a_lost_connection_the_window_can_recover_from(make_window):
    silent = SilentSimulator()
    window = make_window(Meters(silent, RecordingSimulator(dc_voltage=4.0)))
    window.connect(SIMULATOR)
    pump(window, lambda: silent.reads >= 2)

    silent.silent = True
    pump(window, lambda: status(window).startswith("Connection lost"))

    assert "resynchronised" in status(window)
    assert silent.closed
    reconnects_to_a_good_meter(window)


def test_a_worker_whose_thread_ended_is_replaced_when_the_user_connects_again(make_window, monkeypatch):
    original = Worker._connection
    calls: list[int] = []

    def break_once(self, open_transport):
        calls.append(1)
        if len(calls) == 1:
            message = "bug in the Worker"
            raise RuntimeError(message)
        return original(self, open_transport)

    monkeypatch.setattr(Worker, "_connection", break_once)
    window = make_window(Meters(Simulator(), RecordingSimulator(dc_voltage=4.0)))
    window.connect(SIMULATOR)
    pump(window, lambda: status(window).startswith("Failed"))
    pump(window, lambda: not window.worker_is_alive())

    reconnects_to_a_good_meter(window)

    assert window.worker_is_alive()
