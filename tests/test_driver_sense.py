"""The Driver's sense options: AC Filter, Gate Time, Autozero, Input Impedance and Terminals."""

import pytest

from agilent34401a.driver import Driver
from agilent34401a.errors import MalformedReplyError
from agilent34401a.meter import (
    AcFilter,
    Autozero,
    Function,
    GateTime,
    InputImpedance,
    Resolution,
    Setup,
    Terminals,
)
from agilent34401a.sim import Simulator

AUTOZERO_FUNCTIONS = [
    Function.DC_VOLTAGE,
    Function.DC_CURRENT,
    Function.RESISTANCE_2W,
    Function.RESISTANCE_4W,
    Function.DC_VOLTAGE_RATIO,
]


class Recording:
    """Passes everything through to a Simulator, remembers what was written and can answer a query wrongly."""

    timeout = 1.0

    def __init__(self, simulator: Simulator, wrong_answers: dict[str, str] | None = None) -> None:
        self._simulator = simulator
        self._wrong_answers = wrong_answers or {}
        self.commands: list[str] = []
        self._override: str | None = None

    def write(self, command: str) -> None:
        self.commands.append(command)
        self._override = self._wrong_answers.get(command)
        self._simulator.write(command)

    def read(self) -> str:
        reply = self._simulator.read()
        return reply if self._override is None else self._override

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self._simulator.clear()

    def close(self) -> None:
        self._simulator.close()


def test_apply_sends_the_ac_filter_after_the_range():
    transport = Recording(Simulator())

    Driver(transport).apply(Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.SLOW))

    assert transport.commands == ['FUNC "VOLT:AC"', "VOLT:AC:RANG:AUTO ON", "DET:BAND 3", "SYST:ERR?"]


@pytest.mark.parametrize(
    ("function", "commands"),
    [
        (Function.FREQUENCY, ['FUNC "FREQ"', "FREQ:VOLT:RANG:AUTO ON", "FREQ:APER 1", "SYST:ERR?"]),
        (Function.PERIOD, ['FUNC "PER"', "PER:VOLT:RANG:AUTO ON", "PER:APER 1", "SYST:ERR?"]),
    ],
)
def test_apply_sends_the_gate_time_in_seconds(function, commands):
    transport = Recording(Simulator())

    Driver(transport).apply(Setup.default(function).with_gate_time(GateTime.ONE_SECOND))

    assert transport.commands == commands


def test_apply_sends_autozero_and_input_impedance_after_the_integration_time():
    transport = Recording(Simulator())
    setup = (
        Setup.default(Function.DC_VOLTAGE)
        .with_autozero(Autozero.OFF)
        .with_input_impedance(InputImpedance.HIGH_IMPEDANCE)
    )

    Driver(transport).apply(setup)

    assert transport.commands == [
        'FUNC "VOLT:DC"',
        "VOLT:DC:RANG:AUTO ON",
        "VOLT:DC:NPLC 10",
        "ZERO:AUTO OFF",
        "INP:IMP:AUTO ON",
        "SYST:ERR?",
    ]


@pytest.mark.parametrize(("autozero", "word"), [(Autozero.ON, "ON"), (Autozero.OFF, "OFF"), (Autozero.ONCE, "ONCE")])
def test_apply_sends_each_autozero_the_way_the_meter_spells_it(autozero, word):
    transport = Recording(Simulator())

    Driver(transport).apply(Setup.default(Function.RESISTANCE_2W).with_autozero(autozero))

    assert f"ZERO:AUTO {word}" in transport.commands


@pytest.mark.parametrize("ac_filter", list(AcFilter))
@pytest.mark.parametrize("function", [Function.AC_VOLTAGE, Function.AC_CURRENT])
def test_every_ac_filter_can_be_applied_and_read_back(function, ac_filter):
    driver = Driver(Simulator())
    setup = Setup.default(function).with_ac_filter(ac_filter)

    assert driver.apply(setup) == []
    assert driver.read_setup() == setup


@pytest.mark.parametrize("gate_time", list(GateTime))
@pytest.mark.parametrize("function", [Function.FREQUENCY, Function.PERIOD])
def test_every_gate_time_can_be_applied_and_read_back_with_the_resolution_that_goes_with_it(function, gate_time):
    driver = Driver(Simulator())
    setup = Setup.default(function).with_gate_time(gate_time)

    assert driver.apply(setup) == []
    read_back = driver.read_setup()
    assert read_back == setup
    assert read_back.resolution is gate_time.resolution


@pytest.mark.parametrize("autozero", [Autozero.ON, Autozero.OFF])
@pytest.mark.parametrize("function", AUTOZERO_FUNCTIONS)
def test_autozero_on_and_off_can_be_applied_and_read_back(function, autozero):
    driver = Driver(Simulator())
    setup = Setup.default(function).with_autozero(autozero)

    assert driver.apply(setup) == []
    assert driver.read_setup() == setup


def test_autozero_once_is_read_back_as_off_because_the_meter_leaves_it_off():
    driver = Driver(Simulator())
    setup = Setup.default(Function.DC_VOLTAGE)

    assert driver.apply(setup.with_autozero(Autozero.ONCE)) == []

    assert driver.read_setup() == setup.with_autozero(Autozero.OFF)


@pytest.mark.parametrize("impedance", list(InputImpedance))
def test_every_input_impedance_can_be_applied_and_read_back(impedance):
    driver = Driver(Simulator())
    setup = Setup.default(Function.DC_VOLTAGE).with_input_impedance(impedance)

    assert driver.apply(setup) == []
    assert driver.read_setup() == setup


def test_a_meter_with_options_already_set_has_them_read_back_on_connect():
    simulator = Simulator()
    for command in ('FUNC "VOLT:AC"', "DET:BAND 200"):
        simulator.write(command)

    setup = Driver(simulator).read_setup()

    assert setup.ac_filter is AcFilter.FAST


def test_read_setup_asks_for_the_options_of_the_function_only_and_changes_nothing():
    simulator = Simulator()
    simulator.write('FUNC "FREQ"')
    transport = Recording(simulator)

    Driver(transport).read_setup()

    assert all(command.endswith("?") for command in transport.commands)
    assert "FREQ:APER?" in transport.commands
    assert not any(command in transport.commands for command in ("DET:BAND?", "ZERO:AUTO?", "INP:IMP:AUTO?"))


def test_selecting_a_function_keeps_the_options_the_meter_has_for_it():
    simulator = Simulator()
    simulator.write("FREQ:APER 1")
    driver = Driver(simulator)

    assert driver.select_function(Function.FREQUENCY) == []

    assert driver.read_setup().gate_time is GateTime.ONE_SECOND


def test_a_reading_has_the_digits_the_gate_time_gives():
    driver = Driver(Simulator(signals={Function.FREQUENCY: 1234.56789}))

    driver.apply(Setup.default(Function.FREQUENCY).with_resolution(Resolution.FOUR_HALF))

    assert driver.read().value == pytest.approx(1234.6)


@pytest.mark.parametrize(
    ("function", "query", "reply"),
    [
        (Function.AC_VOLTAGE, "DET:BAND?", "+1.70000000E+01"),
        (Function.AC_VOLTAGE, "DET:BAND?", "fast"),
        (Function.FREQUENCY, "FREQ:APER?", "+5.00000000E-02"),
        (Function.PERIOD, "PER:APER?", "garbage"),
        (Function.DC_VOLTAGE, "ZERO:AUTO?", "maybe"),
        (Function.DC_VOLTAGE, "INP:IMP:AUTO?", "2"),
    ],
)
def test_read_setup_rejects_an_option_the_meter_does_not_have(function, query, reply):
    simulator = Simulator()
    simulator.write(f'FUNC "{function.value}"')

    with pytest.raises(MalformedReplyError):
        Driver(Recording(simulator, {query: reply})).read_setup()


@pytest.mark.parametrize(("word", "terminals"), [("FRON", Terminals.FRONT), ("REAR", Terminals.REAR)])
def test_the_terminals_are_read_from_the_meter(word, terminals):
    driver = Driver(Recording(Simulator(), {"ROUT:TERM?": word}))

    assert driver.read_terminals() is terminals
    assert driver.terminals is terminals


def test_the_terminals_of_the_simulator_are_front_until_the_switch_moves():
    simulator = Simulator()
    driver = Driver(simulator)
    assert driver.read_terminals() is Terminals.FRONT

    simulator.terminals = Terminals.REAR

    assert driver.terminals is Terminals.FRONT  # what it last read
    assert driver.read_terminals() is Terminals.REAR


@pytest.mark.parametrize("reply", ["", "FRONT", "5", "garbage"])
def test_a_terminals_reply_the_meter_does_not_give_is_rejected(reply):
    with pytest.raises(MalformedReplyError):
        Driver(Recording(Simulator(), {"ROUT:TERM?": reply})).read_terminals()
