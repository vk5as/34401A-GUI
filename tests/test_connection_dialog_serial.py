import threading
import tkinter as tk
from collections.abc import Callable, Iterator, Mapping

import pytest

from agilent34401a.backend import Backend
from agilent34401a.connection import BackendScan, ConnectionSettings
from agilent34401a.gui.connection_dialog import ConnectionDialog
from agilent34401a.probe import ProbeJob
from agilent34401a.serial_config import FlowControl, Framing, Parity, SerialSettings, Terminator
from agilent34401a.settings import LastConnection
from agilent34401a.sim_serial import SimulatedSerialMeter
from agilent34401a.transport import Transport
from tests.test_connection_dialog import TIMEOUT_S, Chosen, default_detect, pick_backend, pump, scan, select

MakeProbe = Callable[[SerialSettings, Backend, bool], ProbeJob]


class Probes:
    """Stands in for the real Probe: it runs against a simulated line and remembers what it was asked."""

    def __init__(self, line: SimulatedSerialMeter) -> None:
        self.line = line
        self.asked: list[tuple[SerialSettings, Backend, bool]] = []
        self.gate: Callable[[int], None] = lambda _opened: None
        self.jobs: list[ProbeJob] = []

    def __call__(self, base: SerialSettings, backend: Backend, include_flow_control: bool) -> ProbeJob:
        self.asked.append((base, backend, include_flow_control))

        def open_port(settings: SerialSettings) -> Transport:
            self.gate(len(self.line.opened))
            return self.line.open(settings)

        job = ProbeJob(open_port, base, include_flow_control=include_flow_control)
        self.jobs.append(job)
        return job


def hold_second_attempt(release: threading.Event) -> Callable[[int], None]:
    """A gate that makes the Probe wait, with its second attempt announced but not opened, until `release` is set."""

    def gate(opened: int) -> None:
        if opened == 1:
            release.wait(TIMEOUT_S)

    return gate


def serial_scan(_requested: Backend) -> Mapping[Backend, BackendScan]:
    return {Backend.PYVISA_PY: BackendScan(resources=("ASRL/dev/ttyUSB0::INSTR", "GPIB0::22::INSTR"))}


@pytest.fixture
def open_dialog(tk_root: tk.Tk) -> Iterator[Callable[..., tuple[ConnectionDialog, Chosen]]]:
    dialogs: list[ConnectionDialog] = []
    parent = tk.Toplevel(tk_root)
    parent.update()

    def make(
        *,
        probe: MakeProbe | None = None,
        probe_allowed: Callable[[], bool] = lambda: True,
        initial: LastConnection | None = None,
        scan: Callable[[Backend], Mapping[Backend, BackendScan]] = serial_scan,
    ) -> tuple[ConnectionDialog, Chosen]:
        chosen = Chosen()
        dialog = ConnectionDialog(
            parent,
            detect=default_detect,
            scan=scan,
            on_connect=chosen,
            initial=initial,
            probe=probe or Probes(SimulatedSerialMeter()),
            probe_allowed=probe_allowed,
        )
        dialogs.append(dialog)
        pump(tk_root, lambda: dialog.detected)
        return dialog, chosen

    yield make
    for dialog in dialogs:
        dialog.close()
    parent.destroy()


def choose(box, text: str) -> None:
    box.set(text)
    box.event_generate("<<ComboboxSelected>>")


def use_serial(dialog: ConnectionDialog, port: str = "COM3") -> None:
    dialog.serial_radio.invoke()
    dialog.port_box.set(port)


def test_a_serial_connection_defaults_to_the_meters_factory_settings(open_dialog):
    dialog, chosen = open_dialog()

    use_serial(dialog, "/dev/ttyUSB0")
    dialog.connect_button.invoke()

    [(choice, _)] = chosen.calls
    assert choice == LastConnection(
        simulate=False, connection=ConnectionSettings(serial=SerialSettings(port="/dev/ttyUSB0"))
    )


def test_every_serial_parameter_chosen_is_connected_with(open_dialog):
    dialog, chosen = open_dialog()

    use_serial(dialog, "COM4")
    choose(dialog.baud_box, "2400")
    choose(dialog.data_bits_box, "7")
    choose(dialog.parity_box, "Odd")
    choose(dialog.stop_bits_box, "2")
    choose(dialog.flow_box, "RTS/CTS")
    choose(dialog.terminator_box, "CR+LF")
    choose(dialog.dtr_box, "Asserted")
    choose(dialog.rts_box, "Unasserted")
    pick_backend(dialog, Backend.PYVISA_PY)
    dialog.connect_button.invoke()

    [(choice, _)] = chosen.calls
    assert choice.connection == ConnectionSettings(
        backend=Backend.PYVISA_PY,
        serial=SerialSettings(
            port="COM4",
            baud=2400,
            framing=Framing(7, Parity.ODD, 2),
            flow_control=FlowControl.RTS_CTS,
            terminator=Terminator.CRLF,
            dtr=True,
            rts=False,
        ),
    )


def test_the_baud_rate_dropdown_offers_the_six_rates_of_the_meter(open_dialog):
    dialog, _ = open_dialog()

    assert list(dialog.baud_box.cget("values")) == ["300", "600", "1200", "2400", "4800", "9600"]
    assert list(dialog.flow_box.cget("values")) == ["None", "XON/XOFF", "RTS/CTS", "DTR/DSR"]
    assert dialog.baud_box.get() == "9600"


def test_a_serial_connection_without_a_port_is_explained_and_nothing_is_connected(open_dialog):
    dialog, chosen = open_dialog()

    dialog.serial_radio.invoke()
    dialog.connect_button.invoke()

    assert "port" in dialog.error_label.cget("text")
    assert chosen.calls == []


def test_choosing_rs232_enables_the_serial_fields_and_disables_the_gpib_ones(open_dialog):
    dialog, _ = open_dialog()
    assert str(dialog.port_box.cget("state")) == "disabled"
    assert str(dialog.probe_button.cget("state")) == "disabled"

    dialog.serial_radio.invoke()

    assert str(dialog.port_box.cget("state")) == "normal"
    assert str(dialog.baud_box.cget("state")) == "readonly"
    assert str(dialog.probe_button.cget("state")) == "normal"
    assert str(dialog.scan_button.cget("state")) == "normal"
    assert str(dialog.backend_box.cget("state")) == "readonly"
    for widget in (dialog.board_box, dialog.address_box, dialog.resource_entry):
        assert str(widget.cget("state")) == "disabled"

    dialog.meter_radio.invoke()

    assert str(dialog.port_box.cget("state")) == "disabled"
    assert str(dialog.resource_entry.cget("state")) == "normal"

    dialog.simulator_radio.invoke()

    assert str(dialog.probe_button.cget("state")) == "disabled"
    assert str(dialog.scan_button.cget("state")) == "disabled"


def test_the_dialog_starts_from_a_remembered_serial_connection(open_dialog):
    serial = SerialSettings(
        port="COM7",
        baud=1200,
        framing=Framing(7, Parity.EVEN, 1),
        flow_control=FlowControl.DTR_DSR,
        terminator=Terminator.CR,
        dtr=False,
    )
    last = LastConnection(simulate=False, connection=ConnectionSettings(backend=Backend.PYVISA_PY, serial=serial))

    dialog, chosen = open_dialog(initial=last)

    assert dialog.port_box.get() == "COM7"
    assert dialog.baud_box.get() == "1200"
    assert dialog.parity_box.get() == "Even"
    assert dialog.flow_box.get() == "DTR/DSR"
    assert dialog.dtr_box.get() == "Unasserted"
    assert dialog.rts_box.get() == "Automatic"
    dialog.connect_button.invoke()
    assert chosen.calls[0][0] == last


def test_choosing_a_scanned_serial_port_selects_rs232_and_fills_in_the_port(open_dialog, tk_root):
    dialog, chosen = open_dialog()
    scan(dialog, tk_root)

    select(dialog, 0)
    dialog.connect_button.invoke()

    [(choice, _)] = chosen.calls
    assert choice.connection.serial == SerialSettings(port="/dev/ttyUSB0")
    assert choice.connection.backend is Backend.PYVISA_PY


def test_the_port_dropdown_offers_the_serial_ports_a_scan_found(open_dialog, tk_root):
    dialog, _ = open_dialog()

    scan(dialog, tk_root)

    assert list(dialog.port_box.cget("values")) == ["/dev/ttyUSB0"]


def test_probe_fills_in_the_settings_the_meter_answered_at(open_dialog, tk_root):
    probes = Probes(SimulatedSerialMeter(baud=1200, framing=Framing(7, Parity.ODD, 1)))
    dialog, chosen = open_dialog(probe=probes)
    use_serial(dialog, "COM3")

    dialog.probe_button.invoke()
    pump(tk_root, lambda: str(dialog.probe_button.cget("state")) == "normal")

    assert (dialog.baud_box.get(), dialog.data_bits_box.get(), dialog.parity_box.get()) == ("1200", "7", "Odd")
    assert dialog.probe_status.cget("text").startswith("Found the Meter at COM3, 1200 baud, 7O1")
    assert probes.asked == [(SerialSettings(port="COM3"), Backend.AUTO, False)]
    dialog.connect_button.invoke()
    assert chosen.calls[0][0].connection.serial == SerialSettings(
        port="COM3", baud=1200, framing=Framing(7, Parity.ODD, 1)
    )


def test_probe_includes_flow_control_when_asked_to(open_dialog, tk_root):
    probes = Probes(SimulatedSerialMeter(flow_control=FlowControl.XON_XOFF))
    dialog, _ = open_dialog(probe=probes)
    use_serial(dialog)

    dialog.include_flow_check.invoke()
    dialog.probe_button.invoke()
    pump(tk_root, lambda: str(dialog.probe_button.cget("state")) == "normal")

    assert probes.asked[0][2] is True
    assert dialog.flow_box.get() == "XON/XOFF"
    assert "XON/XOFF" in dialog.probe_status.cget("text")


def test_probe_asks_for_the_backend_that_is_chosen(open_dialog, tk_root):
    probes = Probes(SimulatedSerialMeter())
    dialog, _ = open_dialog(probe=probes)
    use_serial(dialog)
    pick_backend(dialog, Backend.PYVISA_PY)

    dialog.probe_button.invoke()
    pump(tk_root, lambda: str(dialog.probe_button.cget("state")) == "normal")

    assert probes.asked[0][1] is Backend.PYVISA_PY


def test_probe_shows_progress_and_the_window_stays_responsive_until_it_is_cancelled(open_dialog, tk_root):
    release = threading.Event()
    line = SimulatedSerialMeter(baud=300)
    probes = Probes(line)
    probes.gate = hold_second_attempt(release)
    dialog, chosen = open_dialog(probe=probes)
    use_serial(dialog)

    try:
        dialog.probe_button.invoke()
        pump(tk_root, lambda: "(2/18)" in dialog.probe_status.cget("text"))

        assert dialog.probe_status.cget("text") == "Trying 9600 baud 7E1 (2/18)"
        assert float(dialog.probe_progress.cget("maximum")) == 18
        assert float(dialog.probe_progress.cget("value")) == 2
        assert str(dialog.probe_button.cget("state")) == "disabled"
        assert str(dialog.probe_cancel_button.cget("state")) == "normal"
        assert str(dialog.connect_button.cget("state")) == "disabled"  # Probe has the port

        dialog.probe_cancel_button.invoke()
        assert dialog.probe_status.cget("text") == "Cancelling…"
    finally:
        release.set()
    pump(tk_root, lambda: str(dialog.probe_button.cget("state")) == "normal")

    assert dialog.probe_status.cget("text") == "Probe cancelled."
    assert str(dialog.probe_cancel_button.cget("state")) == "disabled"
    assert str(dialog.connect_button.cget("state")) == "normal"
    assert len(line.opened) < 18
    assert chosen.calls == []


def test_probe_that_finds_nothing_says_what_to_check(open_dialog, tk_root):
    dialog, _ = open_dialog(probe=Probes(SimulatedSerialMeter(flow_control=FlowControl.RTS_CTS)))
    use_serial(dialog)

    dialog.probe_button.invoke()
    pump(tk_root, lambda: str(dialog.probe_button.cget("state")) == "normal")

    text = dialog.probe_status.cget("text")
    for hint in ("null-modem", "I/O menu", "RS-232", "Flow Control"):
        assert hint in text
    assert dialog.baud_box.get() == "9600"  # nothing was found, so nothing changed


def test_probe_needs_a_serial_port(open_dialog):
    probes = Probes(SimulatedSerialMeter())
    dialog, _ = open_dialog(probe=probes)

    dialog.serial_radio.invoke()
    dialog.probe_button.invoke()

    assert "port" in dialog.probe_status.cget("text")
    assert probes.asked == []


def test_probe_is_refused_while_a_connection_has_the_port(open_dialog):
    probes = Probes(SimulatedSerialMeter())
    dialog, _ = open_dialog(probe=probes, probe_allowed=lambda: False)
    use_serial(dialog)

    dialog.probe_button.invoke()

    assert "Disconnect" in dialog.probe_status.cget("text")
    assert probes.asked == []


def test_a_probe_that_fails_unexpectedly_is_reported_and_the_dialog_still_works(open_dialog):
    def explode(*_args: object) -> ProbeJob:
        message = "boom"
        raise RuntimeError(message)

    dialog, chosen = open_dialog(probe=explode)
    use_serial(dialog)

    dialog.probe_button.invoke()

    assert "boom" in dialog.probe_status.cget("text")
    assert str(dialog.probe_button.cget("state")) == "normal"
    dialog.connect_button.invoke()
    assert len(chosen.calls) == 1


def test_closing_the_dialog_during_a_probe_cancels_it(open_dialog, tk_root):
    release = threading.Event()
    probes = Probes(SimulatedSerialMeter(baud=300))
    probes.gate = hold_second_attempt(release)
    dialog, chosen = open_dialog(probe=probes)
    use_serial(dialog)
    dialog.probe_button.invoke()
    pump(tk_root, lambda: "(2/18)" in dialog.probe_status.cget("text"))

    dialog.cancel_button.invoke()
    release.set()

    probes.jobs[0].join(TIMEOUT_S)
    assert not probes.jobs[0].is_alive()
    assert not dialog.is_open
    assert chosen.calls == []
    assert len(probes.line.opened) < 18
