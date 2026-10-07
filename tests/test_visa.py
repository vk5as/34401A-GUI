from typing import TYPE_CHECKING

import pytest
from pyvisa import constants
from pyvisa.errors import VisaIOError

from agilent34401a.errors import BackendUnavailableError, TransportError, TransportTimeoutError
from agilent34401a.visa import VisaTransport, check_library, open_visa_transport

if TYPE_CHECKING:
    from collections.abc import Callable

    from agilent34401a.transport import Transport


class FakeResource:
    """Just enough of a pyvisa message-based resource."""

    def __init__(self) -> None:
        self.timeout = 2000.0
        self.written: list[str] = []
        self.replies: list[str | Exception] = []
        self.cleared = 0
        self.closed = 0
        self.fail_on: dict[str, Exception] = {}

    def write(self, message: str) -> int:
        if "write" in self.fail_on:
            raise self.fail_on["write"]
        self.written.append(message)
        return len(message)

    def read(self) -> str:
        if "read" in self.fail_on:
            raise self.fail_on["read"]
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def clear(self) -> None:
        if "clear" in self.fail_on:
            raise self.fail_on["clear"]
        self.cleared += 1

    def close(self) -> None:
        self.closed += 1


class FakeManager:
    def __init__(self) -> None:
        self.closed = 0
        self.open_resource: Callable[[str], object] = lambda _name: FakeResource()

    def close(self) -> None:
        self.closed += 1


def make() -> tuple[VisaTransport, FakeResource, FakeManager]:
    resource, manager = FakeResource(), FakeManager()
    return VisaTransport(resource, manager), resource, manager


def test_visa_transport_is_a_transport():
    transport: Transport = make()[0]

    assert transport.timeout > 0


def test_write_read_and_query_pass_text_through():
    transport, resource, _ = make()
    resource.replies = ["+1.00000000E+00\n", "+2.00000000E+00"]

    assert transport.query("READ?") == "+1.00000000E+00"
    transport.write("*CLS")
    assert transport.read() == "+2.00000000E+00"
    assert resource.written == ["READ?", "*CLS"]


def test_timeout_is_in_seconds_but_pyvisa_counts_milliseconds():
    transport, resource, _ = make()

    transport.timeout = 31.5

    assert resource.timeout == 31500
    assert transport.timeout == 31.5


def test_new_transport_applies_the_default_timeout():
    transport, resource, _ = make()

    assert resource.timeout == 2000
    assert transport.timeout == 2.0


def test_a_visa_timeout_becomes_a_transport_timeout():
    transport, resource, _ = make()
    resource.replies = [VisaIOError(constants.StatusCode.error_timeout)]

    with pytest.raises(TransportTimeoutError, match=r"[Tt]imed out"):
        transport.read()


@pytest.mark.parametrize("operation", ["write", "read", "clear"])
def test_any_other_visa_error_becomes_a_transport_error(operation):
    transport, resource, _ = make()
    resource.fail_on[operation] = VisaIOError(constants.StatusCode.error_connection_lost)
    calls = {"write": lambda: transport.write("X"), "read": transport.read, "clear": transport.clear}

    with pytest.raises(TransportError) as error_info:
        calls[operation]()

    assert not isinstance(error_info.value, TransportTimeoutError)


def test_a_timeout_while_writing_is_also_a_transport_timeout():
    transport, resource, _ = make()
    resource.fail_on["write"] = VisaIOError(constants.StatusCode.error_timeout)

    with pytest.raises(TransportTimeoutError):
        transport.write("X")


def test_clear_sends_a_device_clear():
    transport, resource, _ = make()

    transport.clear()

    assert resource.cleared == 1


def test_close_releases_the_resource_and_the_manager_once():
    transport, resource, manager = make()

    transport.close()
    transport.close()

    assert (resource.closed, manager.closed) == (1, 1)


def test_a_closed_transport_refuses_every_operation():
    transport, _, _ = make()
    transport.close()

    for operation in (lambda: transport.write("X"), transport.read, transport.clear, lambda: transport.query("X")):
        with pytest.raises(TransportError, match="closed"):
            operation()


def test_close_survives_a_resource_that_fails_to_close():
    class FailsToClose(FakeResource):
        def close(self) -> None:
            raise VisaIOError(constants.StatusCode.error_connection_lost)

    manager = FakeManager()
    transport = VisaTransport(FailsToClose(), manager)

    transport.close()

    assert manager.closed == 1


@pytest.mark.parametrize("operation", ["write", "read", "clear"])
def test_a_refused_or_dropped_socket_becomes_a_transport_error(operation):
    # pyvisa-py's socket sessions raise plain OSErrors rather than VisaIOError.
    transport, resource, _ = make()
    resource.fail_on[operation] = ConnectionRefusedError(111, "Connection refused")
    calls = {"write": lambda: transport.write("X"), "read": transport.read, "clear": transport.clear}

    with pytest.raises(TransportError, match="refused") as error_info:
        calls[operation]()

    assert not isinstance(error_info.value, TransportTimeoutError)


def test_a_socket_timeout_becomes_a_transport_timeout():
    transport, resource, _ = make()
    resource.fail_on["read"] = TimeoutError("timed out")

    with pytest.raises(TransportTimeoutError):
        transport.read()


def test_pyvisa_py_is_reported_available():
    status = check_library("@py")

    assert status.available is True
    assert status.reason == ""


def test_an_unknown_library_is_reported_unavailable_with_a_reason():
    status = check_library("@no-such-backend")

    assert status.available is False
    assert status.reason


def test_a_missing_vendor_library_says_what_to_install(monkeypatch):
    monkeypatch.setattr(
        "pyvisa.ResourceManager", lambda _library: (_ for _ in ()).throw(OSError("Could not open VISA library:"))
    )

    status = check_library("@ivi")

    assert status.available is False
    assert "Could not open VISA library" in status.reason
    assert "Keysight IO Libraries" in status.reason


def test_opening_through_an_unavailable_library_is_reported_as_the_backend_being_unavailable():
    with pytest.raises(BackendUnavailableError):
        open_visa_transport("@no-such-backend", "GPIB0::22::INSTR")


def _open_and_query(resource_name: str) -> None:
    transport = open_visa_transport("@py", resource_name)
    try:
        transport.query("*IDN?")
    finally:
        transport.close()


def test_a_socket_nobody_listens_on_is_a_transport_error(unused_port):
    # pyvisa-py connects lazily on Linux (the refusal surfaces on the first command) but eagerly on Windows
    # (opening fails), so either step may be the one that raises.
    with pytest.raises(TransportError):
        _open_and_query(f"TCPIP::127.0.0.1::{unused_port}::SOCKET")


class PlainResource:
    """A VISA resource that is not message-based, such as a raw memory-mapped one."""

    closed = False

    def close(self) -> None:
        self.closed = True


def _manager_opening(monkeypatch: pytest.MonkeyPatch, opener) -> FakeManager:
    manager = FakeManager()
    manager.open_resource = opener
    monkeypatch.setattr("pyvisa.ResourceManager", lambda _library: manager)
    return manager


def test_a_resource_pyvisa_cannot_open_is_a_transport_error_that_names_it(monkeypatch):
    def refuse(_name: str):
        raise VisaIOError(constants.StatusCode.error_resource_not_found)

    manager = _manager_opening(monkeypatch, refuse)

    with pytest.raises(TransportError, match="Could not open GPIB0::22::INSTR"):
        open_visa_transport("@py", "GPIB0::22::INSTR")

    assert manager.closed == 1


def test_a_malformed_resource_string_is_a_transport_error(monkeypatch):
    def refuse(_name: str):
        message = "invalid resource"
        raise ValueError(message)

    _manager_opening(monkeypatch, refuse)

    with pytest.raises(TransportError, match="Could not open nonsense"):
        open_visa_transport("@py", "nonsense")


def test_a_resource_that_does_not_take_text_commands_is_refused_and_released(monkeypatch):
    plain = PlainResource()
    manager = _manager_opening(monkeypatch, lambda _name: plain)

    with pytest.raises(TransportError, match="text commands"):
        open_visa_transport("@py", "VXI0::1::INSTR")

    assert plain.closed is True
    assert manager.closed == 1


@pytest.mark.parametrize(
    "error",
    [
        Exception("could not connect: [Errno -2] Name or service not known"),
        OSError("no route to host"),
        RuntimeError("boom"),
    ],
)
def test_whatever_pyvisa_raises_while_opening_becomes_a_transport_error_and_nothing_leaks(monkeypatch, error):
    def explode(_name: str):
        raise error

    manager = _manager_opening(monkeypatch, explode)

    with pytest.raises(TransportError, match="Could not open TCPIP::nosuch::5025::SOCKET"):
        open_visa_transport("@py", "TCPIP::nosuch::5025::SOCKET")

    assert manager.closed == 1


def test_a_missing_vendor_library_gets_a_well_formed_reason_with_the_cause_and_a_hint(monkeypatch):
    message = "Could not open VISA library:\nlibvisa.so.0: cannot open shared object file\n"
    monkeypatch.setattr("pyvisa.ResourceManager", lambda _library: (_ for _ in ()).throw(OSError(message)))

    status = check_library("@ivi")

    assert status.reason == (
        "Could not open VISA library; libvisa.so.0: cannot open shared object file. "
        "Install Keysight IO Libraries Suite (or NI-VISA) to use this Backend."
    )


def test_a_library_that_fails_without_a_message_still_gives_a_reason(monkeypatch):
    monkeypatch.setattr("pyvisa.ResourceManager", lambda _library: (_ for _ in ()).throw(ImportError()))

    assert check_library("@py").reason == "ImportError"


def test_close_still_releases_the_manager_when_the_resource_fails_with_an_os_error():
    class Breaks(FakeResource):
        def close(self) -> None:
            message = "socket already gone"
            raise OSError(message)

    manager = FakeManager()
    transport = VisaTransport(Breaks(), manager)

    transport.close()

    assert manager.closed == 1


def test_timeouts_are_whole_milliseconds():
    transport, resource, _ = make()

    transport.timeout = 0.1 + 0.2

    assert resource.timeout == 300
    assert isinstance(resource.timeout, int)
