import pytest

from agilent34401a.cli import main
from agilent34401a.sim import HEWLETT_PACKARD_IDENTITY, Simulator


def _record_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    writes: list[str] = []
    original = Simulator.write

    def record(self, command):
        writes.append(command)
        original(self, command)

    monkeypatch.setattr(Simulator, "write", record)
    return writes


def test_raw_prints_the_reply_to_a_query(capsys):
    assert main(["raw", "--simulate", "*IDN?"]) == 0

    captured = capsys.readouterr()
    assert captured.out == f"{HEWLETT_PACKARD_IDENTITY}\n"
    assert captured.err == ""


def test_raw_sends_a_command_and_prints_nothing(monkeypatch, capsys):
    writes = _record_writes(monkeypatch)

    assert main(["raw", "--simulate", "VOLT:DC:NPLC 10"]) == 0

    assert "VOLT:DC:NPLC 10" in writes
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_raw_reports_what_the_meter_complained_about_and_exits_non_zero(capsys):
    assert main(["raw", "--simulate", "NOTACOMMAND"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agilent34401a-cli: error: Meter error -113: Undefined header" in captured.err


def test_raw_reports_a_query_the_meter_never_answers_and_exits_non_zero(capsys):
    assert main(["raw", "--simulate", "NOTAQUERY?"]) == 1

    assert "agilent34401a-cli: error:" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["CAL:SEC:STAT OFF,HP034401", "cal:val 1", "*IDN?;:CAL:STR 'x'", "CAL?"])
def test_raw_refuses_a_calibration_write_and_never_sends_it(monkeypatch, capsys, command):
    writes = _record_writes(monkeypatch)

    assert main(["raw", "--simulate", command]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--allow-calibration" in captured.err
    assert not any("CAL" in sent.upper() for sent in writes)
    assert writes.count("*IDN?") == 1  # only the Meter's identification at connect, not a harmless part of the command


def test_raw_sends_a_calibration_write_when_it_is_explicitly_allowed(monkeypatch):
    writes = _record_writes(monkeypatch)

    main(["raw", "--simulate", "--allow-calibration", "CAL:STR 'x'"])

    assert "CAL:STR 'x'" in writes


def test_raw_sends_a_read_only_calibration_query_without_the_override(monkeypatch):
    writes = _record_writes(monkeypatch)

    main(["raw", "--simulate", "CAL:COUN?"])

    assert "CAL:COUN?" in writes


@pytest.mark.parametrize("command", ["", "   ", ";"])
def test_raw_refuses_an_empty_command_as_a_usage_error(capsys, command):
    assert main(["raw", "--simulate", command]) == 2

    assert "Nothing to send" in capsys.readouterr().err


def test_raw_refuses_more_than_one_line_as_a_usage_error(capsys):
    assert main(["raw", "--simulate", "*IDN?\nFUNC?"]) == 2

    assert "one line" in capsys.readouterr().err


def test_help_lists_the_raw_subcommand(capsys):
    main([])

    assert "raw" in capsys.readouterr().out
