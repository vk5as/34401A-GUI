import queue
from collections.abc import Callable, Iterator

import pytest

from agilent34401a.serial_config import Framing, Parity, SerialSettings
from agilent34401a.sim import Simulator
from agilent34401a.sim_serial import SimulatedSerialMeter
from agilent34401a.worker import Connected, ConnectionFailed, Disconnected, Event, Worker

TIMEOUT_S = 5.0
SEVEN_E_ONE = Framing(7, Parity.EVEN, 1)


class RecordingSimulator(Simulator):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)
        super().write(command)


def next_event(events: "queue.Queue[Event]") -> Event:
    return events.get(timeout=TIMEOUT_S)


def is_remote(simulator: Simulator) -> bool:
    return simulator.remote  # a function, so the type checker does not assume the answer cannot change


StartWorker = Callable[[SimulatedSerialMeter, SerialSettings], tuple[Worker, "queue.Queue[Event]"]]


@pytest.fixture
def serial_worker() -> Iterator[StartWorker]:
    workers: list[Worker] = []

    def start(line: SimulatedSerialMeter, settings: SerialSettings) -> tuple[Worker, "queue.Queue[Event]"]:
        events: queue.Queue[Event] = queue.Queue()
        worker = Worker(lambda: line.open(settings), events)
        workers.append(worker)
        worker.start()
        return worker, events

    yield start
    for worker in workers:
        worker.shutdown()


def test_the_meter_is_put_in_remote_with_a_command_before_it_is_asked_who_it_is(serial_worker):
    simulator = RecordingSimulator()
    line = SimulatedSerialMeter(simulator, baud=4800, framing=SEVEN_E_ONE)

    _, events = serial_worker(line, SerialSettings(port="SIM", baud=4800, framing=SEVEN_E_ONE))

    assert isinstance(next_event(events), Connected)
    assert simulator.writes[:2] == ["SYST:REM", "*IDN?"]
    assert is_remote(simulator)


def test_disconnecting_returns_an_rs232_meter_to_local_with_a_command(serial_worker):
    simulator = RecordingSimulator()
    worker, events = serial_worker(SimulatedSerialMeter(simulator), SerialSettings(port="SIM"))
    assert isinstance(next_event(events), Connected)
    simulator.write("SYST:RWL")  # a front panel locked out by the application is handed back too

    worker.disconnect()

    assert isinstance(next_event(events), Disconnected)
    assert simulator.writes[-1] == "SYST:LOC"
    assert not is_remote(simulator)
    assert not simulator.front_panel_locked


def test_an_rs232_meter_that_does_not_hear_the_settings_is_a_failed_connection(serial_worker):
    _, events = serial_worker(SimulatedSerialMeter(baud=9600), SerialSettings(port="SIM", baud=300))

    failed = next_event(events)

    assert isinstance(failed, ConnectionFailed)
    assert failed.message
    assert isinstance(next_event(events), Disconnected)
