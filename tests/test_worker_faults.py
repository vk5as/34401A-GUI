"""The Worker keeps its head when the Meter or the Connection misbehaves (ADR-0002)."""

import queue
from collections.abc import Callable, Iterator

import pytest

from agilent34401a.errors import TransportError, TransportTimeoutError
from agilent34401a.math_operations import MathOperation, MathSettings
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import Simulator
from agilent34401a.trigger import TriggerSettings
from agilent34401a.worker import (
    BurstFailed,
    BurstFinished,
    Connected,
    ConnectionLost,
    Disconnected,
    Event,
    ReadingFailed,
    ReadingTaken,
    SetupChanged,
    StatisticsRead,
    Worker,
    WorkerFailed,
)

TIMEOUT_S = 5.0
STALE = "+9.99000000E+01"


class FaultySimulator(Simulator):
    """A Simulator that mishandles the queries a test picks, as a Connection that has no device clear would see it."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.on_query: Callable[[str, int], str | None] = lambda _command, _count: None
        self.queries: list[str] = []
        self.sent: list[str] = []  # everything written, which includes the sentinel a resynchronisation sends
        self.clears = 0
        self.closed_by_worker = False
        self.silent = False  # the Meter takes in everything and answers nothing
        self.write_error: Exception | None = None
        self.close_error: Exception | None = None
        self.local_error: Exception | None = None
        self._counts: dict[str, int] = {}

    def query(self, command: str) -> str:
        self.queries.append(command)
        count = self._counts[command] = self._counts.get(command, 0) + 1
        reply = self.on_query(command, count)
        return super().query(command) if reply is None else reply

    def write(self, command: str) -> None:
        self.sent.append(command)
        if self.write_error is not None:
            raise self.write_error
        if not self.silent:
            super().write(command)

    def clear(self) -> None:
        self.clears += 1  # and nothing is dropped: a socket has no device clear, so stale replies stay

    def go_to_local(self) -> None:
        if self.local_error is not None:
            raise self.local_error
        super().go_to_local()

    def close(self) -> None:
        self.closed_by_worker = True
        super().close()
        if self.close_error is not None:
            raise self.close_error

    def late_reply(self, reply: str) -> None:
        """A reply that arrives after the client gave up waiting for it."""
        self._reply(reply)


def resynchronised(simulator: FaultySimulator) -> bool:
    """Whether the Worker asked the sentinel again after connecting, which is what resynchronising does."""
    return simulator.sent.count("*IDN?") > 1


def timing_out(simulator: FaultySimulator, command: str, on: set[int], *, late: str | None = None) -> None:
    """Make the `on`-th `command` time out, leaving `late` behind as its reply if given."""

    def misbehave(asked: str, count: int) -> str | None:
        if asked == command and count in on:
            if late is not None:
                simulator.late_reply(late)
            message = "no reply"
            raise TransportTimeoutError(message)
        return None

    simulator.on_query = misbehave


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


def collect_until(events: "queue.Queue[Event]", stop: Callable[[Event], bool]) -> list[Event]:
    collected = []
    while True:
        event = next_event(events)
        collected.append(event)
        if stop(event):
            return collected


def ended(event: Event) -> bool:
    return isinstance(event, Disconnected)


@pytest.fixture
def idle_worker() -> Iterator[Callable[..., tuple[Worker, "queue.Queue[Event]"]]]:
    workers: list[Worker] = []

    def make(**options) -> tuple[Worker, "queue.Queue[Event]"]:
        events: queue.Queue[Event] = queue.Queue()
        worker = Worker(None, events, **options)
        worker.start()
        workers.append(worker)
        return worker, events

    yield make
    for worker in workers:
        worker.shutdown()


def connect(worker: Worker, events, simulator: Simulator) -> None:
    worker.connect(lambda: simulator)
    assert isinstance(next_event(events), Connected)


def test_a_late_reply_to_a_lost_reading_is_never_taken_for_the_next_one(idle_worker):
    worker, events = idle_worker()
    simulator = FaultySimulator()
    timing_out(simulator, "READ?", {2}, late=STALE)
    connect(worker, events, simulator)

    worker.start_continuous()
    seen = [next_event(events) for _ in range(4)]

    assert [type(event) for event in seen] == [ReadingTaken, ReadingFailed, ReadingTaken, ReadingTaken]
    assert [event.reading.value for event in seen if isinstance(event, ReadingTaken)] == [1.0] * 3
    assert "*IDN?" in simulator.queries  # the sentinel that found the end of the stale replies


def test_a_garbled_reply_is_logged_and_the_connection_resynchronised(idle_worker, caplog):
    worker, events = idle_worker()
    simulator = FaultySimulator()
    simulator.on_query = lambda command, count: "\x00garbage" if command == "READ?" and count == 1 else None
    connect(worker, events, simulator)

    worker.start_continuous()
    failed = next_event(events)

    assert isinstance(failed, ReadingFailed)
    assert isinstance(next_event(events), ReadingTaken)
    assert simulator.clears == 1
    assert "Lost a Reading" in caplog.text


@pytest.mark.parametrize("fault", ["silent", "broken"])
def test_a_connection_that_stopped_answering_is_reported_lost_once_and_the_worker_stays_alive(idle_worker, fault):
    worker, events = idle_worker()
    simulator = FaultySimulator()

    def go_dead(command: str, count: int) -> str | None:
        if command == "READ?" and count >= 2:
            message = "no reply"
            if fault == "broken":
                simulator.write_error = TransportError("The Connection to the Meter failed: broken pipe")
                raise simulator.write_error
            simulator.silent = True
            raise TransportTimeoutError(message)
        return None

    simulator.on_query = go_dead
    connect(worker, events, simulator)

    worker.start_continuous()
    seen = collect_until(events, ended)

    # A Meter that says nothing is first a lost Reading; a Connection that is broken outright is lost at once.
    lost_reading = [ReadingFailed] if fault == "silent" else []
    assert [type(event) for event in seen] == [ReadingTaken, *lost_reading, ConnectionLost, Disconnected]
    assert simulator.closed_by_worker is True
    assert worker.is_alive()
    connect(worker, events, FaultySimulator())  # and the user can reconnect


def test_a_run_of_bad_replies_ends_in_a_lost_connection(idle_worker):
    worker, events = idle_worker(max_consecutive_failures=3)
    simulator = FaultySimulator()
    simulator.on_query = lambda command, _count: "not a number" if command == "READ?" else None
    connect(worker, events, simulator)

    worker.start_continuous()
    seen = collect_until(events, ended)

    assert [type(event) for event in seen] == [ReadingFailed] * 3 + [ConnectionLost, Disconnected]
    lost = seen[3]
    assert isinstance(lost, ConnectionLost)
    assert "3" in lost.message
    assert worker.is_alive()


def test_good_replies_between_the_bad_ones_keep_the_connection_open(idle_worker):
    worker, events = idle_worker(max_consecutive_failures=2)
    simulator = FaultySimulator()
    simulator.on_query = lambda command, count: "not a number" if command == "READ?" and count % 2 == 1 else None
    connect(worker, events, simulator)

    worker.start_continuous()
    seen = [next_event(events) for _ in range(10)]

    assert not any(isinstance(event, ConnectionLost) for event in seen)
    assert sum(isinstance(event, ReadingTaken) for event in seen) == 5


def test_the_timeout_for_a_reading_can_be_chosen_by_whoever_starts_the_worker(idle_worker):
    worker, events = idle_worker(reading_timeout=lambda _setup: 0.25)
    simulator = FaultySimulator()

    connect(worker, events, simulator)

    assert simulator.timeout == 0.25


def test_a_transport_that_fails_to_close_does_not_stop_the_worker_saying_it_is_disconnected(idle_worker):
    worker, events = idle_worker()
    simulator = FaultySimulator()
    simulator.close_error = RuntimeError("close blew up")
    connect(worker, events, simulator)

    worker.disconnect()

    assert isinstance(next_event(events), Disconnected)
    assert worker.is_alive()
    connect(worker, events, FaultySimulator())


def test_a_transport_that_fails_to_return_to_local_does_not_stop_the_worker_either(idle_worker):
    worker, events = idle_worker()
    simulator = FaultySimulator()
    simulator.local_error = OSError("bus gone")
    connect(worker, events, simulator)

    worker.disconnect()

    assert isinstance(next_event(events), Disconnected)
    assert simulator.closed_by_worker is True
    assert worker.is_alive()


def test_an_unexpected_exception_anywhere_in_a_connection_is_a_fatal_event_not_a_silent_death(idle_worker, caplog):
    worker, events = idle_worker()
    simulator = FaultySimulator()

    def explode(command: str, _count: int) -> str | None:
        if command == "SYST:ERR?":
            message = "boom"
            raise RuntimeError(message)
        return None

    simulator.on_query = explode
    connect(worker, events, simulator)

    worker.check_errors()
    seen = collect_until(events, ended)

    failed = seen[0]
    assert isinstance(failed, WorkerFailed)
    assert "RuntimeError" in failed.message
    assert "boom" in failed.message
    assert "The Worker failed unexpectedly" in caplog.text
    assert worker.is_alive()
    connect(worker, events, FaultySimulator())


def test_even_a_bug_in_the_worker_itself_is_reported_before_its_thread_ends(idle_worker, monkeypatch):
    worker, events = idle_worker()

    def explode(_self, _open_transport):
        message = "bug"
        raise RuntimeError(message)

    monkeypatch.setattr(Worker, "_connection", explode)
    worker.connect(Simulator)

    seen = collect_until(events, ended)

    failed = seen[0]
    assert isinstance(failed, WorkerFailed)
    assert "bug" in failed.message
    assert worker.shutdown() is True
    assert worker.is_alive() is False


# --- the paths that used to clear the Connection without resynchronising it (R7) ------------------------------


def test_a_lost_statistics_reply_is_resynchronised_so_its_late_answer_is_not_taken_for_the_next(idle_worker):
    worker, events = idle_worker()
    simulator = FaultySimulator()
    timing_out(simulator, "CALC:AVER:MIN?", {1}, late=STALE)
    connect(worker, events, simulator)
    statistics = Setup.default(Function.DC_VOLTAGE).with_math(MathSettings(operation=MathOperation.STATISTICS))
    worker.apply_setup(statistics)
    assert isinstance(next_event(events), SetupChanged)

    worker.start_continuous()
    seen = collect_until(events, lambda event: isinstance(event, StatisticsRead))
    seen += [next_event(events) for _ in range(4)]

    assert any(isinstance(event, ReadingFailed) for event in seen)
    assert resynchronised(simulator)
    values = [event.reading.value for event in seen if isinstance(event, ReadingTaken)]
    assert values == [1.0] * len(values)  # never the stale 99.9 that was left on the line


def test_a_burst_that_could_not_be_started_resynchronises_the_connection(idle_worker):
    worker, events = idle_worker()
    simulator = FaultySimulator()
    timing_out(simulator, "SYST:ERR?", {1}, late=STALE)
    connect(worker, events, simulator)

    worker.start_burst(TriggerSettings(sample_count=3))
    failed = next_event(events)

    assert isinstance(failed, BurstFailed)
    assert resynchronised(simulator)


def test_a_burst_that_fails_while_it_runs_resynchronises_the_connection(idle_worker):
    worker, events = idle_worker()
    simulator = FaultySimulator()
    timing_out(simulator, "FETC?", {1}, late=STALE)
    connect(worker, events, simulator)

    worker.start_burst(TriggerSettings(sample_count=3))
    seen = collect_until(events, lambda event: isinstance(event, BurstFailed))

    assert isinstance(seen[-1], BurstFailed)
    assert resynchronised(simulator)


def test_failing_to_put_the_trigger_settings_back_after_a_burst_resynchronises_the_connection(idle_worker):
    worker, events = idle_worker()
    simulator = FaultySimulator()
    timing_out(simulator, "SYST:ERR?", {3}, late=STALE)  # the check after the Burst has been taken
    connect(worker, events, simulator)

    worker.start_burst(TriggerSettings(sample_count=3))
    collect_until(events, lambda event: isinstance(event, BurstFinished))

    assert resynchronised(simulator)


def test_the_timeout_after_a_burst_comes_from_the_worker_s_own_timeout_rule(idle_worker):
    worker, events = idle_worker(reading_timeout=lambda _setup: 3.5)
    simulator = FaultySimulator()
    connect(worker, events, simulator)
    simulator.timeout = 1.0

    worker.start_burst(TriggerSettings(sample_count=3))
    collect_until(events, lambda event: isinstance(event, BurstFinished))

    assert simulator.timeout == 3.5
