import queue
from datetime import datetime, timedelta, timezone

from agilent34401a.sim import Simulator
from agilent34401a.worker import Event, ReadingTaken, Worker
from tests.test_worker import next_event


def next_reading(events: "queue.Queue[Event]") -> ReadingTaken:
    while True:
        event = next_event(events)
        if isinstance(event, ReadingTaken):
            return event


START = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def test_each_reading_carries_the_wall_clock_time_it_was_taken_from_the_workers_clock():
    ticks = iter(START + timedelta(seconds=n) for n in range(1000))
    events: queue.Queue[Event] = queue.Queue()
    worker = Worker(Simulator, events, wall_clock=lambda: next(ticks))
    worker.start()
    try:
        worker.start_continuous()
        first, second = next_reading(events), next_reading(events)
    finally:
        worker.shutdown()

    assert second.taken_at - first.taken_at == timedelta(seconds=1)
    assert first.taken_at >= START


def test_by_default_a_readings_time_is_the_local_wall_clock_time_with_its_offset():
    events: queue.Queue[Event] = queue.Queue()
    worker = Worker(Simulator, events)
    worker.start()
    try:
        worker.start_continuous()
        taken = next_reading(events).taken_at
    finally:
        worker.shutdown()

    assert taken.utcoffset() is not None
    assert abs(taken - datetime.now(timezone.utc)) < timedelta(seconds=5)
