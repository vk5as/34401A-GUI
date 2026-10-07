import queue
import threading
import time
from collections.abc import Callable

import pytest

from agilent34401a.driver import QueuedError
from agilent34401a.errors import TransportError
from agilent34401a.meter import Function, Resolution, Setup, reading_timeout
from agilent34401a.sim import AGILENT_IDENTITY, Simulator
from agilent34401a.worker import (
    Connected,
    ConnectionFailed,
    Disconnected,
    ErrorsReported,
    Event,
    RawFailed,
    RawRefused,
    RawReplied,
    ReadingFailed,
    ReadingTaken,
    SetupChanged,
    SetupFailed,
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
        self.writes: list[str] = []
        self.refuse_ranges_of: str | None = None
        self.garbage_function = False

    def write(self, command: str) -> None:
        self.writes.append(command)
        if self.refuse_ranges_of and not command.endswith("?") and command.startswith(f"{self.refuse_ranges_of}:RANG"):
            self._errors.append('-222,"Data out of range"')
            return
        super().write(command)

    def query(self, command: str) -> str:
        if command == "FUNC?" and self.garbage_function:
            return "garbage"
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
    assert event.setup == Setup.default(Function.DC_VOLTAGE)
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
    simulator = HookedSimulator(time_scale=100, sleep=lambda _seconds: None)  # 40 s per Reading, beyond any timeout
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


def test_connecting_reads_the_setup_back_without_changing_the_meter(started):
    simulator = HookedSimulator()
    simulator.write('FUNC "RES"')
    simulator.write("RES:RANG 1000")
    simulator.write("RES:NPLC 1")
    simulator.writes.clear()
    _worker, events = started(simulator)

    connected = next_event(events)

    assert isinstance(connected, Connected)
    assert connected.setup == Setup.default(Function.RESISTANCE_2W).with_range(1000.0).with_nplc(1)
    assert all(command.endswith("?") for command in simulator.writes)


def test_a_meter_whose_setup_cannot_be_understood_fails_the_connection(started):
    simulator = HookedSimulator()
    simulator.garbage_function = True
    _worker, events = started(simulator)

    failed = next_event(events)

    assert isinstance(failed, ConnectionFailed)
    assert "garbage" in failed.message


def test_the_timeout_is_calculated_from_the_setup_the_meter_is_in(started):
    simulator = HookedSimulator()
    _worker, events = started(simulator)

    next_event(events)

    assert simulator.timeout == reading_timeout(Setup.default(Function.DC_VOLTAGE))


def test_applying_a_setup_reports_the_setup_the_meter_ended_up_in_and_readings_follow_it(started):
    simulator = HookedSimulator(signals={Function.AC_VOLTAGE: 2.0})
    worker, events = started(simulator)
    next_event(events)
    setup = Setup.default(Function.AC_VOLTAGE).with_range(10.0)

    worker.apply_setup(setup)
    worker.start_continuous()

    changed = next_event(events)
    assert changed == SetupChanged(setup)
    reading = next_reading(events)
    assert reading.reading.function is Function.AC_VOLTAGE
    assert reading.reading.value == pytest.approx(2.0)
    assert reading.setup == setup


def test_a_setup_can_be_applied_while_continuous_is_running(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)
    worker.start_continuous()
    next_reading(events)
    setup = Setup.default(Function.DC_VOLTAGE).with_range(1.0).with_resolution(Resolution.FIVE_HALF)

    worker.apply_setup(setup)

    seen = collect_until(events, lambda event: isinstance(event, ReadingTaken) and event.setup == setup)
    assert SetupChanged(setup) in seen


def test_the_timeout_grows_with_the_integration_time_so_slow_readings_do_not_time_out(started):
    simulator = HookedSimulator(time_scale=1, sleep=lambda _seconds: None)  # 100 NPLC takes 4 s, beyond the 2 s default
    worker, events = started(simulator)
    next_event(events)
    slow = Setup.default(Function.DC_VOLTAGE).with_nplc(100)

    worker.apply_setup(slow)
    worker.start_continuous()

    assert next_event(events) == SetupChanged(slow)
    assert simulator.timeout == reading_timeout(slow)
    assert simulator.timeout > 4.0
    assert isinstance(next_event(events), ReadingTaken)


def test_errors_the_meter_queues_after_a_setup_change_are_reported_with_the_setup_it_kept(started):
    simulator = HookedSimulator()
    simulator.refuse_ranges_of = "VOLT:AC"
    worker, events = started(simulator)
    next_event(events)

    worker.apply_setup(Setup.default(Function.AC_VOLTAGE).with_range(10.0))

    assert next_event(events) == SetupChanged(Setup.default(Function.AC_VOLTAGE))
    assert next_event(events) == ErrorsReported((QueuedError(-222, "Data out of range"),))


def test_a_setup_change_that_succeeds_reports_no_errors(started):
    worker, events = started(HookedSimulator())
    next_event(events)

    worker.apply_setup(Setup.default(Function.DIODE))

    assert isinstance(next_event(events), SetupChanged)
    time.sleep(0.05)
    assert events.empty()


def test_a_setup_change_the_worker_cannot_confirm_is_reported_and_the_worker_carries_on(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)
    simulator.garbage_function = True

    worker.apply_setup(Setup.default(Function.DIODE))
    failed = next_event(events)

    assert isinstance(failed, SetupFailed)
    assert "garbage" in failed.message
    assert simulator.clears == 1
    simulator.garbage_function = False
    worker.start_continuous()
    assert isinstance(next_event(events), ReadingTaken)


def test_selecting_a_function_keeps_its_own_settings_and_reports_the_setup(started):
    simulator = HookedSimulator(signals={Function.RESISTANCE_2W: 4700.0})
    simulator.write('FUNC "RES"')
    simulator.write("RES:RANG 10000")
    simulator.write("RES:NPLC 1")
    simulator.write('FUNC "VOLT"')
    worker, events = started(simulator)
    next_event(events)
    simulator.writes.clear()

    worker.select_function(Function.RESISTANCE_2W)
    worker.start_continuous()

    expected = Setup.default(Function.RESISTANCE_2W).with_range(1e4).with_nplc(1)
    assert next_event(events) == SetupChanged(expected)
    reading = next_reading(events)
    assert reading.reading.function is Function.RESISTANCE_2W
    assert reading.setup == expected
    assert [command for command in simulator.writes if not command.endswith("?")] == ['FUNC "RES"']


def test_a_raw_query_is_answered_with_the_reply_and_changes_no_setup(started):
    worker, events = started(HookedSimulator(identity=AGILENT_IDENTITY))
    next_event(events)

    worker.send_raw("*IDN?")

    assert next_event(events) == RawReplied("*IDN?", AGILENT_IDENTITY, ())
    time.sleep(0.05)
    assert events.empty()


def test_a_raw_write_is_followed_by_the_setup_the_meter_is_now_in(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)

    worker.send_raw('FUNC "RES"')

    assert next_event(events) == RawReplied('FUNC "RES"', None, ())
    changed = next_event(events)
    assert changed == SetupChanged(Setup.default(Function.RESISTANCE_2W))
    worker.start_continuous()
    assert next_reading(events).reading.function is Function.RESISTANCE_2W


def test_a_raw_write_the_meter_complains_about_reports_its_errors(started):
    worker, events = started(HookedSimulator())
    next_event(events)

    worker.send_raw("NOTACOMMAND")

    replied = next_event(events)
    assert isinstance(replied, RawReplied)
    assert [queued.code for queued in replied.errors] == [-113]


def test_raw_commands_are_served_between_readings_while_continuous_runs(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)
    worker.start_continuous()
    next_reading(events)

    worker.send_raw("VOLT:DC:NPLC 10")

    after = collect_until(events, lambda event: isinstance(event, SetupChanged))
    assert any(isinstance(event, RawReplied) for event in after)
    assert next_reading(events).setup.nplc == 10
    # The Transport was only ever used by the worker: the Reading in progress was never cut in two.
    assert "VOLT:DC:NPLC 10" in simulator.writes


def test_a_raw_query_that_times_out_is_reported_and_the_worker_carries_on(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)

    worker.send_raw("NOTAQUERY?")

    failed = next_event(events)
    assert isinstance(failed, RawFailed)
    assert failed.command == "NOTAQUERY?"
    assert [queued.code for queued in failed.errors] == [-113]
    assert simulator.clears == 1
    worker.send_raw("*IDN?")
    assert isinstance(next_event(events), RawReplied)
    assert worker.is_alive()


def test_a_raw_calibration_write_is_refused_and_never_reaches_the_meter(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)

    worker.send_raw("CAL:SEC:STAT OFF,HP034401")

    refused = next_event(events)
    assert isinstance(refused, RawRefused)
    assert refused.command == "CAL:SEC:STAT OFF,HP034401"
    assert "calibration" in refused.message
    worker.send_raw("*IDN?")
    next_event(events)
    assert not any(command.upper().startswith("CAL") for command in simulator.writes)


def test_a_raw_calibration_write_goes_through_when_it_is_explicitly_allowed(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)

    worker.send_raw("CAL:STR 'x'", allow_calibration=True)

    assert isinstance(next_event(events), RawReplied)
    assert "CAL:STR 'x'" in simulator.writes


def test_a_read_only_calibration_query_needs_no_override(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)

    worker.send_raw("CAL:COUN?")

    replied = next_event(events)
    assert isinstance(replied, RawReplied)
    assert replied.reply == "+1"
    assert "CAL:COUN?" in simulator.writes


def test_an_empty_raw_command_is_reported_not_sent(started):
    simulator = HookedSimulator()
    worker, events = started(simulator)
    next_event(events)
    written = len(simulator.writes)

    worker.send_raw("   ")

    failed = next_event(events)
    assert isinstance(failed, RawFailed)
    assert "Nothing to send" in failed.message
    assert len(simulator.writes) == written
