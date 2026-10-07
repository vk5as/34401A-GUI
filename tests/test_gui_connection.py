import gc
import threading
import time
import tkinter as tk
import weakref
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from agilent34401a.backend import Backend, BackendStatus
from agilent34401a.connection import BackendScan, ConnectionSettings, detect_all_backends
from agilent34401a.errors import BackendUnavailableError, TransportError
from agilent34401a.gui.main_window import NO_READING, MainWindow
from agilent34401a.meter import Function
from agilent34401a.settings import LastConnection, Settings
from agilent34401a.sim import Simulator
from agilent34401a.sim_server import SimulatorServer
from agilent34401a.transport import Transport
from tests.test_gui_app import TIMEOUT_S, pump, pump_for

SIMULATOR = LastConnection(simulate=True, connection=ConnectionSettings())
GPIB_METER = LastConnection(simulate=False, connection=ConnectionSettings(backend=Backend.PYVISA_PY, gpib_address=9))


class RecordingSimulator(Simulator):
    """A Simulator that remembers every command it was sent and every time it was closed."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.commands: list[str] = []
        self.closed = False
        self.reads = 0
        self.hold_reading: tuple[int, threading.Event] | None = None
        self.fail_reading_with: Exception | None = None

    def write(self, command: str) -> None:
        self.commands.append(command)
        super().write(command)

    def query(self, command: str) -> str:
        if command == "READ?":
            self.reads += 1
            if self.hold_reading and self.hold_reading[0] == self.reads:
                self.hold_reading[1].wait(TIMEOUT_S)
            if self.fail_reading_with is not None:
                raise self.fail_reading_with
        return super().query(command)

    def close(self) -> None:
        self.closed = True
        super().close()

    @property
    def writes_that_change_the_meter(self) -> list[str]:
        return [command for command in self.commands if not command.endswith("?")]


class Meters:
    """Hands out one prepared Simulator per Connection and remembers what it was asked to connect to."""

    def __init__(self, *simulators: Simulator) -> None:
        self.simulators = list(simulators)
        self.asked: list[LastConnection] = []

    def opener_for(self, choice: LastConnection) -> Callable[[], Transport]:
        self.asked.append(choice)
        simulator = self.simulators.pop(0)
        return lambda: simulator


def detect() -> Mapping[Backend, BackendStatus]:
    return {
        Backend.VENDOR: BackendStatus(available=False, reason="not installed"),
        Backend.PYVISA_PY: BackendStatus(True),
    }


def scan(_requested: Backend) -> Mapping[Backend, BackendScan]:
    return {Backend.PYVISA_PY: BackendScan(resources=("GPIB0::22::INSTR",))}


@pytest.fixture
def make_window(tk_root: tk.Tk) -> Iterator[Callable[..., MainWindow]]:
    windows: list[MainWindow] = []

    def make(meters: Meters, settings: Settings | None = None) -> MainWindow:
        window = MainWindow(
            tk.Toplevel(tk_root),
            settings=settings,
            make_opener=meters.opener_for,
            detect=detect,
            scan=scan,
        )
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.close()


def is_remote(simulator: Simulator) -> bool:
    return simulator.remote  # a function, so the type checker does not assume the answer cannot change


def last_connection(settings: Settings) -> LastConnection | None:
    return settings.last_connection


def status(window: MainWindow) -> str:
    return str(window.status_connection.cget("text"))


def is_connected(window: MainWindow) -> bool:
    return status(window).startswith("Connected")


def menu_state(window: MainWindow, label: str) -> str:
    return str(window.menu("File").entrycget(label, "state"))


def disconnected(window: MainWindow) -> bool:
    return status(window) == "Disconnected"


def test_a_window_with_no_connection_starts_not_connected_with_nothing_running(make_window):
    window = make_window(Meters())

    assert status(window) == "Not connected"
    assert window.readout.cget("text") == NO_READING
    assert str(window.run_button.cget("state")) == "disabled"


def test_connecting_to_the_simulator_shows_its_resource_and_identity_and_readings(make_window):
    meters = Meters(RecordingSimulator(dc_voltage=2.5))
    window = make_window(meters)

    window.connect(SIMULATOR)
    assert status(window) == "Connecting…"
    pump(window, lambda: window.readout.cget("text") != NO_READING)

    assert status(window) == "Connected · Simulator"
    assert "34401A" in window.status_identity.cget("text")
    assert window.readout.cget("text") == "2.500000 V"
    assert meters.asked == [SIMULATOR]


def test_the_status_bar_names_the_resource_of_a_meter_connection(make_window):
    window = make_window(Meters(RecordingSimulator()))

    window.connect(GPIB_METER)
    pump(window, lambda: is_connected(window))

    assert status(window) == "Connected · GPIB0::9::INSTR"


def test_connecting_reads_the_setup_back_and_changes_nothing_on_the_meter(make_window):
    simulator = RecordingSimulator()
    simulator.write('FUNC "RES"')
    simulator.write("RES:RANG 10000")
    simulator.commands.clear()
    window = make_window(Meters(simulator))

    window.connect(SIMULATOR)
    pump(window, lambda: simulator.reads >= 3)

    assert window.function_label.cget("text") == Function.RESISTANCE_2W.label
    assert window.range_box.get() == "10 kΩ"
    assert simulator.writes_that_change_the_meter == []


def test_disconnecting_returns_the_meter_to_local_leaves_its_setup_alone_and_closes_the_connection(make_window):
    simulator = RecordingSimulator()
    simulator.write('FUNC "FREQ"')
    simulator.commands.clear()
    window = make_window(Meters(simulator))
    window.connect(SIMULATOR)
    pump(window, lambda: simulator.reads >= 2)
    assert is_remote(simulator)

    window.disconnect()
    pump(window, lambda: disconnected(window))

    assert not is_remote(simulator)
    assert simulator.closed
    assert simulator.writes_that_change_the_meter == []


def test_closing_the_window_returns_the_meter_to_local(make_window):
    simulator = RecordingSimulator()
    window = make_window(Meters(simulator))
    window.connect(SIMULATOR)
    pump(window, lambda: simulator.reads >= 1)
    assert is_remote(simulator)

    window.close()

    assert not is_remote(simulator)
    assert simulator.closed


def test_disconnecting_does_not_wait_for_a_reading_that_is_still_in_progress(make_window):
    simulator = RecordingSimulator()
    release = threading.Event()
    simulator.hold_reading = (2, release)
    window = make_window(Meters(simulator))
    window.connect(SIMULATOR)
    pump(window, lambda: simulator.reads >= 2)

    began = time.monotonic()
    window.disconnect()
    window.root.update()
    assert time.monotonic() - began < 0.25
    assert status(window) == "Disconnecting…"

    release.set()
    pump(window, lambda: disconnected(window))
    assert not is_remote(simulator)


def test_disconnecting_when_not_connected_does_nothing(make_window):
    window = make_window(Meters())

    window.disconnect()

    assert status(window) == "Not connected"


def test_the_window_can_connect_again_after_disconnecting(make_window):
    first, second = RecordingSimulator(dc_voltage=1.0), RecordingSimulator(dc_voltage=2.0)
    window = make_window(Meters(first, second))
    window.connect(SIMULATOR)
    pump(window, lambda: window.readout.cget("text") == "1.000000 V")
    window.disconnect()
    pump(window, lambda: disconnected(window))

    window.connect(SIMULATOR)
    assert window.readout.cget("text") == NO_READING
    pump(window, lambda: window.readout.cget("text") == "2.000000 V")

    assert is_connected(window)
    assert first.closed
    assert not is_remote(first)
    assert is_remote(second)


def test_connecting_while_connected_returns_the_first_meter_to_local_before_using_the_second(make_window):
    first, second = RecordingSimulator(dc_voltage=1.0), RecordingSimulator(dc_voltage=2.0)
    window = make_window(Meters(first, second))
    window.connect(SIMULATOR)
    pump(window, lambda: window.readout.cget("text") == "1.000000 V")

    window.connect(GPIB_METER)
    pump(window, lambda: window.readout.cget("text") == "2.000000 V")

    assert first.closed
    assert not is_remote(first)
    assert status(window) == "Connected · GPIB0::9::INSTR"


def test_a_failed_connection_is_explained_and_the_window_can_connect_again(make_window):
    def refuse() -> Simulator:
        message = "no such resource"
        raise TransportError(message)

    good = RecordingSimulator(dc_voltage=3.0)

    class Refusing(Meters):
        def opener_for(self, choice: LastConnection) -> Callable[[], Transport]:
            self.asked.append(choice)
            return refuse if len(self.asked) == 1 else (lambda: good)

    window = make_window(Refusing())
    window.connect(GPIB_METER)

    pump(window, lambda: status(window).startswith("Connection failed"))
    assert status(window) == "Connection failed: no such resource"
    assert "File" in window.status_message.cget("text")
    assert str(window.run_button.cget("state")) == "disabled"

    window.connect(GPIB_METER)
    pump(window, lambda: window.readout.cget("text") == "3.000000 V")
    assert window.status_message.cget("text") == ""


def test_a_backend_that_is_not_installed_is_reported_with_its_reason(make_window):
    def unavailable() -> Simulator:
        message = "Keysight/NI VISA is not available: Could not open VISA library"
        raise BackendUnavailableError(message)

    class Missing(Meters):
        def opener_for(self, _choice: LastConnection) -> Callable[[], Transport]:
            return unavailable

    window = make_window(Missing())
    window.connect(GPIB_METER)

    pump(window, lambda: status(window).startswith("Connection failed"))

    assert "Could not open VISA library" in status(window)


def test_a_dropped_connection_is_reported_clearly_and_the_window_can_connect_again(make_window):
    dropping, good = RecordingSimulator(), RecordingSimulator(dc_voltage=4.0)
    window = make_window(Meters(dropping, good))
    window.connect(SIMULATOR)
    pump(window, lambda: dropping.reads >= 2)

    dropping.fail_reading_with = TransportError("the cable was pulled")
    pump(window, lambda: status(window).startswith("Connection lost"))

    assert "the cable was pulled" in status(window)
    assert "File" in window.status_message.cget("text")
    assert str(window.run_button.cget("state")) == "disabled"
    assert str(window.function_buttons[Function.DC_VOLTAGE].cget("state")) == "disabled"
    assert dropping.closed

    window.connect(SIMULATOR)
    pump(window, lambda: window.readout.cget("text") == "4.000000 V")


def test_the_last_connection_is_remembered_once_it_has_connected(make_window, tmp_path: Path):
    settings = Settings.load(tmp_path)
    window = make_window(Meters(RecordingSimulator()), settings)
    assert last_connection(settings) is None

    window.connect(GPIB_METER)
    pump(window, lambda: is_connected(window))

    assert last_connection(settings) == GPIB_METER
    assert Settings.load(tmp_path).last_connection == GPIB_METER


def test_a_connection_that_failed_is_not_remembered(make_window, tmp_path: Path):
    class Refusing(Meters):
        def opener_for(self, _choice: LastConnection) -> Callable[[], Transport]:
            def refuse() -> Transport:
                message = "no such resource"
                raise TransportError(message)

            return refuse

    settings = Settings.load(tmp_path)
    window = make_window(Refusing(), settings)

    window.connect(GPIB_METER)
    pump(window, lambda: status(window).startswith("Connection failed"))

    assert last_connection(settings) is None
    assert not (tmp_path / "settings.json").exists()


def test_settings_that_cannot_be_saved_do_not_stop_the_connection(make_window, tmp_path: Path):
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("a file where the config folder should be", encoding="utf-8")
    window = make_window(Meters(RecordingSimulator()), Settings.load(blocked))

    window.connect(SIMULATOR)
    pump(window, lambda: is_connected(window))

    assert "settings" in window.status_error.cget("text").lower()


def test_the_file_menu_connects_and_disconnects(make_window):
    window = make_window(Meters(RecordingSimulator()))
    assert menu_state(window, "Connect…") == "normal"
    assert menu_state(window, "Disconnect") == "disabled"

    window.connect(SIMULATOR)
    pump(window, lambda: is_connected(window))
    assert menu_state(window, "Disconnect") == "normal"

    window.menu("File").invoke("Disconnect")
    pump(window, lambda: disconnected(window))
    assert menu_state(window, "Disconnect") == "disabled"
    assert menu_state(window, "Connect…") == "normal"


def pick_simulator_in_dialog(window: MainWindow) -> None:
    pump(window, lambda: window.connection_dialog is not None and window.connection_dialog.detected)
    assert window.connection_dialog is not None
    window.connection_dialog.simulator_radio.invoke()
    window.connection_dialog.connect_button.invoke()


def test_the_connect_menu_entry_opens_the_dialog_and_connecting_in_it_connects_the_window(make_window):
    meters = Meters(RecordingSimulator(dc_voltage=5.0))
    window = make_window(meters)

    window.menu("File").invoke("Connect…")
    pick_simulator_in_dialog(window)
    pump(window, lambda: window.readout.cget("text") == "5.000000 V")

    assert meters.asked[0].simulate
    assert window.connection_dialog is None or not window.connection_dialog.is_open


def test_choosing_the_connect_entry_twice_does_not_open_a_second_dialog(make_window):
    window = make_window(Meters())

    window.menu("File").invoke("Connect…")
    first = window.connection_dialog
    window.menu("File").invoke("Connect…")

    assert first is not None
    assert window.connection_dialog is first


def test_the_dialog_starts_from_the_remembered_connection_and_the_reconnect_setting(make_window):
    settings = Settings.in_memory()
    settings.last_connection = GPIB_METER
    settings.auto_reconnect = True
    window = make_window(Meters(), settings)

    window.show_connection_dialog()

    dialog = window.connection_dialog
    assert dialog is not None
    assert dialog.address_box.get() == "9"
    assert dialog.auto_reconnect_var.get() is True


def test_the_reconnect_choice_made_in_the_dialog_is_saved(make_window, tmp_path: Path):
    settings = Settings.load(tmp_path)
    window = make_window(Meters(RecordingSimulator()), settings)
    window.show_connection_dialog()
    pump(window, lambda: window.connection_dialog is not None and window.connection_dialog.detected)
    assert window.connection_dialog is not None

    window.connection_dialog.auto_reconnect_check.invoke()
    window.connection_dialog.simulator_radio.invoke()
    window.connection_dialog.connect_button.invoke()

    assert settings.auto_reconnect is True
    assert Settings.load(tmp_path).auto_reconnect is True


def test_closing_the_window_with_the_dialog_open_closes_both(make_window):
    window = make_window(Meters())
    window.show_connection_dialog()
    dialog = window.connection_dialog
    assert dialog is not None

    window.close()

    assert not dialog.is_open


def test_scanning_in_the_dialog_uses_the_scan_the_window_was_given(make_window):
    window = make_window(Meters())
    window.show_connection_dialog()
    dialog = window.connection_dialog
    assert dialog is not None

    dialog.scan_button.invoke()
    pump(window, lambda: str(dialog.scan_button.cget("state")) == "normal")

    assert list(dialog.resource_list.get(0, "end")) == ["GPIB0::22::INSTR  (pyvisa-py)"]


def test_the_window_stays_responsive_while_a_connection_is_being_opened(make_window):
    release = threading.Event()
    simulator = RecordingSimulator()

    class Slow(Meters):
        def opener_for(self, _choice: LastConnection) -> Callable[[], Transport]:
            def open_slowly() -> Transport:
                release.wait(TIMEOUT_S)
                return simulator

            return open_slowly

    window = make_window(Slow())
    began = time.monotonic()
    window.connect(SIMULATOR)
    pump_for(window, 0.1)

    assert status(window) == "Connecting…"
    assert time.monotonic() - began < 1.0
    release.set()
    pump(window, lambda: is_connected(window))


def test_the_connection_settings_the_dialog_collects_reach_the_opener(make_window):
    meters = Meters(RecordingSimulator())
    window = make_window(meters)

    window.connect(LastConnection(simulate=False, connection=ConnectionSettings(resource="TCPIP::h::5025::SOCKET")))
    pump(window, lambda: is_connected(window))

    assert status(window) == "Connected · TCPIP::h::5025::SOCKET"


@pytest.fixture
def make_real_window(tk_root: tk.Tk) -> Iterator[Callable[[], MainWindow]]:
    """A window that opens Connections the real way: the in-process Simulator, or pyvisa-py over a socket."""
    windows: list[MainWindow] = []

    def make() -> MainWindow:
        window = MainWindow(tk.Toplevel(tk_root), detect=detect, scan=scan)
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.close()
    gc.collect()


def test_the_real_opener_connects_to_the_in_process_simulator(make_real_window):
    window = make_real_window()

    window.connect(SIMULATOR)
    pump(window, lambda: window.readout.cget("text") != NO_READING)

    assert status(window) == "Connected · Simulator"


def test_the_real_opener_connects_to_the_socket_simulator_through_pyvisa_py_and_disconnects(make_real_window):
    with SimulatorServer(port=0) as server:
        window = make_real_window()
        choice = LastConnection(
            simulate=False, connection=ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name)
        )

        window.connect(choice)
        pump(window, lambda: window.readout.cget("text") != NO_READING)
        assert status(window) == f"Connected · {server.resource_name}"

        window.disconnect()
        pump(window, lambda: disconnected(window))


def test_the_real_opener_reports_a_socket_nobody_listens_on_without_freezing(make_real_window, unused_port):
    window = make_real_window()
    choice = LastConnection(
        simulate=False,
        connection=ConnectionSettings(backend=Backend.PYVISA_PY, resource=f"TCPIP::127.0.0.1::{unused_port}::SOCKET"),
    )

    window.connect(choice)
    pump(window, lambda: status(window).startswith("Connection failed"))

    assert str(window.run_button.cget("state")) == "disabled"


def test_the_real_opener_reports_a_backend_that_is_not_installed_with_its_reason(make_real_window):
    window = make_real_window()
    choice = LastConnection(simulate=False, connection=ConnectionSettings(backend=Backend.VENDOR))
    if detect_all_backends()[Backend.VENDOR].available:
        pytest.skip("vendor VISA is installed here")

    window.connect(choice)
    pump(window, lambda: status(window).startswith("Connection failed"))

    assert "Keysight/NI VISA is not available" in status(window)


def _connect_open_the_dialog_and_close(tk_root: tk.Tk) -> list[weakref.ref[object]]:
    meters = Meters(RecordingSimulator())
    window = MainWindow(tk.Toplevel(tk_root), make_opener=meters.opener_for, detect=detect, scan=scan)
    window.connect(SIMULATOR)
    pump(window, lambda: window.readout.cget("text") != NO_READING)
    window.show_connection_dialog()
    dialog = window.connection_dialog
    assert dialog is not None
    window.close()
    return [weakref.ref(window), weakref.ref(dialog)]


def test_a_closed_window_with_a_connection_and_a_dialog_is_freed_at_once_not_by_the_cycle_collector(
    tk_root,
):
    """Tk objects may only be finalised on the Tk thread, so closing must leave no reference cycle behind."""
    gc.disable()
    try:
        freed = _connect_open_the_dialog_and_close(tk_root)

        assert [reference() for reference in freed] == [None, None]
    finally:
        gc.enable()
