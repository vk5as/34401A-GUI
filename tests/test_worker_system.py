import queue
import threading
from collections.abc import Callable  # noqa: TC003 - used in an annotation the tests evaluate
from typing import TypeVar

import pytest

from agilent34401a.driver import QueuedError, SystemInfo
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import Simulator
from agilent34401a.worker import (
    AdminFailed,
    Connected,
    Disconnected,
    ErrorsReported,
    Event,
    LockoutChanged,
    ReadingTaken,
    ResetDone,
    SelfTestFinished,
    SetupChanged,
    SystemRead,
    Worker,
)

TIMEOUT_S = 5.0
_E = TypeVar("_E")
INTERVAL_S = 5.0


class WatchedSimulator(Simulator):
    """A Simulator that logs every command and lets a test act just before a chosen Reading."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.writes: list[str] = []
        self.reads = 0
        self.before_reading: Callable[[int], None] = lambda _count: None

    def write(self, command: str) -> None:
        self.writes.append(command)
        super().write(command)

    def query(self, command: str) -> str:
        if command == "READ?":
            self.reads += 1
            self.before_reading(self.reads)
        return super().query(command)


class FakeClock:
    """A clock only the test moves, so the error check interval is exercised without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


def next_of(events: "queue.Queue[Event]", kind: type[_E]) -> _E:
    """Return the next event of type `kind`, skipping Readings and anything else on the way."""
    while True:
        event = next_event(events)
        if isinstance(event, kind):
            return event


@pytest.fixture
def started():
    workers = []

    def start(transport, *, clock=None, interval=INTERVAL_S):
        events: queue.Queue[Event] = queue.Queue()
        worker = Worker(lambda: transport, events, error_check_interval_s=interval, clock=clock or FakeClock())
        worker.start()
        workers.append(worker)
        assert isinstance(next_event(events), Connected)
        return worker, events

    yield start
    for worker in workers:
        worker.shutdown()


def test_read_system_reports_version_calibration_beeper_and_display(started):
    worker, events = started(Simulator(calibration_count=3, calibration_message="CAL 1"))

    worker.read_system()

    event = next_of(events, SystemRead)
    assert event == SystemRead(SystemInfo("1994.0", 3, "CAL 1", beeper_enabled=True, display_on=True, display_text=""))


def test_reset_sends_rst_and_reports_the_setup_the_meter_is_then_in(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)
    worker.apply_setup(Setup.default(Function.RESISTANCE_2W))
    assert next_of(events, SetupChanged).setup.function is Function.RESISTANCE_2W

    worker.reset_meter()

    assert next_of(events, SetupChanged).setup == Setup.default(Function.DC_VOLTAGE)
    assert isinstance(next_of(events, ResetDone), ResetDone)
    assert simulator.writes.count("*RST") == 1


def test_the_worker_never_resets_the_meter_unless_asked(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)
    worker.apply_setup(Setup.default(Function.RESISTANCE_2W))
    worker.select_function(Function.DC_VOLTAGE)
    worker.start_continuous()
    next_of(events, ReadingTaken)
    worker.read_system()
    next_of(events, SystemRead)

    assert "*RST" not in simulator.writes


def test_self_test_reports_a_pass_and_the_setup_afterwards(started):
    slept: list[float] = []
    simulator = WatchedSimulator(time_scale=1, sleep=slept.append)
    worker, events = started(simulator)

    worker.run_self_test()

    assert next_of(events, SelfTestFinished) == SelfTestFinished(passed=True)
    assert isinstance(next_of(events, SetupChanged), SetupChanged)
    assert slept == [10.0]  # the Worker raised the timeout enough for the ten second test


def test_self_test_reports_a_failure_with_the_errors_the_meter_queued(started):
    simulator = WatchedSimulator()
    simulator.self_test_passes = False
    worker, events = started(simulator)

    worker.run_self_test()

    assert next_of(events, SelfTestFinished) == SelfTestFinished(passed=False)
    reported = next_of(events, ErrorsReported)
    assert isinstance(reported, ErrorsReported)
    assert reported.errors[0].code == -330


def test_a_self_test_that_never_finishes_is_reported_and_the_worker_goes_on(started):
    simulator = WatchedSimulator(time_scale=100, sleep=lambda _seconds: None)
    worker, events = started(simulator)

    worker.run_self_test()

    failed = next_of(events, AdminFailed)
    assert isinstance(failed, AdminFailed)
    assert "Self-test" in failed.message
    worker.read_system()  # the Worker is still alive and serving
    assert isinstance(next_of(events, SystemRead), SystemRead)


def test_a_running_self_test_does_not_block_the_caller(started):
    release = threading.Event()
    entered = threading.Event()

    def blocking_sleep(_seconds: float) -> None:
        entered.set()
        release.wait(TIMEOUT_S)

    simulator = WatchedSimulator(time_scale=1, sleep=blocking_sleep)
    worker, events = started(simulator)

    worker.run_self_test()  # returns at once; the ten seconds pass on the Worker's thread
    assert entered.wait(TIMEOUT_S)
    assert worker.is_alive()
    assert events.empty()
    release.set()

    assert next_of(events, SelfTestFinished) == SelfTestFinished(passed=True)


def test_check_errors_drains_the_queue_without_looking_at_the_status_byte(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)
    simulator.write("BAD:COMMAND")

    worker.check_errors()

    reported = next_of(events, ErrorsReported)
    assert isinstance(reported, ErrorsReported)
    assert reported.errors == (QueuedError(-113, "Undefined header"),)
    assert "*STB?" not in simulator.writes


def test_beeper_choice_test_beep_and_display_changes_are_confirmed_by_a_fresh_system_read(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)

    worker.set_beeper(enabled=False)
    assert next_of(events, SystemRead).info.beeper_enabled is False

    worker.beep()
    next_of(events, SystemRead)
    assert simulator.beeps == 1

    worker.show_display_text("HELLO")
    assert next_of(events, SystemRead).info.display_text == "HELLO"

    worker.show_display_text(None)
    assert next_of(events, SystemRead).info.display_text == ""

    worker.set_display(on=False)
    assert next_of(events, SystemRead).info.display_on is False


def test_display_text_the_display_cannot_show_is_reported_not_sent(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)

    worker.show_display_text("THIS IS FAR TOO LONG")

    assert isinstance(next_of(events, AdminFailed), AdminFailed)
    assert not [command for command in simulator.writes if command.startswith("DISP:TEXT ")]


def test_lockout_is_reported_when_it_starts_and_when_it_ends(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)

    worker.set_lockout(locked=True)
    assert next_of(events, LockoutChanged) == LockoutChanged(locked=True)
    assert simulator.front_panel_locked

    worker.set_lockout(locked=False)
    assert next_of(events, LockoutChanged) == LockoutChanged(locked=False)
    assert not simulator.front_panel_locked


def test_continuous_does_not_look_at_the_status_byte_before_the_interval_has_passed(started):
    clock = FakeClock()
    simulator = WatchedSimulator()
    worker, events = started(simulator, clock=clock)
    simulator.before_reading = lambda count: worker.pause() if count == 3 else None

    worker.start_continuous()
    for _ in range(3):
        assert isinstance(next_event(events), ReadingTaken)
    worker.read_system()
    next_of(events, SystemRead)

    assert "*STB?" not in simulator.writes  # no time has passed on the clock the Worker uses


def test_the_error_queue_is_drained_only_when_the_status_byte_says_it_holds_something(started):
    clock = FakeClock()
    simulator = WatchedSimulator()
    worker, events = started(simulator, clock=clock)

    def act(count: int) -> None:
        if count == 2:
            clock.advance(INTERVAL_S)  # the interval passes before the second Reading, with nothing queued
        if count == 4:
            simulator.write("BAD:COMMAND")  # an error is queued during the fourth Reading...
            clock.advance(INTERVAL_S)  # ...and the interval passes again
        if count == 6:
            worker.pause()

    simulator.before_reading = act
    worker.start_continuous()

    reported = next_of(events, ErrorsReported)
    assert isinstance(reported, ErrorsReported)
    assert reported.errors == (QueuedError(-113, "Undefined header"),)
    worker.read_system()
    next_of(events, SystemRead)
    assert simulator.writes.count("*STB?") == 2  # checked after the second and fourth Readings only
    assert simulator.writes.count("SYST:ERR?") == 2  # the second drain ends at "no error" after the one entry


def test_the_error_check_follows_the_interval_it_was_given(started):
    clock = FakeClock()
    simulator = WatchedSimulator()
    worker, events = started(simulator, clock=clock, interval=60.0)

    def act(count: int) -> None:
        if count == 2:
            clock.advance(59.0)
        if count == 3:
            clock.advance(2.0)
        if count == 5:
            worker.pause()

    simulator.before_reading = act
    worker.start_continuous()
    for _ in range(5):
        assert isinstance(next_event(events), ReadingTaken)
    worker.read_system()
    next_of(events, SystemRead)

    assert simulator.writes.count("*STB?") == 1


def test_no_error_check_happens_while_paused(started):
    clock = FakeClock()
    simulator = WatchedSimulator()
    worker, events = started(simulator, clock=clock)

    clock.advance(10 * INTERVAL_S)
    worker.read_system()
    next_of(events, SystemRead)

    assert "*STB?" not in simulator.writes


def test_a_failed_status_byte_check_is_reported_and_continuous_goes_on(started):
    clock = FakeClock()

    class BrokenStatus(WatchedSimulator):
        def query(self, command: str) -> str:
            return "garbage" if command == "*STB?" else super().query(command)

    simulator = BrokenStatus()
    worker, events = started(simulator, clock=clock)
    simulator.before_reading = lambda count: clock.advance(INTERVAL_S) if count == 2 else None

    worker.start_continuous()

    failed = next_of(events, AdminFailed)
    assert isinstance(failed, AdminFailed)
    assert "error" in failed.message.lower()
    assert isinstance(next_of(events, ReadingTaken), ReadingTaken)


def test_asking_for_continuous_again_does_not_postpone_the_error_check(started):
    clock = FakeClock()
    simulator = WatchedSimulator()
    worker, events = started(simulator, clock=clock)

    def act(count: int) -> None:
        if count == 2:
            clock.advance(INTERVAL_S)
            worker.start_continuous()  # already running: the check due above must still happen
        if count == 4:
            worker.pause()

    simulator.before_reading = act
    worker.start_continuous()
    for _ in range(4):
        assert isinstance(next_event(events), ReadingTaken)
    worker.read_system()
    next_of(events, SystemRead)

    assert simulator.writes.count("*STB?") == 1


def test_disconnecting_releases_a_lockout_the_application_started_before_it_hands_the_meter_back(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)
    worker.set_lockout(locked=True)
    next_of(events, LockoutChanged)

    worker.disconnect()
    next_of(events, Disconnected)

    assert not simulator.front_panel_locked
    assert "SYST:LOC" in simulator.writes  # released by the application, not by the Simulator's going to Local


def test_disconnecting_without_a_lockout_sends_no_unlock_commands(started):
    simulator = WatchedSimulator()
    worker, events = started(simulator)
    worker.set_lockout(locked=True)
    next_of(events, LockoutChanged)
    worker.set_lockout(locked=False)
    next_of(events, LockoutChanged)
    simulator.writes.clear()

    worker.disconnect()
    next_of(events, Disconnected)

    assert "SYST:LOC" not in simulator.writes
