import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agilent34401a import cli_log
from agilent34401a.cli import main
from agilent34401a.csv_log import COLUMNS
from agilent34401a.driver import Driver
from agilent34401a.errors import TransportTimeoutError
from agilent34401a.sim import Simulator

START = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def read_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


class StepClock:
    """A clock that moves on by `step` each time it is looked at."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def run_log(*, count=None, duration_s=None, output=None, step=1.0) -> tuple[int, str]:
    stdout = io.StringIO()
    wall = StepClock(1.0)
    simulator = Simulator()
    driver = Driver(simulator)
    driver.identify()
    code = cli_log.log(
        driver,
        simulator,
        count=count,
        duration_s=duration_s,
        output=output,
        stdout=stdout,
        prog="agilent34401a-cli",
        clock=StepClock(step),
        wall_clock=lambda: START + timedelta(seconds=wall()),
    )
    return code, stdout.getvalue()


# --- the logic ---------------------------------------------------------------------------------------------------


def test_n_readings_are_written_to_stdout_with_the_header():
    code, out = run_log(count=3)

    rows = read_csv(out)
    assert code == 0
    assert out.splitlines()[0] == ",".join(COLUMNS)
    assert len(rows) == 3
    assert [row["elapsed_s"] for row in rows] == ["0.000", "1.000", "2.000"]
    assert rows[0]["timestamp_iso"] == "2026-03-04T05:06:08.000+00:00"
    assert {row["function"] for row in rows} == {"DC V"}


def test_a_duration_keeps_taking_readings_until_that_much_time_has_passed():
    # The clock moves on 1 s every time it is looked at, and each Reading looks at it twice (the check, the stamp).
    code, out = run_log(duration_s=6.5)

    assert code == 0
    assert len(read_csv(out)) == 3


def test_with_both_the_log_ends_at_whichever_limit_comes_first():
    _, by_count = run_log(count=2, duration_s=1000.0)
    _, by_time = run_log(count=1000, duration_s=4.5)

    assert len(read_csv(by_count)) == 2
    assert len(read_csv(by_time)) == 2


def test_the_output_file_is_written_instead_of_stdout(tmp_path: Path):
    path = tmp_path / "log.csv"

    code, out = run_log(count=2, output=path)

    assert code == 0
    assert out == ""
    assert len(read_csv(path.read_text(encoding="utf-8"))) == 2


def test_an_output_file_that_cannot_be_written_is_an_error_and_exit_code_one(tmp_path: Path, capsys):
    code, out = run_log(count=2, output=tmp_path / "nowhere" / "log.csv")

    assert code == 1
    assert out == ""
    assert "agilent34401a-cli: error:" in capsys.readouterr().err


def test_neither_a_count_nor_a_duration_is_a_usage_error(capsys):
    code, out = run_log()

    assert code == 2
    assert out == ""
    assert "--count" in capsys.readouterr().err


# --- the command ------------------------------------------------------------------------------------------------


def test_log_prints_csv_on_stdout_for_the_simulator(capsys):
    assert main(["log", "--simulate", "-n", "4"]) == 0

    captured = capsys.readouterr()
    rows = read_csv(captured.out)
    assert captured.err == ""
    assert len(rows) == 4
    assert all(row["unit"] == "V" and row["value"] not in {"", "OVLD"} for row in rows)
    assert all(row["timestamp_iso"] and row["raw"] for row in rows)


def test_log_writes_a_file_with_output(tmp_path: Path, capsys):
    path = tmp_path / "log.csv"

    assert main(["log", "--simulate", "--count", "3", "--output", str(path)]) == 0

    assert capsys.readouterr().out == ""
    assert len(read_csv(path.read_text(encoding="utf-8"))) == 3


def test_log_for_a_duration_stops_by_itself(capsys):
    assert main(["log", "--simulate", "--duration", "0.05"]) == 0

    assert len(read_csv(capsys.readouterr().out)) >= 1


@pytest.mark.parametrize("arguments", [["-n", "0"], ["-n", "-3"], ["--duration", "0"], ["--duration", "soon"]])
def test_log_rejects_nonsense_limits_with_exit_code_two(arguments, capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["log", "--simulate", *arguments])

    assert stopped.value.code == 2
    assert capsys.readouterr().out == ""


def test_log_without_a_limit_is_a_usage_error(capsys):
    assert main(["log", "--simulate"]) == 2

    assert capsys.readouterr().out == ""


def test_log_takes_the_same_connection_options_as_the_other_commands(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["log", "--simulate", "--resource", "ASRL1::INSTR", "-n", "1"])

    assert stopped.value.code == 2
    assert "--simulate cannot be combined" in capsys.readouterr().err


def test_log_reports_a_meter_that_cannot_be_reached_and_exits_one(unused_port, capsys):
    resource = f"TCPIP::127.0.0.1::{unused_port}::SOCKET"

    assert main(["log", "--backend", "py", "--resource", resource, "-n", "2"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agilent34401a-cli: error:" in captured.err


def test_an_interrupt_keeps_the_rows_already_written_and_exits_130(tmp_path: Path, monkeypatch):
    path = tmp_path / "log.csv"
    original = Simulator.query
    reads: list[str] = []

    def interrupted(self, command):
        if command == "READ?":
            reads.append(command)
            if len(reads) == 3:
                raise KeyboardInterrupt
        return original(self, command)

    monkeypatch.setattr(Simulator, "query", interrupted)

    assert main(["log", "--simulate", "-n", "10", "-o", str(path)]) == 130

    assert len(read_csv(path.read_text(encoding="utf-8"))) == 2


# --- a lost Reading is a warning, not the end of the log --------------------------------------------------------


class LosingSimulator(Simulator):
    """Loses the Readings whose number (counting every `READ?` from 1) is in `lose`, the way a flaky link would."""

    def __init__(self, lose: set[int] | None = None, *, always: bool = False, silent: bool = False) -> None:
        super().__init__()
        self.lose = lose or set()
        self.always = always
        self.silent = silent
        self.reads = 0

    def query(self, command: str) -> str:
        if command != "READ?":
            return super().query(command)
        self.reads += 1
        if not (self.always or self.reads in self.lose):
            return super().query(command)
        if self.silent:
            message = "no answer"
            raise TransportTimeoutError(message)
        return "\x00 not a reading"


def log_with(simulator: Simulator, *, count: int, output: Path | None = None) -> tuple[int, str]:
    stdout = io.StringIO()
    driver = Driver(simulator)
    driver.identify()
    code = cli_log.log(
        driver,
        simulator,
        count=count,
        duration_s=None,
        output=output,
        stdout=stdout,
        prog="agilent34401a-cli",
        clock=StepClock(1.0),
        wall_clock=lambda: START,
    )
    return code, stdout.getvalue()


@pytest.mark.parametrize("silent", [False, True], ids=["garbled reply", "no reply"])
def test_a_lost_reading_is_a_warning_on_stderr_and_the_log_goes_on_to_exit_zero(silent, capsys):
    code, out = log_with(LosingSimulator({2}, silent=silent), count=3)

    captured = capsys.readouterr()
    assert code == 0
    assert len(read_csv(out)) == 3
    assert captured.err.count("agilent34401a-cli: warning: a Reading was lost") == 1
    assert "error" not in captured.err


def test_lost_readings_do_not_count_towards_the_requested_number(capsys):
    code, out = log_with(LosingSimulator({1, 2, 4}), count=3)

    assert code == 0
    assert len(read_csv(out)) == 3
    assert capsys.readouterr().err.count("warning: a Reading was lost") == 3


def test_the_log_says_at_the_end_how_many_readings_were_lost(capsys):
    log_with(LosingSimulator({2, 3}), count=3)

    assert "2 Readings were lost" in capsys.readouterr().err


def test_the_summary_of_a_single_lost_reading_is_in_the_singular(capsys):
    log_with(LosingSimulator({2}), count=3)

    assert "1 Reading was lost during the log" in capsys.readouterr().err


def test_a_log_that_lost_nothing_says_nothing_on_stderr(capsys):
    log_with(LosingSimulator(), count=3)

    assert capsys.readouterr().err == ""


def test_losses_that_are_not_in_a_row_never_end_the_log():
    # Every second Reading is lost: far more than the limit in total, but never that many one after another.
    code, out = log_with(LosingSimulator(set(range(2, 40, 2))), count=12)

    assert code == 0
    assert len(read_csv(out)) == 12


def test_a_meter_that_loses_every_reading_ends_the_log_with_an_error_after_the_limit_in_a_row(tmp_path: Path, capsys):
    path = tmp_path / "log.csv"
    simulator = LosingSimulator({3, 4, 5, 6, 7})

    code = main_log_with(simulator, path)

    assert code == 1
    assert simulator.reads == 7  # two good Readings, then five lost in a row
    assert len(read_csv(path.read_text(encoding="utf-8"))) == 2
    err = capsys.readouterr().err
    assert err.count("warning: a Reading was lost") == 5
    assert "agilent34401a-cli: error: 5 replies in a row were lost or garbled" in err


def main_log_with(simulator: Simulator, path: Path) -> int:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("agilent34401a.cli.Simulator", lambda: simulator)
        return main(["log", "--simulate", "-n", "10", "-o", str(path)])


class DeafAfterALostReading(LosingSimulator):
    """Garbles the second Reading and then never answers anything again, like a Meter that was unplugged."""

    def read(self) -> str:
        if self.reads >= 2:
            message = "no answer"
            raise TransportTimeoutError(message)
        return super().read()


def test_a_meter_that_stays_silent_after_a_lost_reading_ends_the_log_with_an_error(tmp_path: Path, capsys):
    path = tmp_path / "log.csv"

    code = main_log_with(DeafAfterALostReading({2}), path)

    assert code == 1
    assert "agilent34401a-cli: error: The Meter did not answer *IDN?" in capsys.readouterr().err
    assert len(read_csv(path.read_text(encoding="utf-8"))) == 1
