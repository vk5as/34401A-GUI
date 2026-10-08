import threading
import time
import tkinter as tk
from collections.abc import Callable, Iterator, Mapping

import pytest

from agilent34401a.backend import Backend, BackendStatus
from agilent34401a.connection import BackendScan, ConnectionSettings
from agilent34401a.gui.connection_dialog import ConnectionDialog
from agilent34401a.settings import LastConnection

TIMEOUT_S = 10.0
NO_VENDOR = BackendStatus(available=False, reason="Could not open VISA library: libvisa.so.0 is missing")
AVAILABLE = BackendStatus(available=True)


def default_detect() -> Mapping[Backend, BackendStatus]:
    return {Backend.VENDOR: NO_VENDOR, Backend.PYVISA_PY: AVAILABLE}


def default_scan(_requested: Backend) -> Mapping[Backend, BackendScan]:
    return {Backend.PYVISA_PY: BackendScan(resources=("GPIB0::22::INSTR", "ASRL/dev/ttyUSB0::INSTR"))}


class Chosen:
    """Collects what the dialog hands over when the user connects."""

    def __init__(self) -> None:
        self.calls: list[tuple[LastConnection, bool]] = []

    def __call__(self, choice: LastConnection, auto_reconnect: bool) -> None:
        self.calls.append((choice, auto_reconnect))


def pump(root: tk.Misc, until: Callable[[], bool], timeout: float = TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    while not until():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting for the dialog")
        root.update()
        time.sleep(0.002)


@pytest.fixture
def open_dialog(tk_root: tk.Tk) -> Iterator[Callable[..., tuple[ConnectionDialog, Chosen]]]:
    dialogs: list[ConnectionDialog] = []
    parent = tk.Toplevel(tk_root)  # shown, as the main window is: events only reach widgets that are mapped
    parent.update()

    def make(
        *,
        detect: Callable[[], Mapping[Backend, BackendStatus]] = default_detect,
        scan: Callable[[Backend], Mapping[Backend, BackendScan]] = default_scan,
        initial: LastConnection | None = None,
        auto_reconnect: bool = False,
        settle: bool = True,
    ) -> tuple[ConnectionDialog, Chosen]:
        chosen = Chosen()
        dialog = ConnectionDialog(
            parent,
            detect=detect,
            scan=scan,
            on_connect=chosen,
            initial=initial,
            auto_reconnect=auto_reconnect,
        )
        dialogs.append(dialog)
        if settle:
            pump(tk_root, lambda: dialog.detected)
        return dialog, chosen

    yield make
    for dialog in dialogs:
        dialog.close()
    parent.destroy()


def pick_backend(dialog: ConnectionDialog, backend: Backend) -> None:
    dialog.backend_box.current(list(Backend).index(backend))
    dialog.backend_box.event_generate("<<ComboboxSelected>>")
    dialog.window.update()


def test_the_backend_dropdown_offers_auto_vendor_visa_and_pyvisa_py_with_what_was_detected(open_dialog):
    dialog, _ = open_dialog()

    assert list(dialog.backend_box.cget("values")) == [
        "Auto",
        "Keysight/NI VISA (not available)",
        "pyvisa-py (available)",
    ]
    assert dialog.backend_box.get() == "Auto"


def test_choosing_a_backend_that_is_not_available_shows_why(open_dialog):
    dialog, _ = open_dialog()

    pick_backend(dialog, Backend.VENDOR)

    assert "libvisa.so.0 is missing" in dialog.backend_status.cget("text")


def test_choosing_an_available_backend_says_it_was_detected(open_dialog):
    dialog, _ = open_dialog()

    pick_backend(dialog, Backend.PYVISA_PY)

    assert dialog.backend_status.cget("text") == "pyvisa-py was detected."


def test_auto_says_which_backend_it_will_use(open_dialog):
    dialog, _ = open_dialog()

    assert dialog.backend_status.cget("text") == "Auto will use pyvisa-py."


def test_auto_with_no_backend_available_says_why_for_each(open_dialog):
    def nothing() -> dict[Backend, BackendStatus]:
        return {
            Backend.VENDOR: NO_VENDOR,
            Backend.PYVISA_PY: BackendStatus(available=False, reason="pyvisa-py is not importable"),
        }

    dialog, _ = open_dialog(detect=nothing)

    text = dialog.backend_status.cget("text")
    assert "libvisa.so.0 is missing" in text
    assert "pyvisa-py is not importable" in text


def test_a_detection_that_fails_unexpectedly_is_reported_and_the_dialog_still_works(open_dialog):
    def explode() -> dict[Backend, BackendStatus]:
        message = "boom"
        raise RuntimeError(message)

    dialog, chosen = open_dialog(detect=explode)

    assert "boom" in dialog.backend_status.cget("text")
    dialog.connect_button.invoke()
    assert len(chosen.calls) == 1


def test_a_slow_detection_does_not_freeze_the_dialog(open_dialog, tk_root):
    release = threading.Event()

    def slow() -> Mapping[Backend, BackendStatus]:
        release.wait(TIMEOUT_S)
        return default_detect()

    try:
        dialog, _ = open_dialog(detect=slow, settle=False)
        tk_root.update()
        assert dialog.backend_status.cget("text") == "Checking which Backends are installed…"
        assert not dialog.detected
    finally:
        release.set()
    pump(tk_root, lambda: dialog.detected)

    assert dialog.backend_status.cget("text") == "Auto will use pyvisa-py."


def test_the_gpib_board_and_address_default_to_board_0_address_22(open_dialog):
    dialog, chosen = open_dialog()

    dialog.connect_button.invoke()

    [(choice, _)] = chosen.calls
    assert choice == LastConnection(simulate=False, connection=ConnectionSettings())
    assert choice.connection.resource_name == "GPIB0::22::INSTR"


def test_the_board_and_address_chosen_are_connected_to(open_dialog):
    dialog, chosen = open_dialog()

    dialog.board_box.set("1")
    dialog.address_box.set("7")
    pick_backend(dialog, Backend.PYVISA_PY)
    dialog.connect_button.invoke()

    [(choice, _)] = chosen.calls
    assert choice.connection == ConnectionSettings(backend=Backend.PYVISA_PY, gpib_board=1, gpib_address=7)


def test_a_raw_resource_string_is_connected_to_instead_of_the_gpib_address(open_dialog):
    dialog, chosen = open_dialog()

    dialog.resource_entry.insert(0, "  TCPIP::127.0.0.1::5025::SOCKET ")
    dialog.connect_button.invoke()

    [(choice, _)] = chosen.calls
    assert choice.connection.resource_name == "TCPIP::127.0.0.1::5025::SOCKET"


def test_the_simulator_can_be_chosen_instead_of_a_meter(open_dialog):
    dialog, chosen = open_dialog()

    dialog.simulator_radio.invoke()
    dialog.connect_button.invoke()

    [(choice, _)] = chosen.calls
    assert choice.simulate is True


def test_choosing_the_simulator_disables_the_fields_that_only_a_meter_needs(open_dialog):
    dialog, _ = open_dialog()
    assert str(dialog.resource_entry.cget("state")) == "normal"

    dialog.simulator_radio.invoke()

    for widget in (dialog.backend_box, dialog.scan_button, dialog.board_box, dialog.address_box, dialog.resource_entry):
        assert str(widget.cget("state")) == "disabled"

    dialog.meter_radio.invoke()

    assert str(dialog.resource_entry.cget("state")) == "normal"
    assert str(dialog.backend_box.cget("state")) == "readonly"


@pytest.mark.parametrize(
    ("board", "address", "mentions"),
    [("x", "22", "whole numbers"), ("0", "31", "0 to 30"), ("-1", "22", "negative")],
)
def test_an_impossible_gpib_board_or_address_is_explained_and_nothing_is_connected(
    open_dialog, board, address, mentions
):
    dialog, chosen = open_dialog()

    dialog.board_box.set(board)
    dialog.address_box.set(address)
    dialog.connect_button.invoke()

    assert mentions in dialog.error_label.cget("text")
    assert chosen.calls == []


def test_a_blank_resource_string_means_the_gpib_address_is_used(open_dialog):
    dialog, chosen = open_dialog()

    dialog.resource_entry.insert(0, "   ")
    dialog.connect_button.invoke()

    [(choice, _)] = chosen.calls
    assert choice.connection.resource is None


def test_the_dialog_starts_from_the_last_connection(open_dialog):
    last = LastConnection(
        simulate=False,
        connection=ConnectionSettings(backend=Backend.PYVISA_PY, gpib_board=2, gpib_address=9),
    )

    dialog, chosen = open_dialog(initial=last)

    assert dialog.backend_box.get().startswith("pyvisa-py")
    assert (dialog.board_box.get(), dialog.address_box.get()) == ("2", "9")
    dialog.connect_button.invoke()
    assert chosen.calls[0][0] == last


def test_the_dialog_starts_from_a_last_connection_with_a_raw_resource_string(open_dialog):
    last = LastConnection(simulate=False, connection=ConnectionSettings(resource="TCPIP::127.0.0.1::5025::SOCKET"))

    dialog, _ = open_dialog(initial=last)

    assert dialog.resource_entry.get() == "TCPIP::127.0.0.1::5025::SOCKET"


def test_the_dialog_starts_on_the_simulator_when_that_was_the_last_connection(open_dialog):
    dialog, chosen = open_dialog(initial=LastConnection(simulate=True, connection=ConnectionSettings()))

    dialog.connect_button.invoke()

    assert chosen.calls[0][0].simulate is True


def test_reconnecting_at_startup_is_off_by_default_and_the_choice_is_handed_over(open_dialog):
    dialog, chosen = open_dialog()
    assert dialog.auto_reconnect_var.get() is False

    dialog.auto_reconnect_check.invoke()
    dialog.connect_button.invoke()

    assert chosen.calls[0][1] is True


def test_the_dialog_shows_the_reconnect_setting_it_was_given(open_dialog):
    dialog, _ = open_dialog(auto_reconnect=True)

    assert dialog.auto_reconnect_var.get() is True


def test_cancel_closes_the_dialog_without_connecting(open_dialog):
    dialog, chosen = open_dialog()

    dialog.cancel_button.invoke()

    assert chosen.calls == []
    assert not dialog.is_open


def test_connecting_closes_the_dialog(open_dialog):
    dialog, _ = open_dialog()

    dialog.connect_button.invoke()

    assert not dialog.is_open


def scan_listing(dialog: ConnectionDialog) -> list[str]:
    return list(dialog.resource_list.get(0, "end"))


def scan(dialog: ConnectionDialog, tk_root: tk.Tk) -> None:
    dialog.scan_button.invoke()
    pump(tk_root, lambda: str(dialog.scan_button.cget("state")) == "normal")


def test_scan_lists_the_resources_each_backend_can_see(open_dialog, tk_root):
    def both(_requested: Backend) -> dict[Backend, BackendScan]:
        return {
            Backend.VENDOR: BackendScan(resources=("GPIB0::22::INSTR",)),
            Backend.PYVISA_PY: BackendScan(resources=("ASRL/dev/ttyUSB0::INSTR",)),
        }

    dialog, _ = open_dialog(scan=both)

    scan(dialog, tk_root)

    assert scan_listing(dialog) == [
        "GPIB0::22::INSTR  (Keysight/NI VISA)",
        "ASRL/dev/ttyUSB0::INSTR  (pyvisa-py)",
    ]
    assert dialog.scan_status.cget("text") == "Found 2 resources."


def test_scan_asks_for_the_backend_that_is_chosen(open_dialog, tk_root):
    asked: list[Backend] = []

    def record(requested: Backend) -> dict[Backend, BackendScan]:
        asked.append(requested)
        return {}

    dialog, _ = open_dialog(scan=record)
    pick_backend(dialog, Backend.PYVISA_PY)

    scan(dialog, tk_root)

    assert asked == [Backend.PYVISA_PY]


def test_scan_says_when_nothing_was_found(open_dialog, tk_root):
    dialog, _ = open_dialog(scan=lambda _requested: {Backend.PYVISA_PY: BackendScan()})

    scan(dialog, tk_root)

    assert scan_listing(dialog) == []
    assert dialog.scan_status.cget("text") == "No resources found."


def test_scan_says_why_a_backend_could_not_look(open_dialog, tk_root):
    def partly(_requested: Backend) -> dict[Backend, BackendScan]:
        return {
            Backend.VENDOR: BackendScan(problem="Could not open VISA library"),
            Backend.PYVISA_PY: BackendScan(resources=("GPIB0::22::INSTR",)),
        }

    dialog, _ = open_dialog(scan=partly)

    scan(dialog, tk_root)

    assert scan_listing(dialog) == ["GPIB0::22::INSTR  (pyvisa-py)"]
    assert dialog.scan_status.cget("text") == "Found 1 resource. Keysight/NI VISA: Could not open VISA library"


def test_an_unexpected_scan_failure_is_reported_and_the_scan_button_comes_back(open_dialog, tk_root):
    def explode(_requested: Backend) -> dict[Backend, BackendScan]:
        message = "boom"
        raise RuntimeError(message)

    dialog, _ = open_dialog(scan=explode)

    scan(dialog, tk_root)

    assert "boom" in dialog.scan_status.cget("text")


def test_a_slow_scan_does_not_freeze_the_dialog(open_dialog, tk_root):
    release = threading.Event()

    def slow(_requested: Backend) -> dict[Backend, BackendScan]:
        release.wait(TIMEOUT_S)
        return {Backend.PYVISA_PY: BackendScan(resources=("GPIB0::22::INSTR",))}

    dialog, _ = open_dialog(scan=slow)
    try:
        dialog.scan_button.invoke()
        tk_root.update()
        assert dialog.scan_status.cget("text") == "Scanning…"
        assert str(dialog.scan_button.cget("state")) == "disabled"
    finally:
        release.set()
    pump(tk_root, lambda: str(dialog.scan_button.cget("state")) == "normal")

    assert scan_listing(dialog) == ["GPIB0::22::INSTR  (pyvisa-py)"]


def select(dialog: ConnectionDialog, index: int) -> None:
    dialog.resource_list.selection_clear(0, "end")
    dialog.resource_list.selection_set(index)
    dialog.resource_list.event_generate("<<ListboxSelect>>")
    dialog.window.update()


def test_choosing_a_scanned_gpib_resource_fills_in_the_board_and_address(open_dialog, tk_root):
    def gpib(_requested: Backend) -> dict[Backend, BackendScan]:
        return {Backend.PYVISA_PY: BackendScan(resources=("GPIB1::5::INSTR",))}

    dialog, chosen = open_dialog(scan=gpib)
    scan(dialog, tk_root)

    select(dialog, 0)
    assert (dialog.board_box.get(), dialog.address_box.get()) == ("1", "5")
    assert dialog.resource_entry.get() == ""
    dialog.connect_button.invoke()

    assert chosen.calls[0][0].connection == ConnectionSettings(backend=Backend.PYVISA_PY, gpib_board=1, gpib_address=5)


def test_choosing_any_other_scanned_resource_fills_in_the_resource_string(open_dialog, tk_root):
    def socket(_requested: Backend) -> dict[Backend, BackendScan]:
        return {Backend.PYVISA_PY: BackendScan(resources=("TCPIP::127.0.0.1::5025::SOCKET",))}

    dialog, chosen = open_dialog(scan=socket)
    scan(dialog, tk_root)

    select(dialog, 0)
    assert dialog.resource_entry.get() == "TCPIP::127.0.0.1::5025::SOCKET"
    dialog.connect_button.invoke()

    assert chosen.calls[0][0].connection.resource_name == "TCPIP::127.0.0.1::5025::SOCKET"


def test_choosing_a_scanned_resource_selects_the_backend_that_saw_it(open_dialog, tk_root):
    def vendor_sees_it(_requested: Backend) -> dict[Backend, BackendScan]:
        return {Backend.VENDOR: BackendScan(resources=("GPIB0::22::INSTR",))}

    dialog, chosen = open_dialog(scan=vendor_sees_it)
    scan(dialog, tk_root)

    select(dialog, 0)
    dialog.connect_button.invoke()

    assert chosen.calls[0][0].connection.backend is Backend.VENDOR


def test_closing_the_dialog_while_a_scan_is_running_is_harmless(open_dialog, tk_root):
    release = threading.Event()

    def slow(_requested: Backend) -> dict[Backend, BackendScan]:
        release.wait(TIMEOUT_S)
        return {}

    dialog, chosen = open_dialog(scan=slow)
    dialog.scan_button.invoke()
    dialog.cancel_button.invoke()
    release.set()

    deadline = time.monotonic() + 0.2
    pump(tk_root, lambda: time.monotonic() > deadline)
    assert chosen.calls == []
