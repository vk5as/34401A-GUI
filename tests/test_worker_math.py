"""The Worker and Math Operations: Readings carry their Operation and Limit Test result, and Statistics are reported."""

import queue

import pytest

from agilent34401a.math_operations import LimitResult, MathOperation, MathSettings, MeterStatistics
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import Simulator
from agilent34401a.worker import (
    AdminFailed,
    Connected,
    Event,
    ReadingFailed,
    ReadingTaken,
    SetupChanged,
    StatisticsRead,
    Worker,
)

TIMEOUT_S = 5.0


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


def next_of(events: "queue.Queue[Event]", kind: type[Event]) -> Event:
    """Return the next event of type `kind`, skipping the others."""
    while True:
        event = next_event(events)
        if isinstance(event, kind):
            return event


@pytest.fixture
def started():
    workers = []

    def start(transport):
        events: queue.Queue[Event] = queue.Queue()
        worker = Worker(lambda: transport, events)
        worker.start()
        workers.append(worker)
        return worker, events

    yield start
    for worker in workers:
        worker.shutdown()


def setup_with(operation: MathOperation | None, **settings: float) -> Setup:
    return Setup.default(Function.DC_VOLTAGE).with_math(MathSettings(operation=operation, **settings))


class MalformedStatistics(Simulator):
    """A Simulator whose Statistics count comes back as nonsense."""

    _asked_for_count = False

    def read(self) -> str:
        reply = super().read()
        return "many" if self._asked_for_count else reply

    def write(self, command: str) -> None:
        self._asked_for_count = command == "CALC:AVER:COUN?"
        super().write(command)


def test_a_setup_with_a_math_operation_is_applied_and_reported_back(started):
    worker, events = started(Simulator())
    next_event(events)
    setup = setup_with(MathOperation.DBM, dbm_reference_resistance=50)

    worker.apply_setup(setup)

    assert next_event(events) == SetupChanged(setup)


def test_connecting_reports_the_math_operation_the_meter_was_already_doing(started):
    simulator = Simulator()
    simulator.write("CALC:NULL:OFFS 0.5")
    simulator.write("CALC:FUNC NULL")
    simulator.write("CALC:STAT ON")
    _worker, events = started(simulator)

    connected = next_event(events)

    assert isinstance(connected, Connected)
    assert connected.setup.math == MathSettings(operation=MathOperation.NULL, null_offset=0.5)


def test_readings_carry_the_math_operation_they_were_taken_under(started):
    worker, events = started(Simulator(signals={Function.DC_VOLTAGE: 1.0}))
    next_event(events)
    setup = setup_with(MathOperation.NULL, null_offset=0.25)
    worker.apply_setup(setup)
    next_event(events)

    worker.start_continuous()

    taken = next_of(events, ReadingTaken)
    assert isinstance(taken, ReadingTaken)
    assert taken.setup == setup
    assert taken.reading.math is MathOperation.NULL
    assert taken.reading.value == pytest.approx(0.75)


def test_readings_in_a_limit_test_carry_their_result(started):
    worker, events = started(Simulator(signals={Function.DC_VOLTAGE: 1.5}))
    next_event(events)
    worker.apply_setup(setup_with(MathOperation.LIMIT_TEST, limit_lower=-1, limit_upper=1))
    next_event(events)

    worker.start_continuous()

    taken = next_of(events, ReadingTaken)
    assert isinstance(taken, ReadingTaken)
    assert taken.reading.limit is LimitResult.HIGH


def test_readings_without_a_math_operation_carry_neither(started):
    worker, events = started(Simulator())
    next_event(events)

    worker.start_continuous()

    taken = next_of(events, ReadingTaken)
    assert isinstance(taken, ReadingTaken)
    assert (taken.reading.math, taken.reading.limit) == (None, None)


def test_statistics_follow_every_reading_while_they_are_the_operation_in_effect(started):
    worker, events = started(Simulator(signals={Function.DC_VOLTAGE: 2.0}))
    next_event(events)
    worker.apply_setup(setup_with(MathOperation.STATISTICS))
    next_event(events)

    worker.start_continuous()

    counts = []
    for _ in range(3):
        assert isinstance(next_event(events), ReadingTaken)
        statistics = next_event(events)
        assert isinstance(statistics, StatisticsRead)
        counts.append(statistics.statistics.count)
    assert counts == [1, 2, 3]
    assert isinstance(statistics, StatisticsRead)
    assert statistics.statistics == MeterStatistics(minimum=2.0, maximum=2.0, average=2.0, count=3)


def test_no_statistics_are_reported_for_other_operations(started):
    worker, events = started(Simulator())
    next_event(events)
    worker.apply_setup(setup_with(MathOperation.NULL))
    next_event(events)

    worker.start_continuous()

    assert isinstance(next_event(events), ReadingTaken)
    assert isinstance(next_event(events), ReadingTaken)


def test_the_statistics_can_be_asked_for(started):
    simulator = Simulator(signals={Function.DC_VOLTAGE: 4.0})
    for command in ("CALC:FUNC AVER", "CALC:STAT ON"):
        simulator.write(command)
    simulator.query("READ?")
    simulator.query("READ?")
    worker, events = started(simulator)
    next_event(events)

    worker.read_statistics()

    assert next_event(events) == StatisticsRead(MeterStatistics(minimum=4.0, maximum=4.0, average=4.0, count=2))


def test_the_statistics_can_be_reset(started):
    simulator = Simulator()
    for command in ("CALC:FUNC AVER", "CALC:STAT ON"):
        simulator.write(command)
    simulator.query("READ?")
    worker, events = started(simulator)
    next_event(events)

    worker.reset_statistics()

    assert next_event(events) == StatisticsRead(MeterStatistics(minimum=0, maximum=0, average=0, count=0))
    worker.read_statistics()
    assert next_event(events) == StatisticsRead(MeterStatistics(minimum=0, maximum=0, average=0, count=0))
    assert simulator.query("CALC:STAT?") == "1"


def test_resetting_the_statistics_while_they_are_not_in_effect_is_reported(started):
    worker, events = started(Simulator())
    next_event(events)

    worker.reset_statistics()

    failed = next_event(events)
    assert isinstance(failed, AdminFailed)
    assert "Statistics" in failed.message


def test_statistics_that_cannot_be_read_lose_a_reading_but_not_the_connection(started):
    simulator = MalformedStatistics()
    worker, events = started(simulator)
    next_event(events)
    worker.apply_setup(setup_with(MathOperation.STATISTICS))
    next_event(events)

    worker.start_continuous()

    assert isinstance(next_event(events), ReadingTaken)
    failed = next_event(events)
    assert isinstance(failed, ReadingFailed)
    assert "Statistics" in failed.message
    assert isinstance(next_event(events), ReadingTaken)  # Continuous goes on
