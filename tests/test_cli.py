import pytest

from agilent34401a import __version__
from agilent34401a.cli import main
from agilent34401a.driver import Driver
from agilent34401a.errors import MeterError, UnrecognisedIdentityError
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
