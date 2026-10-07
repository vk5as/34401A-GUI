import os
import socket
import time
import tkinter as tk
from collections.abc import Callable, Iterator

import pytest

from agilent34401a.gui.main_window import MainWindow
from agilent34401a.sim import TIME_SCALE_ENV_VAR, Simulator
from agilent34401a.transport import Transport


@pytest.fixture(autouse=True)
def _instant_simulator(monkeypatch):
    # Tests never wait on simulated measurement time unless they ask for a time scale explicitly.
    monkeypatch.setenv(TIME_SCALE_ENV_VAR, "0")


@pytest.fixture
def unused_port() -> int:
    """A TCP port that was free a moment ago, so connecting to it is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def _isolated_config_folder(monkeypatch, tmp_path):
    """Point the platform config folder at a temporary one, so no test can read or write the user's settings."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


_TK_START_ATTEMPTS = 3


@pytest.fixture(scope="session")
def tk_root() -> Iterator[tk.Tk]:
    """One Tk interpreter for the whole run, with each test getting its own Toplevel on it.

    Creating a fresh interpreter per test made Windows CI fail now and then with "Can't find a usable init.tcl".
    """
    root = None
    for attempt in range(_TK_START_ATTEMPTS):
        try:
            root = tk.Tk()
            break
        except tk.TclError:
            # CI always has a display (xvfb on Linux), so a missing one there is a failure, not a skip.
            if attempt == _TK_START_ATTEMPTS - 1:
                if os.environ.get("CI"):
                    raise
                pytest.skip("no display available")
            time.sleep(0.5)
    assert root is not None
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def make_window(tk_root):
    windows: list[MainWindow] = []

    def make(simulator: Simulator | None = None, *, opener: Callable[[], Transport] | None = None) -> MainWindow:
        meter = simulator if simulator is not None else Simulator()
        window = MainWindow(tk.Toplevel(tk_root), opener or (lambda: meter), "Simulator")
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.close()
