import gc
import tkinter as tk
from collections.abc import Callable, Iterator, Mapping

import pytest

from agilent34401a import __version__
from agilent34401a.backend import Backend, BackendStatus
from agilent34401a.connection import BackendScan, ConnectionSettings
from agilent34401a.gui.app import build_parser, create_window, main, startup_connection
from agilent34401a.gui.main_window import MainWindow
from agilent34401a.settings import LastConnection, Settings
from agilent34401a.sim import Simulator
from agilent34401a.transport import Transport
from tests.test_gui_app import pump

SIMULATOR = LastConnection(simulate=True, connection=ConnectionSettings())
LAST = LastConnection(simulate=False, connection=ConnectionSettings(backend=Backend.PYVISA_PY, gpib_address=9))


def startup(argv: list[str], settings: Settings | None = None) -> LastConnection | None:
    parser = build_parser()
    return startup_connection(parser.parse_args(argv), parser, settings or Settings.in_memory())


def test_simulate_connects_to_the_simulator():
    assert startup(["--simulate"]) == SIMULATOR


def test_the_connection_options_are_the_same_as_the_clis():
    chosen = startup(["--backend", "py", "--gpib-board", "1", "--gpib-address", "7"])

    assert chosen == LastConnection(
        simulate=False, connection=ConnectionSettings(backend=Backend.PYVISA_PY, gpib_board=1, gpib_address=7)
    )


def test_a_serial_port_and_its_options_connect_over_rs_232():
    chosen = startup(["--serial-port", "/dev/ttyUSB0", "--baud", "4800", "--parity", "even", "--data-bits", "7"])

    assert chosen is not None
    assert not chosen.simulate
    serial = chosen.connection.serial
    assert serial is not None
    assert (serial.port, serial.baud) == ("/dev/ttyUSB0", 4800)
    assert (serial.framing.data_bits, serial.framing.parity.value) == (7, "even")


def test_serial_options_without_a_port_are_an_error_not_ignored():
    with pytest.raises(SystemExit):
        startup(["--baud", "4800"])


def test_a_raw_resource_string_can_be_given_on_the_command_line():
    chosen = startup(["--backend", "ivi", "--resource", "TCPIP::127.0.0.1::5025::SOCKET"])

    assert chosen == LastConnection(
        simulate=False,
        connection=ConnectionSettings(backend=Backend.VENDOR, resource="TCPIP::127.0.0.1::5025::SOCKET"),
    )


def test_the_gpib_address_defaults_to_22():
    chosen = startup(["--backend", "py"])

    assert chosen is not None
    assert chosen.connection.resource_name == "GPIB0::22::INSTR"


@pytest.mark.parametrize(
    "argv",
    [
        ["--simulate", "--backend", "py"],
        ["--resource", "X", "--gpib-address", "3"],
        ["--backend", "py", "--gpib-address", "31"],
    ],
)
def test_options_that_make_no_sense_together_are_a_usage_error(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        startup(argv)

    assert exit_info.value.code == 2
    assert capsys.readouterr().err


def test_with_no_options_and_nothing_remembered_there_is_nothing_to_connect_to_yet():
    assert startup([]) is None


def test_a_remembered_connection_is_not_used_unless_reconnecting_is_switched_on():
    settings = Settings.in_memory()
    settings.last_connection = LAST

    assert startup([], settings) is None


def test_reconnecting_at_startup_uses_the_remembered_connection():
    settings = Settings.in_memory()
    settings.last_connection = LAST
    settings.auto_reconnect = True

    assert startup([], settings) == LAST


def test_reconnecting_at_startup_with_nothing_remembered_connects_to_nothing():
    settings = Settings.in_memory()
    settings.auto_reconnect = True

    assert startup([], settings) is None


def test_the_command_line_wins_over_the_remembered_connection():
    settings = Settings.in_memory()
    settings.last_connection = LAST
    settings.auto_reconnect = True

    assert startup(["--simulate"], settings) == SIMULATOR


def detect() -> Mapping[Backend, BackendStatus]:
    return {Backend.VENDOR: BackendStatus(available=False, reason="missing"), Backend.PYVISA_PY: BackendStatus(True)}


def scan(_requested: Backend) -> Mapping[Backend, BackendScan]:
    return {}


@pytest.fixture
def make_app_window(tk_root: tk.Tk) -> Iterator[Callable[..., MainWindow]]:
    windows: list[MainWindow] = []

    def make(settings: Settings, first: LastConnection | None, opener: Callable[[], Transport]) -> MainWindow:
        window = create_window(
            settings,
            first,
            root=tk.Toplevel(tk_root),
            make_opener=lambda _choice: opener,
            detect=detect,
            scan=scan,
        )
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.close()


def test_the_window_opens_the_connection_dialog_when_there_is_nothing_to_connect_to(make_app_window):
    window = make_app_window(Settings.in_memory(), None, Simulator)

    assert window.connection_dialog is not None
    assert window.connection_dialog.is_open
    assert window.status_connection.cget("text") == "Not connected"


def test_the_startup_dialog_offers_the_remembered_connection(make_app_window):
    settings = Settings.in_memory()
    settings.last_connection = LAST

    window = make_app_window(settings, None, Simulator)

    assert window.connection_dialog is not None
    assert window.connection_dialog.address_box.get() == "9"


def test_the_window_connects_straight_away_when_it_has_a_connection(make_app_window):
    window = make_app_window(Settings.in_memory(), SIMULATOR, lambda: Simulator(dc_voltage=1.5))

    pump(window, lambda: window.readout.cget("text") == "1.500000 V")

    assert window.connection_dialog is None
    assert window.status_connection.cget("text") == "Connected · Simulator"


@pytest.fixture(autouse=True)
def _free_tk_objects_on_this_thread() -> Iterator[None]:
    """Collect after each test: a Tk interpreter freed on a Worker's thread aborts the whole process."""
    yield
    gc.collect()


def close_like_the_user(root: tk.Tk) -> None:
    """Close the window through the window manager's close button, which is what ends a real mainloop."""
    root.tk.call(root.protocol("WM_DELETE_WINDOW"))


def test_running_the_app_with_simulate_shows_a_window_and_returns_success_when_it_is_closed(monkeypatch):
    shown_titles = []

    def close_immediately(self, _n=0):
        shown_titles.append(self.title())
        close_like_the_user(self)

    monkeypatch.setattr(tk.Tk, "mainloop", close_immediately)

    assert main(["--simulate"]) == 0
    assert shown_titles == [f"Agilent 34401A {__version__}"]


def test_running_the_app_with_no_options_opens_the_dialog_instead_of_refusing_to_start(monkeypatch):
    seen = []
    monkeypatch.setattr("agilent34401a.gui.main_window.detect_all_backends", detect)

    def close_immediately(self, _n=0):
        seen.append([child for child in self.winfo_children() if isinstance(child, tk.Toplevel)])
        close_like_the_user(self)

    monkeypatch.setattr(tk.Tk, "mainloop", close_immediately)

    assert main([]) == 0
    assert len(seen[0]) == 1  # the connection dialog


def test_a_usage_error_stops_the_app_before_any_window_is_made(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--simulate", "--resource", "X"])

    assert exit_info.value.code == 2
    assert "--simulate" in capsys.readouterr().err
