import queue
import threading
import time
from collections.abc import Callable

import pytest

from agilent34401a.errors import TransportError
from agilent34401a.sim import AGILENT_IDENTITY, Simulator
from agilent34401a.worker import (
    Connected,
    ConnectionFailed,
    Disconnected,
    Event,
    ReadingFailed,
    ReadingTaken,
    Worker,
    WorkerFailed,
)

TIMEOUT_S = 5.0


class HookedSimulator(Simulator):
    """A Simulator that lets a test observe, and interfere with, each Reading the worker takes."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads = 0
        self.closed = False
        self.clears = 0
        self.before_reading: Callable[[int], None] = lambda _count: None
        self.garbage_on: set[int] = set()

    def query(self, command: str) -> str:
        if command == "READ?":
            self.reads += 1
            self.before_reading(self.reads)
            if self.reads in self.garbage_on:
                return "not a number"
        return super().query(command)

    def clear(self) -> None:
        self.clears += 1
        super().clear()

    def close(self) -> None:
        self.closed = True
        super().close()


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


def next_reading(events: "queue.Queue[Event]") -> ReadingTaken:
    event = next_event(events)
    assert isinstance(event, ReadingTaken)
    return event


def collect_until(events: "queue.Queue[Event]", stop: Callable[[Event], bool]) -> list[Event]:
    collected = []
    while True:
        event = next_event(events)
        collected.append(event)
        if stop(event):
            return collected


def start_worker(transport) -> tuple[Worker, "queue.Queue[Event]"]:
    events: queue.Queue[Event] = queue.Queue()
    worker = Worker(lambda: transport, events)
    worker.start()
    return worker, events


@pytest.fixture
def started():
    workers = []

    def start(transport):
        worker, events = start_worker(transport)
        workers.append(worker)
        return worker, events

    yield start
    for worker in workers:
        worker.shutdown()


def test_worker_identifies_the_meter_on_connect_and_waits_for_a_request_before_reading(started):
    simulator = HookedSimulator(identity=AGILENT_IDENTITY)
    _worker, events = started(simulator)

    event = next_event(events)

    assert isinstance(event, Connected)
    assert event.identity.manufacturer == "Agilent Technologies"
    time.sleep(0.05)
    assert events.empty()
    assert simulator.reads == 0


def test_continuous_delivers_readings_with_their_raw_reading(started):
    worker, events = started(HookedSimulator(dc_voltage=1.5))
    assert isinstance(next_event(events), Connected)

    worker.start_continuous()

    readings = [next_reading(events) for _ in range(3)]
    assert readings[0].reading.value == pytest.approx(1.5)
    assert readings[0].reading.raw == "+1.50000000E+00"
    assert readings[0].timestamp <= readings[1].timestamp <= readings[2].timestamp


def test_pause_stops_continuous_and_it_can_be_resumed(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    # Pausing from inside the third Reading is the only race-free way to know exactly where it stops.
    simulator.before_reading = lambda count: worker.pause() if count == 3 else None
    next_event(events)

    worker.start_continuous()
    for _ in range(3):
        assert isinstance(next_event(events), ReadingTaken)
    time.sleep(0.1)

    assert simulator.reads == 3
    assert events.empty()

    worker.start_continuous()

    assert isinstance(next_event(events), ReadingTaken)
    assert simulator.reads > 3


def test_worker_does_not_flood_events_nobody_is_draining(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)

    worker.start_continuous()
    time.sleep(0.3)

    assert events.qsize() < 500
    assert simulator.reads < 500


def test_shutdown_closes_the_transport_and_reports_disconnected(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)

    assert worker.shutdown() is True

    assert simulator.closed is True
    assert isinstance(next_event(events), Disconnected)


def test_shutdown_while_continuous_is_running_stops_cleanly(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)
    worker.start_continuous()
    next_event(events)

    assert worker.shutdown() is True

    assert simulator.closed is True
    remaining = []
    while not events.empty():
        remaining.append(events.get_nowait())
    assert isinstance(remaining[-1], Disconnected)


def test_shutdown_reports_failure_when_a_reading_will_not_finish(started):
    simulator = HookedSimulator()
    release = threading.Event()
    worker, events = started(simulator)

    def block(_count: int) -> None:
        release.wait(TIMEOUT_S)

    simulator.before_reading = block
    next_event(events)
    worker.start_continuous()
    deadline = time.monotonic() + TIMEOUT_S
    while simulator.reads == 0 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert worker.shutdown(timeout=0.05) is False

    release.set()
    assert worker.shutdown() is True


def test_shutdown_is_harmless_when_repeated(started):
    worker, events = started(HookedSimulator())
    next_event(events)

    assert worker.shutdown() is True
    assert worker.shutdown() is True
    assert worker.is_alive() is False


def test_shutdown_before_start_does_not_poison_a_later_start():
    simulator = HookedSimulator()
    events: queue.Queue[Event] = queue.Queue()
    worker = Worker(lambda: simulator, events)

    assert worker.shutdown() is True
    assert simulator.closed is False

    worker.start()

    assert isinstance(next_event(events), Connected)
    assert worker.shutdown() is True


def test_a_device_that_is_not_a_34401a_fails_the_connection_and_is_closed(started):
    simulator = HookedSimulator(identity="Rigol Technologies,DM3058,DM3O123456789,01.01")
    _worker, events = started(simulator)

    failed = next_event(events)

    assert isinstance(failed, ConnectionFailed)
    assert "Rigol Technologies" in failed.message
    assert isinstance(next_event(events), Disconnected)
    assert simulator.closed is True


def test_a_transport_that_cannot_be_opened_fails_the_connection():
    def refuse() -> Simulator:
        message = "no such resource"
        raise TransportError(message)

    events: queue.Queue[Event] = queue.Queue()
    worker = Worker(refuse, events)
    worker.start()

    assert next_event(events) == ConnectionFailed("no such resource")
    assert isinstance(next_event(events), Disconnected)
    assert worker.shutdown() is True


def test_an_unexpected_error_while_opening_is_reported_not_swallowed():
    def explode() -> Simulator:
        message = "boom"
        raise RuntimeError(message)

    events: queue.Queue[Event] = queue.Queue()
    worker = Worker(explode, events)
    worker.start()

    failed = next_event(events)

    assert isinstance(failed, WorkerFailed)
    assert "RuntimeError" in failed.message
    assert "boom" in failed.message
    assert isinstance(next_event(events), Disconnected)


def test_a_malformed_reply_is_reported_and_the_connection_resynchronised_without_killing_the_worker(started, caplog):
    simulator = HookedSimulator()
    simulator.garbage_on = {2}
    worker, events = started(simulator)
    next_event(events)

    worker.start_continuous()
    seen = [next_event(events) for _ in range(3)]

    kinds = [type(event) for event in seen]
    assert kinds == [ReadingTaken, ReadingFailed, ReadingTaken]
    failure = seen[1]
    assert isinstance(failure, ReadingFailed)
    assert "not a number" in failure.message
    assert simulator.clears == 1
    assert "not a number" in caplog.text


def test_a_reading_that_times_out_is_reported_and_the_worker_carries_on(started):
    simulator = HookedSimulator(time_scale=1, sleep=lambda _seconds: None)
    simulator.timeout = 0.1
    worker, events = started(simulator)
    next_event(events)

    worker.start_continuous()
    first, second = next_event(events), next_event(events)

    assert isinstance(first, ReadingFailed)
    assert isinstance(second, ReadingFailed)
    assert simulator.clears >= 1


def test_an_unexpected_error_while_reading_ends_the_worker_loudly(started, caplog):
    simulator = HookedSimulator()

    def explode(_count):
        message = "boom"
        raise RuntimeError(message)

    simulator.before_reading = explode
    worker, events = started(simulator)
    next_event(events)

    worker.start_continuous()
    seen = collect_until(events, lambda event: isinstance(event, Disconnected))

    assert isinstance(seen[0], WorkerFailed)
    assert "boom" in seen[0].message
    assert "RuntimeError" in caplog.text
    assert simulator.closed is True
    assert worker.shutdown() is True


def test_a_transport_failure_while_reading_ends_the_worker_loudly(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)
    simulator.close()

    worker.start_continuous()
    seen = collect_until(events, lambda event: isinstance(event, Disconnected))

    assert isinstance(seen[0], WorkerFailed)
