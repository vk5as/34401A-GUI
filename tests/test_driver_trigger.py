"""The Driver's trigger settings, Single Readings and Bursts through Reading Memory."""

import pytest

from agilent34401a.driver import Driver
from agilent34401a.errors import BurstRefusedError, BurstTooLargeError, MalformedReplyError
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import Simulator
from agilent34401a.trigger import TriggerSettings, TriggerSource


class Recording:
    """Passes everything through to a Simulator and remembers what was sent and what the timeout was."""

    def __init__(self, simulator: Simulator, wrong_answers: dict[str, str] | None = None) -> None:
        self.simulator = simulator
        self._wrong_answers = wrong_answers or {}
        self.commands: list[str] = []
        self.timeouts_seen: list[float] = []
        self._override: str | None = None

    @property
    def timeout(self) -> float:
        return self.simulator.timeout

    @timeout.setter
    def timeout(self, seconds: float) -> None:
        self.simulator.timeout = seconds

    def write(self, command: str) -> None:
        self.commands.append(command)
        self.timeouts_seen.append(self.simulator.timeout)
        self._override = self._wrong_answers.get(command)
        self.simulator.write(command)

    def read(self) -> str:
        reply = self.simulator.read()
        return reply if self._override is None else self._override

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self.simulator.clear()

    def close(self) -> None:
        self.simulator.close()


def burst_of(samples: int, triggers: int = 1, source: TriggerSource = TriggerSource.IMMEDIATE, delay=None):
    return TriggerSettings(source=source, delay=delay, sample_count=samples, trigger_count=triggers)


# --- the trigger settings in the Setup -----------------------------------------------------------------------


def test_read_setup_reads_the_trigger_settings_the_meter_holds():
    simulator = Simulator()
    for command in ("TRIG:SOUR EXT", "TRIG:DEL 0.25", "SAMP:COUN 30", "TRIG:COUN 4"):
        simulator.write(command)

    setup = Driver(simulator).read_setup()

    assert setup.trigger == TriggerSettings(TriggerSource.EXTERNAL, 0.25, 30, 4)


def test_read_setup_reads_an_automatic_delay_and_an_infinite_trigger_count():
    simulator = Simulator()
    simulator.write("TRIG:COUN INF")

    trigger = Driver(simulator).read_setup().trigger

    assert trigger.delay is None
    assert trigger.trigger_count is None


def test_read_setup_of_a_reset_meter_has_the_default_trigger_settings():
    assert Driver(Simulator()).read_setup().trigger == TriggerSettings()


@pytest.mark.parametrize(
    ("question", "answer"),
    [("TRIG:SOUR?", "SOMETIMES"), ("SAMP:COUN?", "many"), ("TRIG:COUN?", "0"), ("TRIG:DEL:AUTO?", "maybe")],
)
def test_read_setup_rejects_trigger_replies_it_cannot_understand(question, answer):
    transport = Recording(Simulator(), {question: answer})

    with pytest.raises(MalformedReplyError):
        Driver(transport).read_setup()


def test_apply_sends_the_trigger_settings_last():
    transport = Recording(Simulator())
    setup = Setup.default(Function.DC_VOLTAGE).with_trigger(TriggerSettings(TriggerSource.BUS, 0.5, 20, 3))

    errors = Driver(transport).apply(setup)

    assert errors == []
    assert transport.commands[-5:-1] == ["TRIG:SOUR BUS", "TRIG:DEL 0.5", "SAMP:COUN 20", "TRIG:COUN 3"]
    assert Driver(transport.simulator).read_setup().trigger == setup.trigger


def test_apply_sends_an_automatic_delay_and_an_infinite_trigger_count_in_the_meters_words():
    transport = Recording(Simulator())
    setup = Setup.default(Function.DC_VOLTAGE).with_trigger(TriggerSettings(trigger_count=None))

    Driver(transport).apply(setup)

    assert "TRIG:DEL:AUTO ON" in transport.commands
    assert "TRIG:COUN INF" in transport.commands


# --- Single and Continuous ------------------------------------------------------------------------------------


def test_a_reading_with_the_default_trigger_settings_sends_nothing_but_the_read_query():
    transport = Recording(Simulator())
    driver = Driver(transport)

    driver.read()

    assert transport.commands == ["READ?"]


def test_a_reading_puts_the_meter_back_to_one_immediate_reading_when_a_burst_setup_was_left_in_place():
    transport = Recording(Simulator(dc_voltage=2.5))
    driver = Driver(transport)
    driver.apply(Setup.default(Function.DC_VOLTAGE).with_trigger(burst_of(20, 3, TriggerSource.BUS, 0.1)))
    transport.commands.clear()

    reading = driver.read()

    assert reading.value == 2.5
    assert transport.commands == ["TRIG:SOUR IMM", "TRIG:DEL 0.1", "SAMP:COUN 1", "TRIG:COUN 1", "READ?"]
    assert driver.setup.trigger == TriggerSettings(delay=0.1)  # the delay is kept; it applies to every Reading
    transport.commands.clear()
    driver.read()
    assert transport.commands == ["READ?"]


# --- Bursts ---------------------------------------------------------------------------------------------------


def test_a_burst_larger_than_reading_memory_is_refused_before_anything_is_sent():
    transport = Recording(Simulator())

    with pytest.raises(BurstTooLargeError):
        Driver(transport).start_burst(burst_of(100, 6))

    assert transport.commands == []


def test_an_immediate_burst_is_started_waited_for_and_fetched_from_reading_memory():
    transport = Recording(Simulator(dc_voltage=1.5))
    driver = Driver(transport)

    burst = driver.start_burst(burst_of(8, 2))
    driver.wait_for_burst(burst)
    readings = driver.fetch_burst(burst)

    assert [reading.value for reading in readings] == [1.5] * 16
    assert all(reading.function is Function.DC_VOLTAGE for reading in readings)
    assert readings[0].raw == "+1.50000000E+00"
    assert transport.commands[:4] == ["TRIG:SOUR IMM", "TRIG:DEL:AUTO ON", "SAMP:COUN 8", "TRIG:COUN 2"]
    assert "INIT" in transport.commands
    assert transport.commands.index("INIT") < transport.commands.index("*OPC?") < transport.commands.index("FETC?")


def test_a_burst_leaves_the_meter_as_it_found_it_when_it_is_finished():
    simulator = Simulator()
    driver = Driver(simulator)
    before = driver.read_setup()
    burst = driver.start_burst(burst_of(5, 1, TriggerSource.BUS, 0.2))
    driver.wait_for_burst(burst)

    assert driver.finish_burst(burst) == []

    assert Driver(simulator).read_setup() == before
    assert driver.setup == before


def test_waiting_for_a_burst_uses_a_timeout_that_fits_it_and_puts_the_old_one_back():
    transport = Recording(Simulator())
    driver = Driver(transport)
    transport.timeout = 7.0
    burst = driver.start_burst(burst_of(50))

    driver.wait_for_burst(burst, timeout=123.0)
    driver.fetch_burst(burst)

    opc = transport.commands.index("*OPC?")
    assert transport.timeouts_seen[opc] == 123.0
    assert transport.timeout == 7.0


def test_polling_an_external_burst_reports_progress_until_the_trigger_input_fires():
    simulator = Simulator()
    driver = Driver(simulator)
    burst = driver.start_burst(burst_of(6, 1, TriggerSource.EXTERNAL))

    waiting = driver.burst_progress(burst)
    simulator.external_trigger()
    done = driver.burst_progress(burst)

    assert (waiting.points, waiting.complete) == (0, False)
    assert (done.points, done.complete) == (6, True)
    assert len(driver.fetch_burst(burst)) == 6


def test_polling_a_bus_burst_sends_each_trigger_when_the_meter_is_ready_for_it():
    transport = Recording(Simulator())
    driver = Driver(transport)
    burst = driver.start_burst(burst_of(3, 3, TriggerSource.BUS))

    progress = [driver.burst_progress(burst) for _ in range(4)]

    assert transport.commands.count("*TRG") == 3
    assert [step.complete for step in progress][-1]
    assert progress[-1].points == 9
    assert driver.finish_burst(burst) == []  # no trigger was ignored


def test_a_burst_the_meter_will_not_start_is_refused_with_its_own_complaint_and_the_meter_is_put_back():
    simulator = Simulator(time_scale=1, clock=lambda: 100.0)  # time stands still, so the measurement never ends
    simulator.write("SAMP:COUN 100")
    simulator.write("INIT")  # a measurement is already under way
    driver = Driver(simulator)
    before = driver.read_setup()

    with pytest.raises(BurstRefusedError, match="Init ignored"):
        driver.start_burst(burst_of(5))

    assert Driver(simulator).read_setup().trigger == before.trigger
