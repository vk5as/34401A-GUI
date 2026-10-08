import sys

import pytest
from pyvisa import constants
from pyvisa.errors import VisaIOError

from agilent34401a.backend import Backend, BackendStatus
from agilent34401a.connection import ConnectionSettings, open_transport
from agilent34401a.errors import TransportError
from agilent34401a.serial_config import FlowControl, Framing, Parity, SerialSettings, Terminator
from agilent34401a.transport import LocalControl, RemoteControl, Transport
from agilent34401a.visa import VisaTransport, apply_serial_settings, open_serial_transport
from tests.test_visa import FakeManager, FakeResource


def test_connection_settings_name_the_serial_resource_when_a_port_is_given():
    settings = ConnectionSettings(serial=SerialSettings(port="/dev/ttyUSB0"))

    assert settings.resource_name == "ASRL/dev/ttyUSB0::INSTR"


def test_a_raw_resource_string_cannot_be_combined_with_serial_settings():
    with pytest.raises(ValueError, match="resource"):
        ConnectionSettings(resource="ASRL1::INSTR", serial=SerialSettings(port="COM1"))


def test_opening_a_serial_connection_hands_the_serial_settings_to_the_chosen_backend():
    serial = SerialSettings(port="COM3", baud=4800)
    opened: list[tuple[str, SerialSettings]] = []
    sentinel = FakeResource()

    def open_serial(library: str, settings: SerialSettings) -> Transport:
        opened.append((library, settings))
        return VisaTransport(sentinel, FakeManager(), serial=True)

    def detect(_requested: Backend) -> dict[Backend, BackendStatus]:
        return {Backend.PYVISA_PY: BackendStatus(available=True)}

    transport = open_transport(
        ConnectionSettings(backend=Backend.PYVISA_PY, serial=serial),
        detect=detect,
        open_visa=lambda _library, _name: pytest.fail("a serial Connection is not opened as a plain resource"),
        open_serial=open_serial,
    )

    assert opened == [("@py", serial)]
    assert transport.timeout > 0


class FakeSerialResource(FakeResource):
    """A fake pyvisa serial resource: it records the attributes set on it."""

    def __init__(self) -> None:
        super().__init__()
        self.baud_rate = 9600
        self.data_bits = 8
        self.parity = constants.Parity.none
        self.stop_bits = constants.StopBits.one
        self.flow_control = constants.ControlFlow.none
        self.read_termination: str | None = None
        self.write_termination = ""
        self.attributes: dict[constants.ResourceAttribute, object] = {}
        self.attribute_calls: list[tuple[constants.ResourceAttribute, object]] = []
        self.break_error: Exception | None = None

    def set_visa_attribute(self, name: constants.ResourceAttribute, state: object) -> None:
        if name is constants.ResourceAttribute.asrl_break_state and self.break_error is not None:
            raise self.break_error
        self.attributes[name] = state
        self.attribute_calls.append((name, state))


def test_serial_settings_are_applied_to_the_visa_resource():
    resource = FakeSerialResource()
    settings = SerialSettings(
        port="COM3",
        baud=2400,
        framing=Framing(7, Parity.EVEN, 2),
        flow_control=FlowControl.XON_XOFF,
        terminator=Terminator.CRLF,
    )

    apply_serial_settings(resource, settings)

    assert resource.baud_rate == 2400
    assert resource.data_bits == 7
    assert resource.parity is constants.Parity.even
    assert resource.stop_bits is constants.StopBits.two
    assert resource.flow_control is constants.ControlFlow.xon_xoff
    assert resource.write_termination == "\r\n"
    assert resource.read_termination == "\n"  # the Meter ends every reply with CR LF; the CR is stripped on read


@pytest.mark.parametrize(
    ("flow", "expected"),
    [
        (FlowControl.NONE, constants.ControlFlow.none),
        (FlowControl.XON_XOFF, constants.ControlFlow.xon_xoff),
        (FlowControl.RTS_CTS, constants.ControlFlow.rts_cts),
        (FlowControl.DTR_DSR, constants.ControlFlow.dtr_dsr),
    ],
)
def test_each_flow_control_maps_to_its_visa_setting(flow, expected):
    resource = FakeSerialResource()

    apply_serial_settings(resource, SerialSettings(port="COM1", flow_control=flow))

    assert resource.flow_control is expected


@pytest.mark.parametrize(
    ("parity", "expected"),
    [(Parity.NONE, constants.Parity.none), (Parity.EVEN, constants.Parity.even), (Parity.ODD, constants.Parity.odd)],
)
def test_each_parity_maps_to_its_visa_setting(parity, expected):
    resource = FakeSerialResource()

    apply_serial_settings(resource, SerialSettings(port="COM1", framing=Framing(7, parity, 1)))

    assert resource.parity is expected


def test_dtr_and_rts_are_left_alone_unless_the_user_overrides_them():
    resource = FakeSerialResource()

    apply_serial_settings(resource, SerialSettings(port="COM1"))

    assert resource.attribute_calls == []


def test_manual_dtr_and_rts_overrides_assert_or_unassert_the_lines():
    resource = FakeSerialResource()

    apply_serial_settings(resource, SerialSettings(port="COM1", dtr=True, rts=False))

    assert resource.attributes == {
        constants.ResourceAttribute.asrl_dtr_state: constants.LineState.asserted,
        constants.ResourceAttribute.asrl_rts_state: constants.LineState.unasserted,
    }


def test_a_serial_port_that_does_not_exist_is_a_transport_error_that_names_it():
    port = "COM255" if sys.platform == "win32" else "/dev/no-such-serial-port"

    with pytest.raises(TransportError, match="Could not open"):
        open_serial_transport("@py", SerialSettings(port=port))


def test_a_serial_transport_puts_the_meter_in_remote_with_scpi():
    resource = FakeSerialResource()
    transport = VisaTransport(resource, FakeManager(), serial=True)

    assert isinstance(transport, RemoteControl)
    transport.go_to_remote()

    assert resource.written == ["SYST:REM"]


def test_a_serial_transport_returns_the_meter_to_local_with_scpi():
    resource = FakeSerialResource()
    transport = VisaTransport(resource, FakeManager(), serial=True)

    assert isinstance(transport, LocalControl)
    transport.go_to_local()

    assert resource.written == ["SYST:LOC"]


def test_a_transport_that_is_not_serial_needs_no_remote_command():
    resource = FakeResource()
    transport = VisaTransport(resource, FakeManager(), gpib=True)

    transport.go_to_remote()

    assert resource.written == []


def test_a_closed_serial_transport_cannot_go_to_local_or_remote():
    transport = VisaTransport(FakeSerialResource(), FakeManager(), serial=True)
    transport.close()

    with pytest.raises(TransportError, match="closed"):
        transport.go_to_local()
    with pytest.raises(TransportError, match="closed"):
        transport.go_to_remote()


def test_clearing_a_serial_connection_flushes_it_and_sends_a_break():
    resource = FakeSerialResource()
    transport = VisaTransport(resource, FakeManager(), serial=True)

    transport.clear()

    assert resource.cleared == 1
    assert resource.attribute_calls == [
        (constants.ResourceAttribute.asrl_break_state, constants.LineState.asserted),
        (constants.ResourceAttribute.asrl_break_state, constants.LineState.unasserted),
    ]


def test_clearing_a_connection_that_is_not_serial_sends_no_break():
    resource = FakeSerialResource()

    VisaTransport(resource, FakeManager(), gpib=True).clear()

    assert resource.attribute_calls == []


def test_a_break_that_fails_is_a_transport_error():
    resource = FakeSerialResource()
    resource.break_error = VisaIOError(constants.StatusCode.error_connection_lost)
    transport = VisaTransport(resource, FakeManager(), serial=True)

    with pytest.raises(TransportError):
        transport.clear()
