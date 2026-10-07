import pytest

from agilent34401a.backend import CONCRETE_BACKENDS, Backend, BackendStatus, detect_backends, resolve_backend
from agilent34401a.connection import ConnectionSettings, open_transport
from agilent34401a.errors import BackendUnavailableError, TransportError
from agilent34401a.sim import Simulator

AVAILABLE = BackendStatus(available=True)
MISSING_VENDOR = BackendStatus(
    available=False, reason="Could not find a VISA library (is Keysight IO Libraries installed?)"
)
MISSING_PY = BackendStatus(available=False, reason="pyvisa-py is not importable")


def statuses(vendor: BackendStatus, py: BackendStatus = AVAILABLE) -> dict[Backend, BackendStatus]:
    return {Backend.VENDOR: vendor, Backend.PYVISA_PY: py}


def test_gpib_resource_defaults_to_board_0_address_22():
    assert ConnectionSettings().resource_name == "GPIB0::22::INSTR"


def test_gpib_board_and_address_choose_the_resource():
    assert ConnectionSettings(gpib_board=1, gpib_address=7).resource_name == "GPIB1::7::INSTR"


def test_a_resource_string_that_is_empty_after_all_is_never_mistaken_for_the_gpib_default():
    with pytest.raises(ValueError, match="resource"):
        ConnectionSettings(resource="")


def test_a_raw_resource_string_overrides_the_gpib_settings():
    settings = ConnectionSettings(resource="TCPIP::10.0.0.5::5025::SOCKET", gpib_board=3, gpib_address=9)

    assert settings.resource_name == "TCPIP::10.0.0.5::5025::SOCKET"


@pytest.mark.parametrize(("board", "address"), [(-1, 22), (0, -1), (0, 31)])
def test_gpib_settings_outside_what_the_bus_allows_are_rejected(board, address):
    with pytest.raises(ValueError, match="GPIB"):
        ConnectionSettings(gpib_board=board, gpib_address=address)


def test_a_blank_resource_string_is_rejected():
    with pytest.raises(ValueError, match="resource"):
        ConnectionSettings(resource="  ")


def test_auto_prefers_vendor_visa_when_it_loads():
    assert resolve_backend(Backend.AUTO, statuses(AVAILABLE)) is Backend.VENDOR


def test_auto_falls_back_to_pyvisa_py_when_vendor_visa_is_missing():
    assert resolve_backend(Backend.AUTO, statuses(MISSING_VENDOR)) is Backend.PYVISA_PY


def test_auto_with_nothing_available_explains_why_for_each_backend():
    with pytest.raises(BackendUnavailableError) as error_info:
        resolve_backend(Backend.AUTO, statuses(MISSING_VENDOR, MISSING_PY))

    message = str(error_info.value)
    assert "Keysight IO Libraries" in message
    assert "pyvisa-py is not importable" in message


def test_choosing_vendor_visa_when_it_is_missing_reports_the_reason_instead_of_falling_back():
    with pytest.raises(BackendUnavailableError, match="Keysight IO Libraries"):
        resolve_backend(Backend.VENDOR, statuses(MISSING_VENDOR))


def test_choosing_pyvisa_py_ignores_vendor_visa():
    assert resolve_backend(Backend.PYVISA_PY, statuses(AVAILABLE)) is Backend.PYVISA_PY


def test_choosing_pyvisa_py_when_it_is_missing_reports_the_reason():
    with pytest.raises(BackendUnavailableError, match="not importable"):
        resolve_backend(Backend.PYVISA_PY, statuses(AVAILABLE, MISSING_PY))


def test_backend_unavailable_is_a_transport_error():
    assert issubclass(BackendUnavailableError, TransportError)


def test_every_backend_has_a_visa_library_name_and_a_label():
    assert Backend.VENDOR.library == "@ivi"
    assert Backend.PYVISA_PY.library == "@py"
    assert len({backend.label for backend in Backend}) == 3


def test_detecting_for_auto_checks_each_concrete_backend_in_order_of_preference():
    asked = []

    def check(library: str) -> BackendStatus:
        asked.append(library)
        return AVAILABLE if library == "@py" else MISSING_VENDOR

    found = detect_backends(check)

    assert asked == ["@ivi", "@py"]
    assert found == statuses(MISSING_VENDOR)


@pytest.mark.parametrize(("requested", "library"), [(Backend.VENDOR, "@ivi"), (Backend.PYVISA_PY, "@py")])
def test_detecting_for_an_explicit_backend_loads_only_that_library(requested, library):
    asked = []

    def check(name: str) -> BackendStatus:
        asked.append(name)
        return AVAILABLE

    found = detect_backends(check, requested)

    assert asked == [library]
    assert list(found) == [requested]


def test_the_concrete_backends_are_vendor_then_pyvisa_py():
    assert CONCRETE_BACKENDS == (Backend.VENDOR, Backend.PYVISA_PY)


def test_auto_has_no_library_of_its_own():
    with pytest.raises(ValueError, match="Auto"):
        _ = Backend.AUTO.library


def test_opening_with_an_explicit_backend_only_asks_about_that_backend():
    asked = []

    def detect(requested: Backend) -> dict[Backend, BackendStatus]:
        asked.append(requested)
        return {requested: AVAILABLE}

    open_transport(ConnectionSettings(backend=Backend.PYVISA_PY), detect=detect, open_visa=OpenRecorder())

    assert asked == [Backend.PYVISA_PY]


class OpenRecorder:
    """Stands in for pyvisa: remembers what it was asked to open."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.transport = Simulator()

    def __call__(self, library: str, resource_name: str) -> Simulator:
        self.calls.append((library, resource_name))
        return self.transport


def test_opening_a_connection_uses_the_resolved_backend_and_the_resource():
    opener = OpenRecorder()

    transport = open_transport(
        ConnectionSettings(backend=Backend.AUTO, gpib_address=5),
        detect=lambda _requested: statuses(AVAILABLE),
        open_visa=opener,
    )

    assert transport is opener.transport
    assert opener.calls == [("@ivi", "GPIB0::5::INSTR")]


def test_auto_opens_through_pyvisa_py_when_vendor_visa_is_missing():
    opener = OpenRecorder()

    open_transport(ConnectionSettings(), detect=lambda _requested: statuses(MISSING_VENDOR), open_visa=opener)

    assert opener.calls == [("@py", "GPIB0::22::INSTR")]


def test_an_explicit_backend_that_is_missing_never_reaches_pyvisa():
    opener = OpenRecorder()

    with pytest.raises(BackendUnavailableError):
        open_transport(
            ConnectionSettings(backend=Backend.VENDOR),
            detect=lambda _requested: statuses(MISSING_VENDOR),
            open_visa=opener,
        )

    assert opener.calls == []
