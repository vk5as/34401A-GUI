import argparse
import sys
from collections.abc import Callable

import pytest

from agilent34401a import __version__, cli
from agilent34401a.backend import Backend
from agilent34401a.cli import main
from agilent34401a.connection import ConnectionSettings
from agilent34401a.driver import Driver, QueuedError
from agilent34401a.errors import BackendUnavailableError, MeterError, TransportTimeoutError, UnrecognisedIdentityError
from agilent34401a.meter import Function, Setup, reading_timeout
from agilent34401a.sim import Simulator
from agilent34401a.transport import Transport


def test_version_flag_prints_the_version_and_exits_successfully(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_prints_help_and_succeeds(capsys):
    assert main([]) == 0

    assert "agilent34401a-cli" in capsys.readouterr().out


def test_read_with_simulate_prints_a_dc_voltage_reading_and_succeeds(capsys):
    assert main(["read", "--simulate"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "1.000000 V\n"
    assert captured.err == ""


def test_read_reports_a_meter_failure_on_stderr_and_exits_non_zero(monkeypatch, capsys):
    def refuse(_transport):
        message = "Expected an Agilent/HP 34401A"
        raise UnrecognisedIdentityError(message)

    monkeypatch.setattr(Driver, "identify", refuse)

    assert main(["read", "--simulate"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Expected an Agilent/HP 34401A" in captured.err


def test_read_closes_the_transport_even_when_the_meter_fails(monkeypatch):
    closed = []
    monkeypatch.setattr(Simulator, "close", lambda _self: closed.append(True))
    monkeypatch.setattr(Driver, "read", lambda _self: (_ for _ in ()).throw(MeterError("boom")))

    assert main(["read", "--simulate"]) == 1
    assert closed == [True]


def test_help_lists_the_read_subcommand(capsys):
    main([])

    assert "read" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("function", "expected"),
    [
        ("dcv", "1.000000 V"),
        ("acv", "1.000000 V"),
        ("dci", "1.000000 mA"),
        ("aci", "1.000000 mA"),
        ("res", "1.000000 kΩ"),
        ("fres", "1.000000 kΩ"),
        ("freq", "1.00000 kHz"),
        ("period", "1.00000 ms"),
        ("cont", "500.000 mΩ"),
        ("diode", "600.000 mV"),
        ("ratio", "1.000000"),
    ],
)
def test_read_takes_a_reading_in_any_function(function, expected, capsys):
    assert main(["read", "--simulate", "--function", function]) == 0

    assert capsys.readouterr().out == f"{expected}\n"


def test_read_with_a_fixed_range_and_a_lower_resolution(capsys):
    assert main(["read", "--simulate", "--function", "dcv", "--range", "10", "--resolution", "4.5"]) == 0

    assert capsys.readouterr().out == "1.0000 V\n"


def test_read_accepts_auto_as_a_range(capsys):
    assert main(["read", "--simulate", "--range", "auto"]) == 0

    assert capsys.readouterr().out == "1.000000 V\n"


def test_read_digits_follow_the_resolution(capsys):
    main(["read", "--simulate", "--resolution", "5.5"])

    assert capsys.readouterr().out == "1.00000 V\n"


@pytest.mark.parametrize(
    "options",
    [
        ["--function", "dcv", "--range", "5"],
        ["--function", "diode", "--range", "1"],
        ["--function", "acv", "--resolution", "4.5"],
        ["--function", "cont", "--resolution", "6.5"],
    ],
)
def test_read_refuses_a_setup_the_meter_cannot_hold_with_a_usage_exit_code(options, capsys):
    assert main(["read", "--simulate", *options]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agilent34401a-cli: error:" in captured.err


@pytest.mark.parametrize(
    "options",
    [["--range", "lots"], ["--range", ""], ["--range", "nan"], ["--resolution", "7"], ["--function", "bogus"]],
)
def test_read_rejects_unparseable_options_before_connecting(options):
    with pytest.raises(SystemExit) as exit_info:
        main(["read", "--simulate", *options])

    assert exit_info.value.code == 2


def _record_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    writes: list[str] = []
    original = Simulator.write

    def record(self, command):
        writes.append(command)
        original(self, command)

    monkeypatch.setattr(Simulator, "write", record)
    return writes


def test_read_without_setup_options_leaves_the_meters_setup_alone(monkeypatch):
    writes = _record_writes(monkeypatch)

    assert main(["read", "--simulate"]) == 0

    assert all(command.endswith("?") for command in writes)


def test_read_changes_only_what_the_options_ask_for(monkeypatch, capsys):
    writes = _record_writes(monkeypatch)

    assert main(["read", "--simulate", "--range", "10"]) == 0

    assert 'FUNC "VOLT:DC"' in writes
    assert "VOLT:DC:RANG 10" in writes
    assert capsys.readouterr().out == "1.000000 V\n"


def test_read_reports_the_errors_the_meter_queued_and_fails(monkeypatch, capsys):
    monkeypatch.setattr(Driver, "apply", lambda _self, _setup: [QueuedError(-222, "Data out of range")])

    assert main(["read", "--simulate", "--function", "res", "--range", "1000"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "-222" in captured.err
    assert "Data out of range" in captured.err


def test_read_waits_as_long_as_the_setup_needs(monkeypatch):
    timeouts = []
    original = Simulator.query

    def record(self, command):
        if command == "READ?":
            timeouts.append(self.timeout)
        return original(self, command)

    monkeypatch.setattr(Simulator, "query", record)

    main(["read", "--simulate", "--function", "res"])

    assert timeouts == [reading_timeout(Setup.default(Function.RESISTANCE_2W))]


def test_read_reports_errors_from_switching_function_and_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        Driver, "select_function", lambda _self, _function: [QueuedError(-224, "Illegal parameter value")]
    )

    assert main(["read", "--simulate", "--function", "res"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "-224" in captured.err


def test_read_switches_function_without_rewriting_the_functions_own_settings(monkeypatch):
    writes = _record_writes(monkeypatch)

    assert main(["read", "--simulate", "--function", "res"]) == 0

    assert [command for command in writes if not command.endswith("?")] == ['FUNC "RES"']


def _opened(monkeypatch: pytest.MonkeyPatch) -> list[ConnectionSettings]:
    """Replace the real Connection opener with one that hands out a Simulator and records the settings."""
    opened: list[ConnectionSettings] = []

    def open_simulator(settings: ConnectionSettings) -> Simulator:
        opened.append(settings)
        return Simulator()

    monkeypatch.setattr(cli, "open_transport", open_simulator)
    return opened


def test_read_without_simulate_opens_gpib_board_0_address_22_through_auto(monkeypatch, capsys):
    opened = _opened(monkeypatch)

    assert main(["read"]) == 0

    assert opened == [ConnectionSettings(backend=Backend.AUTO, resource=None, gpib_board=0, gpib_address=22)]
    assert opened[0].resource_name == "GPIB0::22::INSTR"
    assert capsys.readouterr().out == "1.000000 V\n"


@pytest.mark.parametrize(
    ("option", "backend"), [("auto", Backend.AUTO), ("ivi", Backend.VENDOR), ("py", Backend.PYVISA_PY)]
)
def test_read_takes_the_backend_from_the_command_line(monkeypatch, option, backend):
    opened = _opened(monkeypatch)

    main(["read", "--backend", option])

    assert opened[0].backend is backend


def test_read_takes_the_gpib_board_and_address_from_the_command_line(monkeypatch):
    opened = _opened(monkeypatch)

    main(["read", "--gpib-board", "1", "--gpib-address", "7"])

    assert opened[0].resource_name == "GPIB1::7::INSTR"


def test_read_takes_a_raw_resource_string(monkeypatch):
    opened = _opened(monkeypatch)

    main(["read", "--resource", "TCPIP::10.0.0.5::5025::SOCKET"])

    assert opened[0].resource_name == "TCPIP::10.0.0.5::5025::SOCKET"


@pytest.mark.parametrize(
    "options",
    [
        ["--simulate", "--resource", "TCPIP::10.0.0.5::5025::SOCKET"],
        ["--simulate", "--backend", "py"],
        ["--simulate", "--gpib-address", "5"],
        ["--simulate", "--gpib-board", "1"],
        ["--resource", "TCPIP::10.0.0.5::5025::SOCKET", "--gpib-address", "5"],
        ["--resource", "TCPIP::10.0.0.5::5025::SOCKET", "--gpib-board", "1"],
        ["--gpib-address", "31"],
        ["--gpib-address", "-1"],
        ["--gpib-board", "-1"],
        ["--resource", "  "],
    ],
)
def test_read_refuses_contradictory_or_impossible_connection_options(monkeypatch, options, capsys):
    opened = _opened(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        main(["read", *options])

    assert exit_info.value.code == 2
    assert opened == []
    assert "error" in capsys.readouterr().err


def test_read_reports_a_backend_that_cannot_be_used_and_exits_non_zero(monkeypatch, capsys):
    def unavailable(_settings):
        message = "Keysight/NI VISA is not available: Could not open VISA library"
        raise BackendUnavailableError(message)

    monkeypatch.setattr(cli, "open_transport", unavailable)

    assert main(["read", "--backend", "ivi"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Keysight/NI VISA is not available" in captured.err


def test_read_reports_a_meter_that_does_not_answer_and_exits_non_zero(monkeypatch, capsys):
    class Silent(Simulator):
        def query(self, _command: str) -> str:
            message = "Timed out waiting for the Meter"
            raise TransportTimeoutError(message)

    monkeypatch.setattr(cli, "open_transport", lambda _settings: Silent())

    assert main(["read"]) == 1

    assert "Timed out waiting for the Meter" in capsys.readouterr().err


def _parse_with_new_subcommand(action: Callable[[Driver, Transport], int], argv: list[str]) -> int:
    """Build a parser with one extra subcommand, the way each feature adds its own."""
    parser = argparse.ArgumentParser(prog="agilent34401a-cli")
    subparsers = parser.add_subparsers(dest="command")
    sub = cli.add_command(subparsers, "demo", lambda args: cli.run_on_meter(args, action), summary="demo")
    cli.add_connection_options(sub)
    args = parser.parse_args(argv)
    return int(args.handler(args))


def test_a_new_subcommand_gets_a_connected_identified_driver_and_its_exit_code(capsys):
    def action(driver, transport):
        sys.stdout.write(f"{driver.setup.function.label} {transport.timeout}\n")
        return 7

    assert _parse_with_new_subcommand(action, ["demo", "--simulate"]) == 7

    assert capsys.readouterr().out.startswith("DC V ")


def test_a_new_subcommand_reports_a_meter_error_on_stderr_and_exits_1(capsys):
    def action(_driver, _transport):
        message = "the Meter said no"
        raise MeterError(message)

    assert _parse_with_new_subcommand(action, ["demo", "--simulate"]) == 1

    assert "agilent34401a-cli: error: the Meter said no" in capsys.readouterr().err


def test_a_new_subcommand_always_closes_the_transport(monkeypatch):
    closed = []
    monkeypatch.setattr(Simulator, "close", lambda _self: closed.append(True))

    _parse_with_new_subcommand(lambda _driver, _transport: 0, ["demo", "--simulate"])

    assert closed == [True]


def test_a_new_subcommand_shares_the_connection_options(monkeypatch):
    opened = _opened(monkeypatch)

    _parse_with_new_subcommand(lambda _driver, _transport: 0, ["demo", "--backend", "py", "--gpib-address", "9"])

    assert opened[0].backend is Backend.PYVISA_PY
    assert opened[0].gpib_address == 9


def test_a_new_subcommand_refuses_contradictory_connection_options():
    with pytest.raises(SystemExit) as exit_info:
        _parse_with_new_subcommand(lambda _driver, _transport: 0, ["demo", "--simulate", "--backend", "py"])

    assert exit_info.value.code == 2
