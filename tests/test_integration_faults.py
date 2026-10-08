"""The Worker through the real pyvisa-py stack against a socket Simulator that misbehaves (ADR-0003)."""

import queue
from collections.abc import Callable, Iterator

import pytest

from agilent34401a.backend import Backend
from agilent34401a.connection import ConnectionSettings, open_transport
from agilent34401a.meter import Function
from agilent34401a.sim_faults import Fault
from agilent34401a.sim_server import SimulatorServer
from agilent34401a.transport import Transport
from agilent34401a.worker import (
    Connected,
    ConnectionLost,
    Disconnected,
    Event,
    RawReplied,
    ReadingFailed,
    ReadingTaken,
    SetupChanged,
    SetupFailed,
    Worker,
)

TIMEOUT_S = 15.0
READING_TIMEOUT_S = 0.3


@pytest.fixture
def server() -> Iterator[SimulatorServer]:
    with SimulatorServer(port=0) as running:
        yield running


@pytest.fixture
def start_worker(server: SimulatorServer) -> Iterator[Callable[[], tuple[Worker, "queue.Queue[Event]"]]]:
    """A Worker connected through pyvisa-py to the server, waiting no longer than a third of a second for a Reading."""
    workers: list[Worker] = []

    def opener() -> Transport:
        return open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name))

    def start() -> tuple[Worker, queue.Queue[Event]]:
        events: queue.Queue[Event] = queue.Queue()
        worker = Worker(opener, events, reading_timeout=lambda _setup: READING_TIMEOUT_S)
        worker.start()
        workers.append(worker)
        assert isinstance(events.get(timeout=TIMEOUT_S), Connected)
        return worker, events

    yield start
    for worker in workers:
        assert worker.shutdown() is True


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


def collect_until(events: "queue.Queue[Event]", stop: Callable[[Event], bool]) -> list[Event]:
    collected = []
    while True:
        event = next_event(events)
        collected.append(event)
        if stop(event):
            return collected


def is_a(kind: type) -> Callable[[Event], bool]:
    return lambda event: isinstance(event, kind)


def first_of(kind: type, events: "queue.Queue[Event]") -> Event:
    return collect_until(events, is_a(kind))[-1]


def assert_still_in_step(worker: Worker, events: "queue.Queue[Event]") -> None:
    """Ask the Meter something only its own reply answers: a stale Reading in its place would be wrong."""
    worker.pause()
    worker.send_raw("FUNC?")
    replied = first_of(RawReplied, events)
    assert isinstance(replied, RawReplied)
    assert replied.reply == '"VOLT"'


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(Fault.slow(0.6, command="READ"), id="slow"),
        pytest.param(Fault.noise(seed=3, command="READ"), id="noise"),
        pytest.param(Fault.unterminated(command="READ"), id="unterminated"),
        pytest.param(Fault.wrong_type(command="READ"), id="wrong-type"),
    ],
)
def test_a_bad_reading_is_lost_and_the_connection_goes_on_in_step(start_worker, server, fault):
    worker, events = start_worker()
    server.inject(fault)

    worker.start_continuous()
    failed = first_of(ReadingFailed, events)
    taken = first_of(ReadingTaken, events)

    assert isinstance(failed, ReadingFailed)
    assert isinstance(taken, ReadingTaken)
    assert taken.reading.value == pytest.approx(1.0)
    assert_still_in_step(worker, events)


def test_a_truncated_reply_to_a_setup_check_fails_the_change_and_the_connection_goes_on(start_worker, server):
    worker, events = start_worker()
    server.inject(Fault.truncated(command=r"^FUNC\?"))

    worker.select_function(Function.DC_VOLTAGE)
    failed = first_of(SetupFailed, events)
    worker.select_function(Function.DC_VOLTAGE)
    changed = first_of(SetupChanged, events)

    assert isinstance(failed, SetupFailed)
    assert isinstance(changed, SetupChanged)
    assert_still_in_step(worker, events)


@pytest.mark.parametrize("fault_kind", [Fault.drop, Fault.reset])
def test_a_dropped_connection_is_reported_lost_once_and_the_user_can_reconnect(start_worker, server, fault_kind):
    worker, events = start_worker()
    server.inject(fault_kind(command="READ"))

    worker.start_continuous()
    seen = collect_until(events, is_a(Disconnected))

    assert sum(isinstance(event, ConnectionLost) for event in seen) == 1
    assert worker.is_alive()
    worker.connect(lambda: open_transport(ConnectionSettings(backend=Backend.PYVISA_PY, resource=server.resource_name)))
    assert isinstance(next_event(events), Connected)
    worker.start_continuous()
    assert isinstance(first_of(ReadingTaken, events), ReadingTaken)


def test_a_meter_that_is_always_too_slow_ends_in_a_lost_connection(start_worker, server):
    worker, events = start_worker()
    server.inject(Fault.slow(5.0, command="READ", times=None))

    worker.start_continuous()
    seen = collect_until(events, is_a(Disconnected))

    assert [type(event) for event in seen if not isinstance(event, ReadingFailed)] == [ConnectionLost, Disconnected]
    assert worker.is_alive()


def test_a_run_of_garbage_ends_in_a_lost_connection(start_worker, server):
    worker, events = start_worker()
    server.inject(Fault.noise(command="READ", times=None))

    worker.start_continuous()
    seen = collect_until(events, is_a(Disconnected))

    lost = [event for event in seen if isinstance(event, ConnectionLost)]
    assert len(lost) == 1
    assert "in a row" in lost[0].message
    assert worker.is_alive()
