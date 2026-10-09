"""The Worker's Meter Memory requests: stored and recalled only when asked, and every outcome reported."""

import queue

import pytest

from agilent34401a.driver import QueuedError
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import Simulator
from agilent34401a.worker import (
    Connected,
    Disconnected,
    ErrorsReported,
    Event,
    MeterMemoryStored,
    ReadingTaken,
    SetupChanged,
    Worker,
)

TIMEOUT_S = 5.0


class WatchedSimulator(Simulator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)
        super().write(command)


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


@pytest.fixture
def started():
    workers = []

    def start(simulator):
        events: queue.Queue[Event] = queue.Queue()
        worker = Worker(lambda: simulator, events)
        worker.start()
        workers.append(worker)
        assert isinstance(next_event(events), Connected)
        return worker, events

    yield start
    for worker in workers:
        worker.shutdown()


def test_nothing_is_stored_or_recalled_on_connect_or_disconnect(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)

    worker.disconnect()
    while not isinstance(next_event(events), Disconnected):
        pass

    assert not [command for command in simulator.writes if command.startswith(("*SAV", "*RCL"))]


def test_a_store_is_acknowledged_and_changes_nothing_on_the_meter(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)

    worker.save_to_meter(2)

    assert next_event(events) == MeterMemoryStored(2)
    assert "*SAV 2" in simulator.writes


def test_a_recall_reports_the_setup_the_meter_is_actually_in(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)
    stored = Setup.default(Function.DC_VOLTAGE).with_range(10.0).with_nplc(1)
    worker.apply_setup(stored)
    assert next_event(events) == SetupChanged(stored)
    worker.save_to_meter(1)
    assert next_event(events) == MeterMemoryStored(1)
    worker.apply_setup(Setup.default(Function.RESISTANCE_2W))
    assert isinstance(next_event(events), SetupChanged)

    worker.recall_from_meter(1)

    changed = next_event(events)
    assert changed == SetupChanged(stored)


def test_a_recall_of_a_location_never_stored_reports_the_error_and_the_setup_unchanged(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)

    worker.recall_from_meter(3)

    assert next_event(events) == SetupChanged(Setup.default(Function.DC_VOLTAGE))
    assert next_event(events) == ErrorsReported((QueuedError(-314, "Save/recall memory lost"),))


def test_readings_follow_the_recalled_setup(started):
    simulator = WatchedSimulator(signals={Function.RESISTANCE_2W: 470.0})
    worker, events = started(simulator)
    worker.apply_setup(Setup.default(Function.RESISTANCE_2W))
    next_event(events)
    worker.save_to_meter(1)
    next_event(events)
    worker.select_function(Function.DC_VOLTAGE)
    next_event(events)

    worker.recall_from_meter(1)
    assert isinstance(next_event(events), SetupChanged)
    worker.single()

    reading = next_event(events)
    assert isinstance(reading, ReadingTaken)
    assert reading.reading.function is Function.RESISTANCE_2W


@pytest.mark.parametrize("location", [0, 4, -1])
def test_a_location_the_meter_cannot_store_to_is_refused_before_a_request_is_made(started, location):
    worker, _events = started(WatchedSimulator())

    with pytest.raises(ValueError, match="location"):
        worker.save_to_meter(location)


@pytest.mark.parametrize("location", [4, -1])
def test_a_location_the_meter_cannot_recall_is_refused_before_a_request_is_made(started, location):
    worker, _events = started(WatchedSimulator())

    with pytest.raises(ValueError, match="location"):
        worker.recall_from_meter(location)
