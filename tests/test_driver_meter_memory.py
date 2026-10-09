"""The Driver's Meter Memory: store the Meter's own Setup in a numbered location and recall it, checking its errors."""

import pytest

from agilent34401a.driver import Driver, QueuedError
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import Simulator


class RecordingSimulator(Simulator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.commands: list[str] = []

    def write(self, command: str) -> None:
        self.commands.append(command)
        super().write(command)


def test_a_setup_stored_in_the_meter_comes_back_with_a_recall_and_is_read_back():
    simulator = RecordingSimulator()
    driver = Driver(simulator)
    wanted = Setup.default(Function.DC_VOLTAGE).with_range(10.0).with_nplc(1)
    driver.apply(wanted)
    assert driver.save_to_meter(2) == []
    driver.apply(Setup.default(Function.RESISTANCE_2W))
    simulator.commands.clear()

    errors = driver.recall_from_meter(2)

    assert errors == []
    assert simulator.commands[0] == "*RCL 2"
    assert driver.read_setup() == wanted


def test_storing_and_recalling_drain_the_error_queue_after_the_command():
    simulator = RecordingSimulator()
    driver = Driver(simulator)

    driver.save_to_meter(1)
    driver.recall_from_meter(1)

    assert simulator.commands == ["*SAV 1", "SYST:ERR?", "*RCL 1", "SYST:ERR?"]


def test_recalling_a_location_that_was_never_stored_returns_the_meters_complaint():
    driver = Driver(Simulator())

    errors = driver.recall_from_meter(3)

    assert errors == [QueuedError(-314, "Save/recall memory lost")]


@pytest.mark.parametrize("location", [-1, 0, 4, 10])
def test_a_location_the_meter_cannot_store_to_is_refused_before_anything_is_sent(location):
    simulator = RecordingSimulator()

    with pytest.raises(ValueError, match="location"):
        Driver(simulator).save_to_meter(location)

    assert simulator.commands == []


@pytest.mark.parametrize("location", [-1, 4, 10])
def test_a_location_the_meter_cannot_recall_is_refused_before_anything_is_sent(location):
    simulator = RecordingSimulator()

    with pytest.raises(ValueError, match="location"):
        Driver(simulator).recall_from_meter(location)

    assert simulator.commands == []


def test_the_power_down_location_can_be_recalled_but_not_stored_to():
    driver = Driver(Simulator())

    assert driver.recall_from_meter(0) == []
