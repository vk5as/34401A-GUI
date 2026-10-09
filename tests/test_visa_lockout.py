import pytest
from pyvisa import constants
from pyvisa.resources import GPIBInstrument

from agilent34401a.driver import Driver
from agilent34401a.errors import TransportError
from agilent34401a.transport import BusLockout
from agilent34401a.visa import VisaTransport
from tests.test_visa import FakeManager, FakeResource


class GpibResource(FakeResource):
    """A fake GPIB resource that records its REN line operations."""

    def __init__(self) -> None:
        super().__init__()
        self.ren_operations: list[constants.RENLineOperation] = []

    def control_ren(self, mode: constants.RENLineOperation) -> None:
        """What pyvisa's GPIBInstrument calls it: the line lives on the resource, `gpib_control_ren` on the library."""
        self.ren_operations.append(mode)


def test_a_gpib_transport_locks_out_the_front_panel_with_a_bus_message():
    resource = GpibResource()
    transport = VisaTransport(resource, FakeManager(), gpib=True)

    assert isinstance(transport, BusLockout)
    assert transport.set_local_lockout(locked=True) is True
    assert resource.ren_operations == [constants.RENLineOperation.asrt_address_llo]
    assert resource.written == []


def test_a_gpib_transport_releases_the_lockout_and_puts_the_meter_back_in_remote():
    resource = GpibResource()
    transport = VisaTransport(resource, FakeManager(), gpib=True)

    assert transport.set_local_lockout(locked=False) is True

    assert resource.ren_operations == [constants.RENLineOperation.deassert_gtl, constants.RENLineOperation.asrt]


def test_a_transport_that_is_not_gpib_cannot_send_the_bus_message_so_the_driver_uses_scpi():
    resource = FakeResource()
    resource.replies = ['+0,"No error"']
    transport = VisaTransport(resource, FakeManager())

    assert transport.set_local_lockout(locked=True) is False

    Driver(transport).lock_front_panel()
    assert resource.written == ["SYST:RWL", "SYST:ERR?"]


def test_the_fake_resource_has_the_ren_method_pyvisas_gpib_resource_really_has():
    assert callable(GPIBInstrument.control_ren)
    assert not hasattr(GPIBInstrument, "gpib_control_ren")  # that one is on the VISA library, not on the resource


def test_a_gpib_resource_without_ren_control_is_an_error_not_a_scpi_command_the_meter_would_reject():
    resource = FakeResource()  # no control_ren
    transport = VisaTransport(resource, FakeManager(), gpib=True)

    with pytest.raises(TransportError, match="REN"):
        transport.set_local_lockout(locked=True)

    assert resource.written == []
