"""The Simulator's trigger model: Trigger Source, Trigger Delay, counts, wait-for-trigger and Reading Memory."""

import pytest

from agilent34401a.errors import TransportTimeoutError
from agilent34401a.sim import Simulator

STATUS_ESB = 32  # the standard event summary bit of the status byte, set when *OPC completes with *ESE 1


class FakeTime:
    """A clock the Simulator and its sleeping share, so a test moves time on without waiting for it."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def errors(simulator: Simulator) -> list[str]:
    found = []
    while (entry := simulator.query("SYST:ERR?")) != '+0,"No error"':
        found.append(entry)
    return found


def values(reply: str) -> list[float]:
    return [float(word) for word in reply.split(",")]


def status(simulator: Simulator) -> int:
    return int(simulator.query("*STB?"))


def points(simulator: Simulator) -> int:
    return int(float(simulator.query("DATA:POIN?")))


def arm_for_completion(simulator: Simulator) -> None:
    """Ask the Meter to flag operation complete in the status byte once everything started so far is done."""
    simulator.write("*CLS")
    simulator.write("*ESE 1")
    simulator.write("*OPC")


# --- settings -------------------------------------------------------------------------------------------------------


def test_a_reset_meter_triggers_immediately_with_an_automatic_delay_and_one_reading():
    simulator = Simulator()

    assert simulator.query("TRIG:SOUR?") == "IMM"
    assert simulator.query("TRIG:DEL:AUTO?") == "1"
    assert float(simulator.query("SAMP:COUN?")) == 1
    assert float(simulator.query("TRIG:COUN?")) == 1


@pytest.mark.parametrize(
    ("command", "query", "reply"),
    [
        ("TRIG:SOUR BUS", "TRIG:SOUR?", "BUS"),
        ("TRIG:SOUR EXT", "TRIG:SOUR?", "EXT"),
        ("TRIGGER:SOURCE EXTERNAL", "TRIG:SOUR?", "EXT"),
        ("TRIG:SOUR IMM", "TRIG:SOUR?", "IMM"),
        ("SAMP:COUN 25", "SAMP:COUN?", "+2.50000000E+01"),
        ("SAMPLE:COUNT 25", "SAMP:COUN?", "+2.50000000E+01"),
        ("TRIG:COUN 7", "TRIG:COUN?", "+7.00000000E+00"),
        ("TRIG:COUN INF", "TRIG:COUN?", "+9.90000000E+37"),
        ("TRIG:COUN MAX", "TRIG:COUN?", "+5.00000000E+04"),
        ("SAMP:COUN MAX", "SAMP:COUN?", "+5.00000000E+04"),
        ("TRIG:DEL 0.25", "TRIG:DEL?", "+2.50000000E-01"),
        ("TRIG:DEL MAX", "TRIG:DEL?", "+3.60000000E+03"),
        ("TRIG:DEL MIN", "TRIG:DEL?", "+0.00000000E+00"),
    ],
)
def test_each_trigger_setting_reads_back_what_was_set(command, query, reply):
    simulator = Simulator()

    simulator.write(command)

    assert simulator.query(query) == reply
    assert errors(simulator) == []


def test_setting_a_fixed_trigger_delay_turns_the_automatic_delay_off_and_auto_turns_it_back_on():
    simulator = Simulator()

    simulator.write("TRIG:DEL 0.5")
    assert simulator.query("TRIG:DEL:AUTO?") == "0"
    simulator.write("TRIG:DEL:AUTO ON")

    assert simulator.query("TRIG:DEL:AUTO?") == "1"


@pytest.mark.parametrize(
    ("command", "error"),
    [
        ("SAMP:COUN 0", '-222,"Data out of range"'),
        ("SAMP:COUN 50001", '-222,"Data out of range"'),
        ("TRIG:COUN 0", '-222,"Data out of range"'),
        ("TRIG:DEL 3601", '-222,"Data out of range"'),
        ("TRIG:DEL -1", '-222,"Data out of range"'),
        ("TRIG:SOUR SOMETIMES", '-224,"Illegal parameter value"'),
        ("SAMP:COUN many", '-224,"Illegal parameter value"'),
        ("TRIG:DEL:AUTO maybe", '-224,"Illegal parameter value"'),
        ("SAMP:COUN", '-109,"Missing parameter"'),
    ],
)
def test_a_trigger_setting_the_meter_cannot_hold_is_refused_with_its_error_code(command, error):
    simulator = Simulator()

    simulator.write(command)

    assert errors(simulator) == [error]


def test_a_reset_puts_the_trigger_settings_back_to_their_defaults():
    simulator = Simulator()
    for command in ("TRIG:SOUR BUS", "SAMP:COUN 9", "TRIG:COUN 4", "TRIG:DEL 1"):
        simulator.write(command)

    simulator.write("*RST")

    assert simulator.query("TRIG:SOUR?") == "IMM"
    assert float(simulator.query("SAMP:COUN?")) == 1
    assert float(simulator.query("TRIG:COUN?")) == 1
    assert simulator.query("TRIG:DEL:AUTO?") == "1"


# --- immediate Bursts and Reading Memory -------------------------------------------------------------------------


def test_init_takes_the_sample_count_times_the_trigger_count_readings_into_reading_memory():
    simulator = Simulator(dc_voltage=2.0)
    simulator.write("SAMP:COUN 4")
    simulator.write("TRIG:COUN 3")

    simulator.write("INIT")

    assert simulator.query("*OPC?") == "1"
    assert points(simulator) == 12
    assert values(simulator.query("FETC?")) == [2.0] * 12


def test_fetch_leaves_the_readings_in_reading_memory_and_r_query_takes_them_out():
    simulator = Simulator()
    simulator.write("SAMP:COUN 5")
    simulator.write("INIT")

    first = simulator.query("FETC?")
    assert simulator.query("FETC?") == first
    assert len(values(simulator.query("R?"))) == 5
    assert points(simulator) == 0


def test_reading_memory_holds_512_readings_and_keeps_the_most_recent_ones():
    simulator = Simulator(signals={})
    simulator.write("SAMP:COUN 600")
    simulator.write("INIT")

    assert points(simulator) == 512
    assert len(values(simulator.query("FETC?"))) == 512


def test_a_new_init_clears_reading_memory():
    simulator = Simulator()
    simulator.write("SAMP:COUN 5")
    simulator.write("INIT")
    simulator.write("SAMP:COUN 2")
    simulator.write("INIT")

    assert points(simulator) == 2


def test_fetching_from_empty_reading_memory_is_an_error():
    simulator = Simulator()

    with pytest.raises(TransportTimeoutError):
        simulator.query("FETC?")

    assert errors(simulator) == ['-230,"Data corrupt or stale"']


def test_init_while_a_measurement_is_waiting_is_ignored_with_an_error():
    simulator = Simulator()
    simulator.write("TRIG:SOUR EXT")
    simulator.write("INIT")

    simulator.write("INIT")

    assert errors(simulator) == ['-213,"Init ignored"']


def test_a_read_query_with_the_default_settings_returns_one_reading_as_before():
    simulator = Simulator(dc_voltage=1.5)

    assert simulator.query("READ?") == "+1.50000000E+00"


def test_a_read_query_returns_every_reading_of_the_sample_count():
    simulator = Simulator(dc_voltage=1.5)
    simulator.write("SAMP:COUN 3")

    assert values(simulator.query("READ?")) == [1.5, 1.5, 1.5]


def test_a_read_query_waiting_on_the_bus_is_a_trigger_deadlock():
    simulator = Simulator()
    simulator.write("TRIG:SOUR BUS")

    with pytest.raises(TransportTimeoutError):
        simulator.query("READ?")

    assert errors(simulator) == ['-214,"Trigger deadlock"']


# --- bus triggers -------------------------------------------------------------------------------------------------


def test_after_init_a_bus_triggered_meter_waits_for_each_trigger_command():
    simulator = Simulator(dc_voltage=3.0)
    simulator.write("TRIG:SOUR BUS")
    simulator.write("SAMP:COUN 2")
    simulator.write("TRIG:COUN 2")
    simulator.write("INIT")
    assert points(simulator) == 0

    simulator.write("*TRG")
    assert points(simulator) == 2
    simulator.write("*TRG")

    assert points(simulator) == 4
    assert simulator.query("*OPC?") == "1"
    assert errors(simulator) == []


def test_a_trigger_command_when_the_meter_is_not_waiting_is_ignored_with_an_error():
    simulator = Simulator()
    simulator.write("TRIG:SOUR BUS")

    simulator.write("*TRG")
    assert errors(simulator) == ['-211,"Trigger ignored"']

    simulator.write("INIT")
    simulator.write("*TRG")
    simulator.write("*TRG")  # the single trigger was used up
    assert errors(simulator) == ['-211,"Trigger ignored"']


def test_operation_complete_cannot_be_queried_while_the_meter_still_waits_for_a_trigger():
    simulator = Simulator()
    simulator.write("TRIG:SOUR BUS")
    simulator.write("INIT")

    with pytest.raises(TransportTimeoutError):
        simulator.query("*OPC?")


# --- external triggers and the status byte ------------------------------------------------------------------


def test_an_externally_triggered_meter_takes_its_readings_when_the_trigger_input_fires():
    simulator = Simulator(dc_voltage=4.0)
    simulator.write("TRIG:SOUR EXT")
    simulator.write("SAMP:COUN 3")
    simulator.write("INIT")
    assert points(simulator) == 0

    simulator.external_trigger()

    assert points(simulator) == 3
    assert values(simulator.query("FETC?")) == [4.0] * 3


def test_the_status_byte_flags_operation_complete_only_when_the_burst_is_done():
    simulator = Simulator()
    simulator.write("TRIG:SOUR EXT")
    simulator.write("TRIG:COUN 2")
    simulator.write("INIT")
    arm_for_completion(simulator)
    assert status(simulator) & STATUS_ESB == 0

    simulator.external_trigger()
    assert status(simulator) & STATUS_ESB == 0
    simulator.external_trigger()

    assert status(simulator) & STATUS_ESB
    assert int(simulator.query("*ESR?")) & 1
    assert status(simulator) & STATUS_ESB == 0  # reading the event register clears it


def test_operation_complete_is_flagged_at_once_when_nothing_is_running():
    simulator = Simulator()
    arm_for_completion(simulator)

    assert status(simulator) & STATUS_ESB


def test_clearing_the_meter_abandons_a_wait_for_trigger_and_keeps_what_was_taken():
    simulator = Simulator()
    simulator.write("TRIG:SOUR EXT")
    simulator.write("TRIG:COUN 2")
    simulator.write("INIT")
    simulator.external_trigger()

    simulator.clear()

    assert points(simulator) == 1
    simulator.external_trigger()  # nothing is waiting any more
    assert points(simulator) == 1
    simulator.write("INIT")  # and a new measurement can start
    assert errors(simulator) == []


def test_an_external_trigger_when_nothing_is_waiting_does_nothing():
    simulator = Simulator()

    simulator.external_trigger()

    assert errors(simulator) == []
    assert points(simulator) == 0


# --- timing -------------------------------------------------------------------------------------------------------


def timed_simulator(fake: FakeTime) -> Simulator:
    simulator = Simulator(time_scale=1, clock=fake.clock, sleep=fake.sleep)
    simulator.write("VOLT:DC:NPLC 1")  # 0.04 s a Reading with Autozero on
    simulator.write("TRIG:DEL 0.01")
    simulator.write("SAMP:COUN 10")
    return simulator


def test_readings_reach_reading_memory_as_the_measurement_time_passes():
    fake = FakeTime()
    simulator = timed_simulator(fake)
    simulator.write("INIT")
    assert points(simulator) == 0

    fake.now += 0.01 + 0.04 * 3 + 0.001
    assert points(simulator) == 3
    fake.now += 1.0

    assert points(simulator) == 10


def test_the_operation_complete_query_takes_as_long_as_the_burst_has_left():
    fake = FakeTime()
    simulator = timed_simulator(fake)
    simulator.write("TRIG:COUN 2")
    simulator.write("INIT")
    fake.now += 0.1

    assert simulator.query("*OPC?") == "1"

    assert fake.slept == [pytest.approx(2 * (0.01 + 0.4) - 0.1)]


def test_the_operation_complete_query_times_out_when_the_burst_outlasts_the_timeout():
    fake = FakeTime()
    simulator = timed_simulator(fake)
    simulator.timeout = 0.2
    simulator.write("INIT")

    with pytest.raises(TransportTimeoutError):
        simulator.query("*OPC?")


def test_a_trigger_that_arrives_while_the_meter_is_measuring_is_ignored():
    fake = FakeTime()
    simulator = timed_simulator(fake)
    simulator.write("TRIG:SOUR BUS")
    simulator.write("TRIG:COUN 2")
    simulator.write("INIT")
    simulator.write("*TRG")
    fake.now += 0.1

    simulator.write("*TRG")
    assert errors(simulator) == ['-211,"Trigger ignored"']
    fake.now += 1.0
    simulator.write("*TRG")

    assert errors(simulator) == []
    fake.now += 1.0
    assert points(simulator) == 20


def test_a_read_query_takes_the_delay_and_all_the_readings_to_reply():
    fake = FakeTime()
    simulator = timed_simulator(fake)

    assert len(values(simulator.query("READ?"))) == 10

    assert fake.slept == [pytest.approx(0.01 + 0.4)]
