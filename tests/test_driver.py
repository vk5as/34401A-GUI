import pytest

from agilent34401a.driver import Driver, Identity
from agilent34401a.errors import MalformedReplyError, UnrecognisedIdentityError
from agilent34401a.meter import Function
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
    reading = Driver(Simulator(dc_voltage=1.234567)).read()

    assert reading.function is Function.DC_VOLTAGE
    assert reading.value == pytest.approx(1.234567)
    assert reading.raw == "+1.23456700E+00"


def test_driver_reports_an_overload_reading():
    reading = Driver(Simulator(dc_voltage=-500.0)).read()

    assert reading.is_overload is True
    assert reading.value < 0


def test_driver_asks_the_meter_for_a_reading_with_read_query():
    transport = ScriptedTransport("+1.00000000E+00")

    Driver(transport).read()

    assert transport.commands == ["READ?"]


def test_driver_rejects_a_malformed_reading():
    with pytest.raises(MalformedReplyError):
        Driver(ScriptedTransport("not a number")).read()
