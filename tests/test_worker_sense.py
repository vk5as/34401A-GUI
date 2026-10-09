"""The Worker and the sense options: they come back on connect and after every Setup change."""

import queue

import pytest

from agilent34401a.driver import QueuedError
from agilent34401a.meter import (
    AcFilter,
    Autozero,
    Function,
    GateTime,
    InputImpedance,
    Setup,
    Terminals,
    reading_timeout,
)
from agilent34401a.sim import Simulator
from agilent34401a.worker import (
    Connected,
    ErrorsReported,
    Event,
    ReadingTaken,
    SetupChanged,
    TerminalsChanged,
    Worker,
)

TIMEOUT_S = 5.0


class RefusingSimulator(Simulator):
    """A Simulator that queues an error instead of accepting a command that starts with `refused`."""

    def __init__(self, refused: str) -> None:
        super().__init__()
        self._refused = refused

    def write(self, command: str) -> None:
        if command.startswith(self._refused) and not command.endswith("?"):
            self._errors.append('-222,"Data out of range"')
        else:
            super().write(command)


class RecordingSimulator(Simulator):
    """A Simulator that remembers every command written to it."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)
        super().write(command)


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


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


def test_connecting_reads_the_sense_options_back_without_changing_the_meter(started):
    simulator = RecordingSimulator()
    for command in ('FUNC "VOLT:AC"', "DET:BAND 3"):
        simulator.write(command)
    simulator.writes.clear()
    _worker, events = started(simulator)

    connected = next_event(events)

    assert isinstance(connected, Connected)
    assert connected.setup == Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.SLOW)
    assert all(command.endswith("?") for command in simulator.writes)


@pytest.mark.parametrize("terminals", list(Terminals))
def test_connecting_reports_which_terminals_are_active(started, terminals):
    simulator = Simulator()
    simulator.terminals = terminals
    _worker, events = started(simulator)

    connected = next_event(events)

    assert isinstance(connected, Connected)
    assert connected.terminals is terminals


@pytest.mark.parametrize(
    "setup",
    [
        Setup.default(Function.AC_CURRENT).with_ac_filter(AcFilter.FAST),
        Setup.default(Function.PERIOD).with_gate_time(GateTime.TEN_MILLISECONDS),
        Setup.default(Function.DC_VOLTAGE)
        .with_autozero(Autozero.OFF)
        .with_input_impedance(InputImpedance.HIGH_IMPEDANCE),
    ],
)
def test_a_setup_with_sense_options_is_applied_and_reported_back(started, setup):
    worker, events = started(Simulator())
    next_event(events)

    worker.apply_setup(setup)

    assert next_event(events) == SetupChanged(setup)


def test_autozero_once_is_reported_back_as_off(started):
    worker, events = started(Simulator())
    next_event(events)
    setup = Setup.default(Function.DC_VOLTAGE)

    worker.apply_setup(setup.with_autozero(Autozero.ONCE))

    assert next_event(events) == SetupChanged(setup.with_autozero(Autozero.OFF))


def test_the_timeout_follows_the_ac_filter_so_slow_readings_do_not_time_out(started):
    simulator = Simulator()
    worker, events = started(simulator)
    next_event(events)
    slow = Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.SLOW)

    worker.apply_setup(slow)

    assert next_event(events) == SetupChanged(slow)
    assert simulator.timeout == reading_timeout(slow)
    assert simulator.timeout > 7.0


def test_readings_follow_the_gate_time_that_was_applied(started):
    simulator = Simulator(signals={Function.FREQUENCY: 1234.56789})
    worker, events = started(simulator)
    next_event(events)
    setup = Setup.default(Function.FREQUENCY).with_gate_time(GateTime.ONE_SECOND)

    worker.apply_setup(setup)
    worker.start_continuous()

    assert next_event(events) == SetupChanged(setup)
    taken = next_event(events)
    assert isinstance(taken, ReadingTaken)
    assert taken.setup == setup
    assert taken.reading.value == pytest.approx(1234.568)


def test_an_option_the_meter_refuses_is_reported_with_the_setup_it_kept(started):
    worker, events = started(RefusingSimulator("DET:BAND"))
    next_event(events)

    worker.apply_setup(Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.SLOW))

    assert next_event(events) == SetupChanged(Setup.default(Function.AC_VOLTAGE))
    assert next_event(events) == ErrorsReported((QueuedError(-222, "Data out of range"),))


def test_a_moved_terminals_switch_is_noticed_after_the_next_setup_change(started):
    simulator = Simulator()
    worker, events = started(simulator)
    next_event(events)
    simulator.terminals = Terminals.REAR

    worker.apply_setup(Setup.default(Function.DIODE))

    assert isinstance(next_event(events), SetupChanged)
    assert next_event(events) == TerminalsChanged(Terminals.REAR)


def test_unchanged_terminals_are_not_reported_again(started):
    simulator = Simulator()
    worker, events = started(simulator)
    next_event(events)

    worker.apply_setup(Setup.default(Function.DIODE))
    worker.apply_setup(Setup.default(Function.CONTINUITY))

    assert next_event(events) == SetupChanged(Setup.default(Function.DIODE))
    assert next_event(events) == SetupChanged(Setup.default(Function.CONTINUITY))
    worker.start_continuous()
    assert isinstance(next_event(events), ReadingTaken)  # nothing about the Terminals came first
