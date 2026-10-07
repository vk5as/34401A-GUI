from pyvisa import constants

from agilent34401a.driver import Driver
from agilent34401a.transport import BusLockout
from agilent34401a.visa import VisaTransport
from tests.test_visa import FakeManager, FakeResource


class GpibResource(FakeResource):
    """A fake GPIB resource that records its REN line operations."""

    def __init__(self) -> None:
        super().__init__()
        self.ren_operations: list[constants.RENLineOperation] = []

    def gpib_control_ren(self, mode: constants.RENLineOperation) -> None:
        self.ren_operations.append(mode)


def test_a_gpib_transport_locks_out_the_front_panel_with_a_bus_message():
    resource = GpibResource()
    transport = VisaTransport(resource, FakeManager(), gpib=True)

    assert isinstance(transport, BusLockout)
    assert transport.set_local_lockout(locked=True) is True
    assert resource.ren_operations == [constants.RENLineOperation.asrt_llo]
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
