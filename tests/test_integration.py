"""The real pyvisa-py stack, talking over TCP to the socket Simulator (ADR-0003)."""

import queue
from collections.abc import Callable, Iterator
from typing import TypeVar

import pytest

from agilent34401a.backend import Backend, detect_backends
from agilent34401a.cli import main
from agilent34401a.connection import ConnectionSettings, open_transport
from agilent34401a.driver import Driver
from agilent34401a.errors import BackendUnavailableError, TransportError, TransportTimeoutError
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import AGILENT_IDENTITY, Simulator
from agilent34401a.sim_server import SimulatorServer
from agilent34401a.transport import Transport
from agilent34401a.trigger import TriggerSettings, TriggerSource
from agilent34401a.visa import check_library
from agilent34401a.worker import BurstFailed, BurstFinished, Connected, Disconnected, Event, ReadingTaken, Worker

TIMEOUT_S = 10.0


@pytest.fixture
def server() -> Iterator[SimulatorServer]:
    with SimulatorServer(port=0) as running:
        yield running


@pytest.fixture
def open_meter(server: SimulatorServer) -> Iterator[Callable[[], Transport]]:
    opened: list[Transport] = []

    def open_it() -> Transport:
        transport = open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name))
        opened.append(transport)
        return transport

    yield open_it
    for transport in opened:
        transport.close()


def test_the_driver_identifies_the_simulator_through_pyvisa_py(open_meter):
    identity = Driver(open_meter()).identify()

    assert identity.model == "34401A"
    assert identity.manufacturer == "HEWLETT-PACKARD"


def test_the_agilent_firmware_identity_is_accepted_too():
    with SimulatorServer(Simulator(identity=AGILENT_IDENTITY), port=0) as server:
        transport = open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name))
        try:
            assert Driver(transport).identify().manufacturer == "Agilent Technologies"
        finally:
            transport.close()


def test_readings_come_back_with_their_raw_reading(open_meter):
    driver = Driver(open_meter())

    reading = driver.read()

    assert reading.value == pytest.approx(1.0)
    assert reading.raw == "+1.00000000E+00"


def test_a_setup_can_be_applied_and_read_back_over_the_wire(open_meter):
    driver = Driver(open_meter())
    wanted = Setup.default(Function.RESISTANCE_4W).with_range(1e4).with_nplc(1)

    assert driver.apply(wanted) == []

    assert driver.read_setup() == wanted
    assert driver.read().function is Function.RESISTANCE_4W


def test_meter_errors_come_back_over_the_wire(open_meter):
    transport = open_meter()
    transport.write("BOGUS")

    assert Driver(transport).drain_errors()[0].code == -113


def test_the_timeout_is_set_in_seconds_and_a_slow_reading_exceeds_it():
    with SimulatorServer(Simulator(time_scale=1), port=0) as server:  # 0.4 s per Reading
        transport = open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name))
        try:
            transport.timeout = 0.1
            assert transport.timeout == pytest.approx(0.1)

            with pytest.raises(TransportTimeoutError):
                transport.query("READ?")
        finally:
            transport.close()


def test_a_reading_that_fits_the_timeout_arrives():
    with SimulatorServer(Simulator(time_scale=1), port=0) as server:
        transport = open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name))
        try:
            transport.timeout = 5.0

            assert float(transport.query("READ?")) == pytest.approx(1.0)
        finally:
            transport.close()


def test_a_closed_connection_refuses_further_use(open_meter):
    transport = open_meter()
    transport.close()

    with pytest.raises(TransportError, match="closed"):
        transport.query("*IDN?")


def test_a_server_that_goes_away_is_reported_as_a_transport_error():
    server = SimulatorServer(port=0)
    server.start()
    try:
        transport = open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name))
        try:
            transport.timeout = 0.3
            assert transport.query("*IDN?")
            server.stop()

            # pyvisa-py cannot tell a dropped socket from a silent Meter, so this surfaces as a timeout.
            with pytest.raises(TransportError):
                transport.query("*IDN?")
        finally:
            transport.close()
    finally:
        server.stop()


def _open_and_query(resource_name: str) -> None:
    transport = open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=resource_name))
    try:
        transport.query("*IDN?")
    finally:
        transport.close()


def test_a_port_nobody_listens_on_is_reported_as_a_transport_error(unused_port):
    # pyvisa-py connects lazily on Linux and eagerly on Windows, so either opening or the first command may raise.
    with pytest.raises(TransportError):
        _open_and_query(f"TCPIP::127.0.0.1::{unused_port}::SOCKET")


def test_pyvisa_py_is_detected_and_auto_uses_it_when_vendor_visa_is_missing(server):
    statuses = detect_backends(check_library)
    assert statuses[Backend.PYVISA_PY].available is True
    if statuses[Backend.VENDOR].available:
        pytest.skip("this machine has a vendor VISA library installed")
    assert statuses[Backend.VENDOR].reason

    transport = open_transport(ConnectionSettings(resource=server.resource_name))  # Backend.AUTO
    try:
        assert Driver(transport).identify().model == "34401A"
    finally:
        transport.close()


def test_choosing_vendor_visa_without_it_installed_explains_why(server):
    if detect_backends(check_library)[Backend.VENDOR].available:
        pytest.skip("this machine has a vendor VISA library installed")

    with pytest.raises(BackendUnavailableError, match="Keysight IO Libraries"):
        open_transport(ConnectionSettings(backend=Backend.VENDOR, resource=server.resource_name))


def test_the_worker_runs_over_the_real_stack_and_closes_the_connection(server):
    events: queue.Queue[Event] = queue.Queue()
    worker = Worker(
        lambda: open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name)), events
    )
    worker.start()
    try:
        connected = events.get(timeout=TIMEOUT_S)
        assert isinstance(connected, Connected)
        assert connected.setup == Setup.default(Function.DC_VOLTAGE)

        worker.start_continuous()
        reading = events.get(timeout=TIMEOUT_S)
        assert isinstance(reading, ReadingTaken)
        assert reading.reading.value == pytest.approx(1.0)
    finally:
        assert worker.shutdown() is True

    remaining = []
    while not events.empty():
        remaining.append(events.get_nowait())
    assert isinstance(remaining[-1], Disconnected)


def test_the_cli_reads_a_reading_over_the_real_stack(server, capsys):
    assert main(["read", "--backend", "py", "--resource", server.resource_name]) == 0

    assert capsys.readouterr().out == "1.000000 V\n"


def test_the_cli_changes_function_and_range_over_the_real_stack(server, capsys):
    assert (
        main(["read", "--backend", "py", "--resource", server.resource_name, "--function", "res", "--range", "1000"])
        == 0
    )

    assert capsys.readouterr().out == "1.000000 kΩ\n"


def test_a_setup_saved_with_the_cli_is_still_in_the_meter_for_a_later_recall_over_the_real_stack(server, capsys):
    connection = ["--backend", "py", "--resource", server.resource_name]
    assert main(["read", *connection, "--function", "res", "--range", "1000"]) == 0
    assert main(["save", "2", *connection]) == 0
    assert main(["read", *connection, "--function", "dcv", "--range", "10"]) == 0
    capsys.readouterr()

    assert main(["recall", "2", *connection]) == 0

    out = capsys.readouterr().out
    assert out.startswith("Recalled Meter Memory location 2: 2-wire Ω")
    assert "1 kΩ range" in out


def test_the_cli_reports_a_meter_that_refuses_a_setting_over_the_real_stack(server, capsys):
    assert (
        main(["read", "--backend", "py", "--resource", server.resource_name, "--function", "dcv", "--range", "5000"])
        == 2
    )

    assert "Range" in capsys.readouterr().err


def test_the_cli_reports_a_connection_that_cannot_be_made(unused_port, capsys):
    assert main(["read", "--backend", "py", "--resource", f"TCPIP::127.0.0.1::{unused_port}::SOCKET"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("agilent34401a-cli: error:")


def test_a_burst_runs_over_the_wire_and_a_raw_socket_cannot_wait_for_an_external_trigger(open_meter):
    events: queue.Queue[Event] = queue.Queue()
    transport = open_meter()
    worker = Worker(lambda: transport, events)
    worker.start()
    try:
        connected = events.get(timeout=TIMEOUT_S)
        assert isinstance(connected, Connected)
        assert not connected.supports_device_clear  # the simulator's server is a raw socket

        worker.start_burst(TriggerSettings(sample_count=20))
        finished = _until(events, BurstFinished)
        assert len(finished.readings) == 20
        assert finished.readings[0].reading.value == pytest.approx(1.0)

        worker.start_burst(TriggerSettings(source=TriggerSource.EXTERNAL, sample_count=2))
        assert "device clear" in _until(events, BurstFailed).message
    finally:
        assert worker.shutdown()


_EventT = TypeVar("_EventT")


def _until(events: "queue.Queue[Event]", kind: type[_EventT]) -> _EventT:
    while True:
        event = events.get(timeout=TIMEOUT_S)
        if isinstance(event, kind):
            return event
