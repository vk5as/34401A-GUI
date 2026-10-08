import tkinter as tk
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from agilent34401a.backend import Backend, BackendStatus
from agilent34401a.connection import BackendScan, ConnectionSettings
from agilent34401a.gui.main_window import MainWindow
from agilent34401a.probe import ProbeJob
from agilent34401a.serial_config import Framing, Parity, SerialSettings
from agilent34401a.settings import LastConnection, Settings
from agilent34401a.sim import Simulator
from agilent34401a.sim_serial import SimulatedSerialMeter
from agilent34401a.transport import Transport
from tests.test_gui_app import pump

SEVEN_E_ONE = Framing(7, Parity.EVEN, 1)


def rs232(**changes: object) -> LastConnection:
    return LastConnection(
        simulate=False, connection=ConnectionSettings(serial=SerialSettings(port="COM3", **changes))  # type: ignore[arg-type]
    )


def detect() -> Mapping[Backend, BackendStatus]:
    return {Backend.VENDOR: BackendStatus(False, "not installed"), Backend.PYVISA_PY: BackendStatus(True)}


def scan(_requested: Backend) -> Mapping[Backend, BackendScan]:
    return {}


class Setup:
    """A window whose RS-232 Connections and Probes run against one simulated line."""

    def __init__(self, tk_root: tk.Tk, line: SimulatedSerialMeter, settings: Settings | None = None) -> None:
        self.line = line
        self.probes: list[SerialSettings] = []

        def make_opener(choice: LastConnection) -> Callable[[], Transport]:
            serial = choice.connection.serial
            assert serial is not None
            return lambda: line.open(serial)

        def probe(base: SerialSettings, _backend: Backend, include_flow_control: bool) -> ProbeJob:
            self.probes.append(base)
            return ProbeJob(line.open, base, include_flow_control=include_flow_control)

        self.window = MainWindow(
            tk.Toplevel(tk_root),
            settings=settings,
            make_opener=make_opener,
            detect=detect,
            scan=scan,
            probe=probe,
        )


@pytest.fixture
def make_setup(tk_root: tk.Tk) -> Iterator[Callable[..., Setup]]:
    setups: list[Setup] = []

    def make(line: SimulatedSerialMeter | None = None, settings: Settings | None = None) -> Setup:
        setup = Setup(tk_root, line or SimulatedSerialMeter(), settings)
        setups.append(setup)
        return setup

    yield make
    for setup in setups:
        setup.window.close()


def is_connected(window: MainWindow) -> bool:
    return str(window.status_connection.cget("text")).startswith("Connected")


def is_remote(simulator: Simulator) -> bool:
    return simulator.remote  # a function, so the type checker does not assume the answer cannot change


def test_the_status_bar_names_the_serial_settings_of_an_rs232_connection(make_setup):
    setup = make_setup(SimulatedSerialMeter(baud=4800, framing=SEVEN_E_ONE))

    setup.window.connect(rs232(baud=4800, framing=SEVEN_E_ONE))
    pump(setup.window, lambda: is_connected(setup.window))

    assert setup.window.status_connection.cget("text") == "Connected · COM3, 4800 baud, 7E1, None"
    assert is_remote(setup.line.simulator)


def test_disconnecting_an_rs232_meter_returns_it_to_local(make_setup):
    setup = make_setup()
    setup.window.connect(rs232())
    pump(setup.window, lambda: is_connected(setup.window))

    setup.window.disconnect()
    pump(setup.window, lambda: not is_connected(setup.window) and not is_remote(setup.line.simulator))

    assert not is_remote(setup.line.simulator)


def test_connecting_with_serial_settings_the_meter_does_not_hear_says_so(make_setup):
    setup = make_setup(SimulatedSerialMeter(baud=9600))

    setup.window.connect(rs232(baud=300))
    pump(setup.window, lambda: str(setup.window.status_connection.cget("text")).startswith("Connection failed"))

    assert "No reply from the Meter on COM3, 300 baud" in str(setup.window.status_connection.cget("text"))


def test_the_serial_connection_is_remembered_with_all_its_settings(make_setup, tmp_path: Path):
    settings = Settings.load(tmp_path)
    setup = make_setup(SimulatedSerialMeter(baud=2400), settings)
    setup.window.show_connection_dialog()
    dialog = setup.window.connection_dialog
    assert dialog is not None
    pump(setup.window, lambda: dialog.detected)

    dialog.serial_radio.invoke()
    dialog.port_box.set("COM3")
    dialog.baud_box.set("2400")
    dialog.connect_button.invoke()
    pump(setup.window, lambda: is_connected(setup.window))

    assert Settings.load(tmp_path).last_connection == rs232(baud=2400)


def test_probe_in_the_window_s_dialog_finds_the_meter(make_setup):
    setup = make_setup(SimulatedSerialMeter(baud=600))
    setup.window.show_connection_dialog()
    dialog = setup.window.connection_dialog
    assert dialog is not None
    pump(setup.window, lambda: dialog.detected)
    dialog.serial_radio.invoke()
    dialog.port_box.set("COM3")

    dialog.probe_button.invoke()
    pump(setup.window, lambda: str(dialog.probe_button.cget("state")) == "normal")

    assert dialog.baud_box.get() == "600"
    assert setup.probes == [SerialSettings(port="COM3")]


def test_probe_is_refused_while_the_window_has_a_connection(make_setup):
    setup = make_setup()
    setup.window.connect(rs232())
    pump(setup.window, lambda: is_connected(setup.window))
    setup.window.show_connection_dialog()
    dialog = setup.window.connection_dialog
    assert dialog is not None
    dialog.serial_radio.invoke()
    dialog.port_box.set("COM3")

    dialog.probe_button.invoke()

    assert "Disconnect" in str(dialog.probe_status.cget("text"))
    assert setup.probes == []
