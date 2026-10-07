import pytest

from agilent34401a import __version__
from agilent34401a.cli import main
from agilent34401a.driver import Driver, QueuedError
from agilent34401a.errors import MeterError, UnrecognisedIdentityError
from agilent34401a.meter import Function, Setup, reading_timeout
from agilent34401a.sim import Simulator


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


def test_read_without_simulate_explains_that_connections_are_not_available_yet(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["read"])

    assert exit_info.value.code == 2
    assert "--simulate" in capsys.readouterr().err


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
