import pytest

from agilent34401a.driver import Driver, Identity, QueuedError
from agilent34401a.errors import (
    CalibrationBlockedError,
    MalformedReplyError,
    TransportTimeoutError,
    UnrecognisedIdentityError,
)
from agilent34401a.meter import Function, Resolution, Setup
from agilent34401a.sim import AGILENT_IDENTITY, HEWLETT_PACKARD_IDENTITY, Simulator


class ScriptedTransport:
    """A Transport that answers every query with a canned reply."""

    timeout = 1.0

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.commands: list[str] = []

    def write(self, command: str) -> None:
        self.commands.append(command)

    def read(self) -> str:
        return self._reply

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_driver_identifies_a_hewlett_packard_meter():
    identity = Driver(Simulator(identity=HEWLETT_PACKARD_IDENTITY)).identify()

    assert identity == Identity(
        manufacturer="HEWLETT-PACKARD",
        model="34401A",
        serial="0",
        firmware="10-5-2",
        raw=HEWLETT_PACKARD_IDENTITY,
    )


def test_driver_identifies_an_agilent_meter_as_the_same_meter():
    identity = Driver(Simulator(identity=AGILENT_IDENTITY)).identify()

    assert identity.manufacturer == "Agilent Technologies"
    assert identity.model == "34401A"
    assert identity.serial == "MY45000001"
    assert identity.firmware == "11-5-2"


@pytest.mark.parametrize(
    "reply",
    [
        "Agilent Technologies,34410A,MY12345678,2.35-2.35-0.09-46-09",
        "Keysight Technologies,34401A,MY12345678,11-5-2",
        "Rigol Technologies,DM3058,DM3O123456789,01.01.00.02.07",
    ],
)
def test_driver_rejects_other_devices(reply):
    with pytest.raises(UnrecognisedIdentityError) as error_info:
        Driver(ScriptedTransport(reply)).identify()

    assert reply in str(error_info.value)


@pytest.mark.parametrize("reply", ["", "garbage", "HEWLETT-PACKARD,34401A"])
def test_driver_rejects_an_identity_reply_it_cannot_understand(reply):
    with pytest.raises(UnrecognisedIdentityError):
        Driver(ScriptedTransport(reply)).identify()


def test_driver_identity_check_ignores_case_and_padding():
    identity = Driver(ScriptedTransport(" hewlett-packard , 34401a ,0,10-5-2\r\n")).identify()

    assert identity.model == "34401a"


def test_driver_reads_a_dc_voltage_reading_with_its_raw_reading():
    reading = Driver(Simulator(dc_voltage=1.23457)).read()

    assert reading.function is Function.DC_VOLTAGE
    assert reading.value == pytest.approx(1.23457)
    assert reading.raw == "+1.23457000E+00"


def test_driver_reports_an_overload_reading():
    reading = Driver(Simulator(dc_voltage=-5000.0)).read()

    assert reading.is_overload is True
    assert reading.value < 0


def test_driver_asks_the_meter_for_a_reading_with_read_query():
    transport = ScriptedTransport("+1.00000000E+00")

    Driver(transport).read()

    assert transport.commands == ["READ?"]


def test_driver_rejects_a_malformed_reading():
    with pytest.raises(MalformedReplyError):
        Driver(ScriptedTransport("not a number")).read()


class RecordingTransport:
    """Passes everything through to a Simulator and remembers what was written."""

    timeout = 1.0

    def __init__(self, simulator: Simulator) -> None:
        self._simulator = simulator
        self.commands: list[str] = []

    def write(self, command: str) -> None:
        self.commands.append(command)
        self._simulator.write(command)

    def read(self) -> str:
        return self._simulator.read()

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self._simulator.clear()

    def close(self) -> None:
        self._simulator.close()


class AnswersInOrder(ScriptedTransport):
    """A Transport whose replies come from a script, one per read."""

    def __init__(self, *replies: str) -> None:
        super().__init__("")
        self._script = list(replies)

    def read(self) -> str:
        return self._script.pop(0) if self._script else self._reply


ALL_FUNCTIONS = list(Function)


def test_a_new_driver_assumes_the_setup_of_a_freshly_reset_meter():
    assert Driver(Simulator()).setup == Setup.default(Function.DC_VOLTAGE)


@pytest.mark.parametrize("function", ALL_FUNCTIONS)
def test_every_function_can_be_applied_and_read_back_from_the_meter(function):
    driver = Driver(Simulator())

    errors = driver.apply(Setup.default(function))

    assert errors == []
    assert driver.read_setup() == Setup.default(function)


@pytest.mark.parametrize(
    "setup",
    [Setup.default(function).with_range(range_value) for function in ALL_FUNCTIONS for range_value in function.ranges],
)
def test_every_range_can_be_applied_and_read_back(setup):
    driver = Driver(Simulator())

    assert driver.apply(setup) == []
    assert driver.read_setup() == setup


@pytest.mark.parametrize(
    "setup",
    [
        Setup.default(function).with_nplc(nplc)
        for function in ALL_FUNCTIONS
        if function.has_integration_time
        for nplc in (0.02, 0.2, 1, 10, 100)
    ],
)
def test_every_integration_time_can_be_applied_and_read_back(setup):
    driver = Driver(Simulator())

    assert driver.apply(setup) == []
    assert driver.read_setup() == setup


def test_applying_a_resolution_reads_back_as_the_integration_time_that_goes_with_it():
    driver = Driver(Simulator())

    driver.apply(Setup.default(Function.RESISTANCE_4W).with_resolution(Resolution.FIVE_HALF))

    assert driver.read_setup().nplc == 1


def test_apply_sends_function_then_range_then_integration_time_then_checks_the_error_queue():
    transport = RecordingTransport(Simulator())

    Driver(transport).apply(Setup.default(Function.DC_VOLTAGE).with_range(10.0).with_nplc(1))

    assert transport.commands == [
        'FUNC "VOLT:DC"',
        "VOLT:DC:RANG 10",
        "VOLT:DC:NPLC 1",
        "ZERO:AUTO ON",
        "INP:IMP:AUTO OFF",
        "SYST:ERR?",
    ]


def test_apply_switches_autorange_on_rather_than_sending_a_range():
    transport = RecordingTransport(Simulator())

    Driver(transport).apply(Setup.default(Function.AC_CURRENT))

    assert transport.commands == ['FUNC "CURR:AC"', "CURR:AC:RANG:AUTO ON", "DET:BAND 20", "SYST:ERR?"]


@pytest.mark.parametrize(
    ("function", "commands"),
    [
        (Function.CONTINUITY, ['FUNC "CONT"', "SYST:ERR?"]),
        (Function.DIODE, ['FUNC "DIOD"', "SYST:ERR?"]),
        (Function.FREQUENCY, ['FUNC "FREQ"', "FREQ:VOLT:RANG:AUTO ON", "FREQ:APER 0.1", "SYST:ERR?"]),
        (Function.PERIOD, ['FUNC "PER"', "PER:VOLT:RANG:AUTO ON", "PER:APER 0.1", "SYST:ERR?"]),
        (Function.RESISTANCE_4W, ['FUNC "FRES"', "FRES:RANG:AUTO ON", "FRES:NPLC 10", "ZERO:AUTO ON", "SYST:ERR?"]),
        (
            Function.DC_VOLTAGE_RATIO,
            ['FUNC "VOLT:DC:RAT"', "VOLT:DC:RANG:AUTO ON", "VOLT:DC:NPLC 10", "ZERO:AUTO ON", "SYST:ERR?"],
        ),
    ],
)
def test_apply_only_sends_the_settings_a_function_has(function, commands):
    transport = RecordingTransport(Simulator())

    Driver(transport).apply(Setup.default(function))

    assert transport.commands == commands


def test_apply_reports_every_error_the_meter_queued_and_stops_at_the_empty_queue():
    transport = AnswersInOrder('-222,"Data out of range"', '-113,"Undefined header"', '+0,"No error"', "unused")

    errors = Driver(transport).apply(Setup.default(Function.DIODE))

    assert errors == [QueuedError(-222, "Data out of range"), QueuedError(-113, "Undefined header")]
    assert transport.commands.count("SYST:ERR?") == 3


def test_apply_makes_the_driver_read_in_the_new_function():
    driver = Driver(Simulator(signals={Function.RESISTANCE_2W: 4700.0}))

    driver.apply(Setup.default(Function.RESISTANCE_2W))
    reading = driver.read()

    assert reading.function is Function.RESISTANCE_2W
    assert reading.value == pytest.approx(4700.0)
    assert driver.setup == Setup.default(Function.RESISTANCE_2W)


def test_read_setup_makes_the_driver_read_in_the_functions_the_meter_is_in():
    simulator = Simulator(signals={Function.PERIOD: 0.002})
    simulator.write('FUNC "PER"')
    driver = Driver(simulator)

    driver.read_setup()

    assert driver.read().function is Function.PERIOD


def test_read_setup_changes_nothing_on_the_meter():
    simulator = Simulator()
    simulator.write('FUNC "RES"')
    simulator.write("RES:RANG 1000")
    transport = RecordingTransport(simulator)

    Driver(transport).read_setup()

    assert all(command.endswith("?") for command in transport.commands)
    assert simulator.query("FUNC?") == '"RES"'
    assert simulator.query("SYST:ERR?") == '+0,"No error"'


def test_drain_errors_empties_the_meters_error_queue():
    simulator = Simulator()
    simulator.write("BOGUS")
    simulator.write("ALSO:BOGUS")
    driver = Driver(simulator)

    assert [error.code for error in driver.drain_errors()] == [-113, -113]
    assert driver.drain_errors() == []


def test_drain_errors_gives_up_after_the_most_the_meters_queue_can_hold():
    transport = ScriptedTransport('-113,"Undefined header"')

    errors = Driver(transport).drain_errors()

    assert len(errors) == 20


@pytest.mark.parametrize("reply", ["", "garbage", 'x,"y"', "+0"])
def test_drain_errors_rejects_a_reply_that_is_not_an_error_entry(reply):
    with pytest.raises(MalformedReplyError):
        Driver(ScriptedTransport(reply)).drain_errors()


@pytest.mark.parametrize("reply", ['"BOGUS"', '"VOLT:FOO"', "", "7"])
def test_read_setup_rejects_a_function_it_does_not_know(reply):
    with pytest.raises(MalformedReplyError):
        Driver(ScriptedTransport(reply)).read_setup()


@pytest.mark.parametrize("reply", ['"VOLT"', "VOLT", '"volt"', '"VOLT:DC"', ' "VOLT"\r\n'])
def test_read_setup_understands_the_forms_the_meter_may_name_dc_voltage_in(reply):
    math_off = ("0", "0", "0", "+6.00000000E+02", "0", "0")  # CALC:STAT? and the settings of the Math Operations
    setup = Driver(AnswersInOrder(reply, "1", "+1.00000000E+01", "1", "0", *math_off)).read_setup()

    assert setup.function is Function.DC_VOLTAGE


def test_read_setup_rejects_a_range_the_function_does_not_have():
    transport = AnswersInOrder('"VOLT"', "0", "+5.00000000E+00")

    with pytest.raises(MalformedReplyError, match="Range"):
        Driver(transport).read_setup()


def test_read_setup_rejects_an_integration_time_the_meter_cannot_have():
    transport = AnswersInOrder('"VOLT"', "1", "+7.00000000E+00")

    with pytest.raises(MalformedReplyError, match="Integration Time"):
        Driver(transport).read_setup()


@pytest.mark.parametrize("auto_reply", ["maybe", ""])
def test_read_setup_rejects_an_autorange_flag_it_cannot_read(auto_reply):
    with pytest.raises(MalformedReplyError):
        Driver(AnswersInOrder('"VOLT"', auto_reply)).read_setup()


def test_the_setup_the_driver_assumes_survives_an_apply_the_meter_rejected_until_it_is_read_back():
    driver = Driver(AnswersInOrder('-222,"Data out of range"', '+0,"No error"'))

    errors = driver.apply(Setup.default(Function.DIODE))

    assert len(errors) == 1
    assert driver.setup.function is Function.DIODE


@pytest.mark.parametrize("number", ["abc", "nan", ""])
def test_read_setup_rejects_a_range_or_integration_time_that_is_not_a_number(number):
    with pytest.raises(MalformedReplyError, match="not a number"):
        Driver(AnswersInOrder('"VOLT"', "0", number)).read_setup()
    with pytest.raises(MalformedReplyError, match="not a number"):
        Driver(AnswersInOrder('"VOLT"', "1", number)).read_setup()


def test_select_function_sends_only_the_function_and_checks_the_error_queue():
    transport = RecordingTransport(Simulator())

    errors = Driver(transport).select_function(Function.RESISTANCE_2W)

    assert errors == []
    assert transport.commands == ['FUNC "RES"', "SYST:ERR?"]


def test_select_function_leaves_the_settings_the_meter_keeps_for_that_function():
    simulator = Simulator()
    simulator.write('FUNC "RES"')
    simulator.write("RES:RANG 10000")
    simulator.write("RES:NPLC 100")
    simulator.write('FUNC "VOLT"')
    driver = Driver(simulator)

    driver.select_function(Function.RESISTANCE_2W)

    assert driver.read_setup() == Setup.default(Function.RESISTANCE_2W).with_range(1e4).with_nplc(100)


def test_select_function_makes_the_driver_read_in_that_function_and_reports_errors():
    driver = Driver(AnswersInOrder('-224,"Illegal parameter value"', '+0,"No error"'))

    errors = driver.select_function(Function.PERIOD)

    assert errors == [QueuedError(-224, "Illegal parameter value")]
    assert driver.setup.function is Function.PERIOD


def test_send_raw_returns_the_reply_to_a_query_and_sends_nothing_else():
    transport = RecordingTransport(Simulator(identity=AGILENT_IDENTITY))

    result = Driver(transport).send_raw("*IDN?")

    assert result.reply == AGILENT_IDENTITY
    assert result.errors == ()
    assert not result.changes_meter
    assert transport.commands == ["*IDN?"]


def test_send_raw_writes_a_command_and_checks_the_error_queue():
    transport = RecordingTransport(Simulator())

    result = Driver(transport).send_raw("VOLT:DC:NPLC 10")

    assert result.reply is None
    assert result.errors == ()
    assert result.changes_meter
    assert transport.commands == ["VOLT:DC:NPLC 10", "SYST:ERR?"]
    assert Driver(transport).read_setup().nplc == 10


def test_send_raw_reports_what_the_meter_complained_about():
    result = Driver(Simulator()).send_raw("NOTACOMMAND")

    assert [queued.code for queued in result.errors] == [-113]


def test_send_raw_of_a_command_and_a_query_together_returns_the_reply_and_checks_errors():
    transport = AnswersInOrder("+1", '+0,"No error"')

    result = Driver(transport).send_raw("VOLT:DC:NPLC 1;NPLC?")

    assert result.reply == "+1"
    assert result.changes_meter
    assert transport.commands == ["VOLT:DC:NPLC 1;NPLC?", "SYST:ERR?"]


def test_send_raw_lets_a_query_time_out_so_the_caller_can_resynchronise():
    with pytest.raises(TransportTimeoutError):
        Driver(Simulator()).send_raw("NOTAQUERY?")


@pytest.mark.parametrize("command", ["CAL:SEC:STAT OFF,HP034401", "cal:val 1", "FUNC?;:CAL:STR 'x'", "CAL?"])
def test_send_raw_never_sends_a_calibration_write_to_the_meter(command):
    transport = RecordingTransport(Simulator())

    with pytest.raises(CalibrationBlockedError):
        Driver(transport).send_raw(command)

    assert transport.commands == []


def test_send_raw_sends_a_calibration_write_when_it_is_explicitly_allowed():
    transport = RecordingTransport(Simulator())

    Driver(transport).send_raw("CAL:STR 'x'", allow_calibration=True)

    assert transport.commands[0] == "CAL:STR 'x'"


def test_send_raw_sends_a_read_only_calibration_query_without_an_override():
    transport = ScriptedTransport("+3")

    result = Driver(transport).send_raw("CAL:COUN?")

    assert result.reply == "+3"
    assert transport.commands == ["CAL:COUN?"]


@pytest.mark.parametrize("command", ["", "  ", " ; "])
def test_send_raw_refuses_an_empty_command(command):
    transport = RecordingTransport(Simulator())

    with pytest.raises(ValueError, match="Nothing to send"):
        Driver(transport).send_raw(command)

    assert transport.commands == []


@pytest.mark.parametrize("command", ["*IDN?\nFUNC?", "*RST\r\n*CLS"])
def test_send_raw_refuses_more_than_one_line(command):
    transport = RecordingTransport(Simulator())

    with pytest.raises(ValueError, match="one line"):
        Driver(transport).send_raw(command)

    assert transport.commands == []
