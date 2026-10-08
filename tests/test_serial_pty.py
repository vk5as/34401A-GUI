"""Serial Connections through the real pyvisa-py and pyserial stack, over a pseudo-terminal (POSIX only)."""

import contextlib
import os
import sys
import threading
from collections.abc import Iterator

import pytest

from agilent34401a.driver import Driver
from agilent34401a.probe import run_probe
from agilent34401a.serial_config import FlowControl, Framing, Parity, SerialSettings, Terminator
from agilent34401a.transport import LocalControl, RemoteControl
from agilent34401a.visa import open_serial_transport

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")

TIMEOUT_S = 5.0
IDENTITY = "HEWLETT-PACKARD,34401A,0,11-5-2"


class FakeMeterOnAPty:
    """Plays the Meter at the far end of a pseudo-terminal: it answers `*IDN?` and records every line it hears."""

    def __init__(self) -> None:
        self.master, slave = os.openpty()
        self.port = os.ttyname(slave)
        self._slave = slave  # held open so the line stays up while the application opens and closes the port
        self.lines: list[str] = []
        self.answer = True
        self._stop = threading.Event()
        self._heard = threading.Condition()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        buffer = b""
        os.set_blocking(self.master, False)
        while not self._stop.is_set():
            try:
                data = os.read(self.master, 256)
            except BlockingIOError:
                self._stop.wait(0.005)
                continue
            except OSError:
                return
            buffer += data
            while b"\n" in buffer or b"\r" in buffer:
                line, _, buffer = buffer.replace(b"\r", b"\n").partition(b"\n")
                text = line.decode("ascii", errors="replace").strip()
                if not text:
                    continue
                with self._heard:
                    self.lines.append(text)
                    self._heard.notify_all()
                if text == "*IDN?" and self.answer:
                    os.write(self.master, f"{IDENTITY}\r\n".encode("ascii"))

    def wait_for(self, count: int) -> None:
        with self._heard:
            assert self._heard.wait_for(lambda: len(self.lines) >= count, TIMEOUT_S)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(TIMEOUT_S)
        for descriptor in (self.master, self._slave):
            with contextlib.suppress(OSError):
                os.close(descriptor)


@pytest.fixture
def meter() -> Iterator[FakeMeterOnAPty]:
    fake = FakeMeterOnAPty()
    yield fake
    fake.close()


def test_a_serial_connection_through_pyvisa_py_talks_to_the_meter_and_hands_it_back(meter):
    settings = SerialSettings(
        port=meter.port,
        baud=4800,
        framing=Framing(8, Parity.NONE, 1),
        flow_control=FlowControl.NONE,
        terminator=Terminator.CRLF,
    )
    transport = open_serial_transport("@py", settings)
    try:
        assert isinstance(transport, RemoteControl)
        assert isinstance(transport, LocalControl)
        transport.timeout = TIMEOUT_S
        transport.go_to_remote()

        identity = Driver(transport).identify()

        transport.go_to_local()
        meter.wait_for(3)
    finally:
        transport.close()

    assert identity.model == "34401A"
    assert meter.lines == ["SYST:REM", "*IDN?", "SYST:LOC"]


def test_probe_finds_a_meter_through_the_real_pyvisa_py_stack(meter):
    result = run_probe(lambda settings: open_serial_transport("@py", settings), SerialSettings(port=meter.port))

    assert result.found == SerialSettings(port=meter.port)  # the pseudo-terminal answers at the first settings tried
    assert result.identity is not None
    assert result.identity.model == "34401A"
    meter.wait_for(3)
    assert meter.lines == ["SYST:REM", "*IDN?", "SYST:LOC"]


def test_probe_skips_the_framings_a_port_refuses_and_reports_silence_when_nothing_answers():
    silent = FakeMeterOnAPty()
    silent.answer = False
    try:
        result = run_probe(
            lambda settings: open_serial_transport("@py", settings),
            SerialSettings(port=silent.port),
            timeout_for=lambda _baud: 0.05,
        )
    finally:
        silent.close()

    assert result.found is None
    assert result.tried == result.total == 18
    assert "null-modem" in result.message


@pytest.mark.parametrize("flow", list(FlowControl))
def test_pyvisa_py_accepts_every_flow_control_the_dialog_offers(meter, flow):
    # A pseudo-terminal refuses 7-bit and parity Framings (EINVAL, as it does for plain pyserial), so only 8N1 can be
    # opened here; the other Framings are covered by checking what is set on the VISA resource.
    settings = SerialSettings(port=meter.port, baud=300, flow_control=flow)

    open_serial_transport("@py", settings).close()
