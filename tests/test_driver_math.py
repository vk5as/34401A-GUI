"""The Driver and Math Operations: applying them, reading them back, tagging Readings and fetching Statistics."""

import pytest

from agilent34401a.driver import Driver
from agilent34401a.errors import MalformedReplyError
from agilent34401a.math_operations import LimitResult, MathOperation, MathSettings, MeterStatistics
from agilent34401a.meter import Function, Setup
from agilent34401a.sim import Simulator


class RecordingSimulator(Simulator):
    """A Simulator that remembers every command sent to it, queries included."""

    def __init__(self, **options: object) -> None:
        super().__init__(**options)  # type: ignore[arg-type]
        self.commands: list[str] = []

    def write(self, command: str) -> None:
        self.commands.append(command)
        super().write(command)


class WrongAnswers(Simulator):
    """A Simulator that answers one query with something else."""

    def __init__(self, query: str, answer: str) -> None:
        super().__init__()
        self._query = query
        self._answer = answer
        self._replace_next = False

    def write(self, command: str) -> None:
        self._replace_next = command == self._query
        super().write(command)

    def read(self) -> str:
        reply = super().read()
        if self._replace_next:
            self._replace_next = False
            return self._answer
        return reply


def setup_with(operation: MathOperation | None, function: Function = Function.DC_VOLTAGE, **settings: float) -> Setup:
    return Setup.default(function).with_math(MathSettings(operation=operation, **settings))


def test_applying_a_setup_with_null_makes_readings_come_back_nulled():
    simulator = Simulator(signals={Function.DC_VOLTAGE: 1.0})
    driver = Driver(simulator)

    errors = driver.apply(setup_with(MathOperation.NULL, null_offset=0.25))

    assert errors == []
    reading = driver.read()
    assert reading.value == pytest.approx(0.75)
    assert reading.math is MathOperation.NULL
    assert reading.limit is None
    assert reading.unit == "V"


def test_readings_in_dbm_say_so():
    driver = Driver(Simulator(signals={Function.DC_VOLTAGE: 1.0}))
    driver.apply(setup_with(MathOperation.DBM, dbm_reference_resistance=50))

    reading = driver.read()

    assert reading.value == pytest.approx(13.0103, abs=1e-3)
    assert (reading.math, reading.unit) == (MathOperation.DBM, "dBm")


def test_readings_in_db_are_relative_to_the_reference():
    driver = Driver(Simulator(signals={Function.AC_VOLTAGE: 1.0}))
    driver.apply(setup_with(MathOperation.DB, Function.AC_VOLTAGE, db_reference=2.0))

    assert driver.read().value == pytest.approx(0.2185, abs=1e-3)


def test_readings_without_a_math_operation_carry_none():
    driver = Driver(Simulator())
    driver.apply(setup_with(MathOperation.NULL))
    driver.apply(setup_with(None))

    reading = driver.read()

    assert (reading.math, reading.limit) == (None, None)


@pytest.mark.parametrize(
    ("volts", "expected"),
    [(1.5, LimitResult.HIGH), (-1.5, LimitResult.LOW), (0.5, LimitResult.PASS), (1.0, LimitResult.PASS)],
)
def test_a_reading_in_a_limit_test_says_how_it_did(volts, expected):
    driver = Driver(Simulator(signals={Function.DC_VOLTAGE: volts}))
    driver.apply(setup_with(MathOperation.LIMIT_TEST, limit_lower=-1, limit_upper=1))

    reading = driver.read()

    assert reading.limit is expected
    assert reading.value == volts  # a Limit Test does not change the Reading
    assert reading.math is MathOperation.LIMIT_TEST


def test_each_reading_in_a_limit_test_is_judged_on_its_own():
    simulator = Simulator(signals={Function.DC_VOLTAGE: 1.5})
    driver = Driver(simulator)
    driver.apply(setup_with(MathOperation.LIMIT_TEST, limit_lower=-1, limit_upper=1))

    first = driver.read()
    simulator.set_signal(Function.DC_VOLTAGE, 0.0)
    second = driver.read()
    simulator.set_signal(Function.DC_VOLTAGE, -1.5)
    third = driver.read()

    assert [first.limit, second.limit, third.limit] == [LimitResult.HIGH, LimitResult.PASS, LimitResult.LOW]


def test_a_failure_the_meter_latched_before_the_limit_test_was_applied_is_not_blamed_on_the_first_reading():
    simulator = Simulator(signals={Function.DC_VOLTAGE: 1.5})
    simulator.write("CALC:LIM:UPP 1")
    simulator.write("CALC:FUNC LIM")
    simulator.write("CALC:STAT ON")
    simulator.query("READ?")  # leaves a high failure latched
    driver = Driver(simulator)
    simulator.set_signal(Function.DC_VOLTAGE, 0.0)

    driver.apply(setup_with(MathOperation.LIMIT_TEST, limit_lower=-1, limit_upper=1))

    assert driver.read().limit is LimitResult.PASS


def test_a_reading_costs_one_query_unless_a_limit_test_is_running():
    simulator = RecordingSimulator()
    driver = Driver(simulator)
    driver.apply(setup_with(MathOperation.NULL))
    simulator.commands.clear()
    driver.read()
    assert simulator.commands == ["READ?"]

    driver.apply(setup_with(MathOperation.LIMIT_TEST))
    simulator.commands.clear()
    driver.read()
    assert simulator.commands == ["READ?", "STAT:QUES:EVEN?"]


def test_applying_sends_the_settings_before_turning_the_operation_on():
    simulator = RecordingSimulator()
    driver = Driver(simulator)

    driver.apply(setup_with(MathOperation.DBM, dbm_reference_resistance=75, null_offset=0.5, limit_upper=2))

    math_commands = [command for command in simulator.commands if command.startswith("CALC")]
    assert math_commands == [
        "CALC:NULL:OFFS 0.5",
        "CALC:DB:REF 0",
        "CALC:DBM:REF 75",
        "CALC:LIM:LOW 0",
        "CALC:LIM:UPP 2",
        "CALC:FUNC DBM",
        "CALC:STAT ON",
    ]


def test_applying_a_setup_without_a_math_operation_turns_it_off():
    simulator = RecordingSimulator()
    driver = Driver(simulator)
    driver.apply(setup_with(MathOperation.NULL))

    driver.apply(setup_with(None))

    assert simulator.query("CALC:STAT?") == "0"


@pytest.mark.parametrize("function", [Function.CONTINUITY, Function.DIODE])
def test_a_function_with_no_math_gets_no_math_commands(function):
    simulator = RecordingSimulator()
    driver = Driver(simulator)

    errors = driver.apply(Setup.default(function))

    assert errors == []
    assert not [command for command in simulator.commands if command.startswith("CALC")]


def test_applying_the_operation_that_is_already_on_does_not_restart_statistics():
    simulator = Simulator()
    driver = Driver(simulator)
    statistics_on = setup_with(MathOperation.STATISTICS)
    driver.apply(statistics_on)
    driver.read()
    driver.read()

    driver.apply(statistics_on.with_range(10.0))

    assert driver.fetch_statistics().count == 2


@pytest.mark.parametrize("operation", [*list(MathOperation), None])
def test_read_setup_reports_the_math_operation_and_settings_the_meter_has(operation):
    simulator = Simulator()
    driver = Driver(simulator)
    wanted = setup_with(
        operation, null_offset=0.125, db_reference=-6, dbm_reference_resistance=93, limit_lower=-2, limit_upper=3
    )
    driver.apply(wanted)

    assert Driver(simulator).read_setup() == wanted


def test_read_setup_reports_no_operation_for_a_meter_that_has_math_off_with_settings_stored():
    simulator = Simulator()
    simulator.write("CALC:NULL:OFFS 0.5")

    setup = Driver(simulator).read_setup()

    assert setup.math == MathSettings(null_offset=0.5)


def test_read_setup_does_not_ask_a_function_with_no_math_about_it():
    simulator = RecordingSimulator()
    simulator.write('FUNC "DIOD"')
    simulator.commands.clear()

    setup = Driver(simulator).read_setup()

    assert setup.math == MathSettings()
    assert not [command for command in simulator.commands if command.startswith("CALC")]


@pytest.mark.parametrize(
    ("query", "answer"),
    [
        ("CALC:FUNC?", "SQUARE"),
        ("CALC:STAT?", "MAYBE"),
        ("CALC:DBM:REF?", "+1.00000000E+01"),
        ("CALC:LIM:UPP?", "nan"),
        ("CALC:NULL:OFFS?", "lots"),
    ],
)
def test_read_setup_rejects_math_answers_that_are_not_what_the_meter_has(query, answer):
    simulator = WrongAnswers(query, answer)
    simulator.write("CALC:STAT ON")

    with pytest.raises(MalformedReplyError):
        Driver(simulator).read_setup()


def test_read_setup_rejects_decibels_reported_for_a_function_that_has_none():
    simulator = WrongAnswers("CALC:FUNC?", "DBM")
    simulator.write('FUNC "CURR"')
    simulator.write("CALC:STAT ON")

    with pytest.raises(MalformedReplyError, match="dBm"):
        Driver(simulator).read_setup()


def test_fetching_statistics_asks_the_meter_for_its_own():
    simulator = Simulator()
    driver = Driver(simulator)
    driver.apply(setup_with(MathOperation.STATISTICS))
    for volts in (1.0, 2.0, 6.0):
        simulator.set_signal(Function.DC_VOLTAGE, volts)
        driver.read()

    assert driver.fetch_statistics() == MeterStatistics(minimum=1.0, maximum=6.0, average=3.0, count=3)


def test_resetting_statistics_starts_them_again_without_changing_the_operation():
    simulator = Simulator()
    driver = Driver(simulator)
    driver.apply(setup_with(MathOperation.STATISTICS))
    driver.read()
    driver.read()

    errors = driver.reset_statistics()

    assert errors == []
    assert driver.fetch_statistics().count == 0
    driver.read()
    assert driver.fetch_statistics().count == 1
    assert simulator.query("CALC:STAT?") == "1"


def test_statistics_can_only_be_reset_while_they_are_the_operation_in_effect():
    driver = Driver(Simulator())
    driver.apply(setup_with(MathOperation.NULL))

    with pytest.raises(ValueError, match="Statistics"):
        driver.reset_statistics()


def test_statistics_with_a_malformed_count_are_rejected():
    simulator = WrongAnswers("CALC:AVER:COUN?", "many")
    driver = Driver(simulator)

    with pytest.raises(MalformedReplyError):
        driver.fetch_statistics()
