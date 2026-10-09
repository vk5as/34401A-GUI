"""The Worker's Single Readings and Bursts: completion, polling for external triggers, cancelling, and failures."""

import queue
import time
from typing import TYPE_CHECKING

import pytest

from agilent34401a.errors import BurstTooLargeError, TransportTimeoutError
from agilent34401a.meter import Function, Setup, measurement_time
from agilent34401a.sim import Simulator
from agilent34401a.trigger import TriggerSettings, TriggerSource
from agilent34401a.worker import (
    BurstCancelled,
    BurstFailed,
    BurstFinished,
    BurstProgressed,
    BurstStarted,
    Connected,
    Disconnected,
    ErrorsReported,
    Event,
    RawReplied,
    ReadingTaken,
    SetupChanged,
    Worker,
)

if TYPE_CHECKING:
    from collections.abc import Callable

TIMEOUT_S = 5.0


class BurstSimulator(Simulator):
    """A Simulator whose queries a test can watch, and interfere with, from the Worker's own thread."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads = 0
        self.clears = 0
        self.writes: list[str] = []
        self.timeouts_at_opc: list[float] = []
        self.on_status: Callable[[int], None] = lambda _count: None
        self.status_polls = 0
        self.fail_opc = False
        self.supports_device_clear = True

    def write(self, command: str) -> None:
        self.writes.append(command)
        super().write(command)

    def query(self, command: str) -> str:
        if command == "READ?":
            self.reads += 1
        elif command == "*STB?":
            self.status_polls += 1
            self.on_status(self.status_polls)
        elif command == "*OPC?":
            self.timeouts_at_opc.append(self.timeout)
            if self.fail_opc:
                message = "no answer"
                raise TransportTimeoutError(message)
        return super().query(command)

    def clear(self) -> None:
        self.clears += 1
        super().clear()


@pytest.fixture
def started():
    workers: list[Worker] = []

    def start(transport: BurstSimulator, **kwargs) -> tuple[Worker, "queue.Queue[Event]"]:
        events: queue.Queue[Event] = queue.Queue()
        worker = Worker(lambda: transport, events, burst_poll_interval_s=0.005, **kwargs)
        worker.start()
        workers.append(worker)
        assert isinstance(events.get(timeout=TIMEOUT_S), Connected)
        return worker, events

    yield start
    for worker in workers:
        worker.shutdown()


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


def settled(worker: Worker, events: "queue.Queue[Event]") -> None:
    """Prove the Worker has acted on every request sent before this and put out every event they led to.

    Requests are served in order, so the answer to a raw query sent now comes after everything earlier requests
    produced: if nothing but that answer arrives, nothing else was going to. (No sleeping and hoping.)
    """
    worker.send_raw("*IDN?")
    marker = next_event(events)
    assert isinstance(marker, RawReplied)
    assert marker.command == "*IDN?"


def until(events: "queue.Queue[Event]", kind: type) -> list[Event]:
    """Collect events up to and including the first of type `kind`."""
    collected = []
    while True:
        event = next_event(events)
        collected.append(event)
        if isinstance(event, kind):
            return collected


def burst(samples: int, triggers: int = 1, source=TriggerSource.IMMEDIATE, delay=None) -> TriggerSettings:
    return TriggerSettings(source=source, delay=delay, sample_count=samples, trigger_count=triggers)


# --- Single ---------------------------------------------------------------------------------------------------


def test_single_takes_exactly_one_reading_and_then_waits(started):
    simulator = BurstSimulator(dc_voltage=2.5)
    worker, events = started(simulator)

    worker.single()

    taken = next_event(events)
    assert isinstance(taken, ReadingTaken)
    assert taken.reading.value == 2.5
    settled(worker, events)
    assert simulator.reads == 1


def test_single_after_a_burst_setup_was_left_on_the_meter_still_gives_one_reading(started):
    simulator = BurstSimulator(dc_voltage=1.0)
    for command in ("TRIG:SOUR BUS", "SAMP:COUN 4", "TRIG:COUN 2"):
        simulator.write(command)
    worker, events = started(simulator)

    worker.single()

    assert isinstance(next_event(events), ReadingTaken)
    assert simulator.query("TRIG:SOUR?") == "IMM"


# --- immediate Bursts -----------------------------------------------------------------------------------------


def test_an_immediate_burst_reports_its_start_then_each_reading_then_the_finish(started):
    simulator = BurstSimulator(dc_voltage=1.25)
    worker, events = started(simulator)

    worker.start_burst(burst(12))
    collected = until(events, BurstFinished)

    assert isinstance(collected[0], BurstStarted)
    assert collected[0].expected_readings == 12
    assert not collected[0].waits_for_trigger
    readings = [event for event in collected if isinstance(event, ReadingTaken)]
    assert [taken.reading.value for taken in readings] == [1.25] * 12
    finished = collected[-1]
    assert isinstance(finished, BurstFinished)
    assert finished.readings == tuple(readings)
    assert finished.trigger == burst(12)


def test_burst_readings_are_stamped_from_the_start_with_the_setup_they_were_taken_under(started):
    worker, events = started(BurstSimulator())
    setup = Setup.default(Function.DC_VOLTAGE).with_trigger(burst(4, delay=0.5))

    worker.start_burst(burst(4, delay=0.5))
    finished = until(events, BurstFinished)[-1]

    assert isinstance(finished, BurstFinished)
    stamps = [taken.timestamp for taken in finished.readings]
    reading = measurement_time(setup)
    assert [later - stamps[0] for later in stamps] == pytest.approx([0, reading, 2 * reading, 3 * reading])
    first = finished.readings[0]
    assert first.setup.function is Function.DC_VOLTAGE
    assert first.setup.trigger == burst(4, delay=0.5)
    assert (finished.readings[-1].taken_at - first.taken_at).total_seconds() == pytest.approx(3 * reading, abs=1e-3)


def test_a_burst_leaves_the_meter_ready_for_continuous_readings(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)
    worker.start_burst(burst(5, 2, TriggerSource.BUS))
    until(events, BurstFinished)

    assert simulator.query("TRIG:SOUR?") == "IMM"
    assert float(simulator.query("SAMP:COUN?")) == 1
    worker.start_continuous()
    assert isinstance(next_event(events), ReadingTaken)


def test_a_burst_larger_than_reading_memory_is_refused_before_anything_is_sent(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)
    sent = len(simulator.writes)

    with pytest.raises(BurstTooLargeError):
        worker.start_burst(burst(100, 6))

    settled(worker, events)
    assert [command for command in simulator.writes[sent:] if command != "*IDN?"] == []


def test_a_burst_waits_for_operation_complete_with_a_timeout_sized_from_the_setup(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)
    worker.apply_setup(Setup.default(Function.DC_VOLTAGE).with_nplc(1))
    until(events, SetupChanged)

    worker.start_burst(burst(50))
    until(events, BurstFinished)

    [timeout] = simulator.timeouts_at_opc
    assert timeout > 50 * 0.04  # the whole Burst, not one Reading
    assert timeout < 60


def test_a_burst_that_does_not_complete_fails_and_the_connection_is_resynchronised(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)
    simulator.fail_opc = True

    worker.start_burst(burst(5))
    failed = until(events, BurstFailed)[-1]

    assert isinstance(failed, BurstFailed)
    assert "no answer" in failed.message
    assert simulator.clears >= 1
    assert simulator.query("SAMP:COUN?") == "+1.00000000E+00"  # the Meter was put back as it was


def test_a_burst_the_meter_will_not_start_fails_with_what_it_complained_of(started):
    simulator = BurstSimulator(time_scale=1, clock=lambda: 100.0)
    worker, events = started(simulator)
    simulator.write("SAMP:COUN 100")
    simulator.write("INIT")  # a measurement is already under way, and never ends

    worker.start_burst(burst(5))
    failed = until(events, BurstFailed)[-1]

    assert isinstance(failed, BurstFailed)
    assert "Init ignored" in failed.message


def test_starting_a_burst_pauses_continuous_readings(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)
    worker.start_continuous()
    assert isinstance(next_event(events), ReadingTaken)

    worker.start_burst(burst(3))
    until(events, BurstFinished)
    reads = simulator.reads
    settled(worker, events)  # a Worker that went back to Continuous would have put a Reading before the marker

    assert simulator.reads == reads


# --- external and bus triggers ----------------------------------------------------------------------------------


def test_an_external_burst_polls_the_status_byte_until_the_trigger_input_fires(started):
    simulator = BurstSimulator(dc_voltage=3.0)
    simulator.on_status = lambda polls: simulator.external_trigger() if polls == 3 else None
    worker, events = started(simulator)

    worker.start_burst(burst(5, source=TriggerSource.EXTERNAL))
    collected = until(events, BurstFinished)

    started_event = collected[0]
    assert isinstance(started_event, BurstStarted)
    assert started_event.waits_for_trigger
    assert simulator.status_polls >= 3
    assert "*OPC?" not in simulator.writes
    assert len([event for event in collected if isinstance(event, ReadingTaken)]) == 5
    progress = [event for event in collected if isinstance(event, BurstProgressed)]
    assert progress[-1].points == 5
    assert progress[-1].expected_readings == 5


def test_a_cancelled_external_burst_clears_the_meter_and_puts_its_settings_back(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)
    worker.start_burst(burst(5, 2, TriggerSource.EXTERNAL))
    assert isinstance(next_event(events), BurstStarted)

    worker.cancel_burst()
    cancelled = next_event(events)

    assert isinstance(cancelled, BurstCancelled)
    assert simulator.clears == 1
    assert simulator.query("TRIG:SOUR?") == "IMM"
    assert float(simulator.query("TRIG:COUN?")) == 1
    worker.single()  # the Meter is idle again, and the Worker is serving requests
    assert isinstance(next_event(events), ReadingTaken)


def test_cancelling_when_no_burst_is_running_does_nothing(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)

    worker.cancel_burst()
    worker.single()

    assert isinstance(next_event(events), ReadingTaken)
    assert simulator.clears == 0


def test_a_burst_over_a_connection_that_cannot_send_a_device_clear_refuses_to_wait_for_an_external_trigger(started):
    simulator = BurstSimulator()
    simulator.supports_device_clear = False
    worker, events = started(simulator)

    worker.start_burst(burst(5, source=TriggerSource.EXTERNAL))
    failed = until(events, BurstFailed)[-1]

    assert isinstance(failed, BurstFailed)
    assert "device clear" in failed.message
    assert "INIT" not in simulator.writes


def test_a_bus_burst_sends_its_own_triggers(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)

    worker.start_burst(burst(4, 3, TriggerSource.BUS))
    collected = until(events, BurstFinished)

    assert simulator.writes.count("*TRG") == 3
    assert len([event for event in collected if isinstance(event, ReadingTaken)]) == 12
    assert not [event for event in collected if isinstance(event, ErrorsReported)]


# --- other requests while a Burst waits ---------------------------------------------------------------------------


def test_a_setup_change_asked_for_during_a_burst_is_made_after_it(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)
    worker.start_burst(burst(5, source=TriggerSource.EXTERNAL))
    assert isinstance(next_event(events), BurstStarted)

    worker.apply_setup(Setup.default(Function.DC_VOLTAGE).with_nplc(1))
    # The Worker polls the status byte while it waits, looking at its requests between polls: a few more polls prove it
    # has seen the request and left it for after the Burst.
    polls = simulator.status_polls
    deadline = time.monotonic() + TIMEOUT_S
    while simulator.status_polls < polls + 3 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert simulator.status_polls >= polls + 3
    assert events.empty()
    worker.cancel_burst()

    assert isinstance(next_event(events), BurstCancelled)
    changed = next_event(events)
    assert isinstance(changed, SetupChanged)
    assert changed.setup.nplc == 1


def test_disconnecting_during_a_burst_cancels_it_and_closes_the_connection(started):
    simulator = BurstSimulator()
    worker, events = started(simulator)
    worker.start_burst(burst(5, source=TriggerSource.EXTERNAL))
    assert isinstance(next_event(events), BurstStarted)

    worker.disconnect()
    collected = until(events, Disconnected)

    assert any(isinstance(event, BurstCancelled) for event in collected)
    assert simulator.clears >= 1
