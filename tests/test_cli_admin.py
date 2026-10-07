from collections.abc import Callable

import pytest

from agilent34401a import cli
from agilent34401a.cli import main
from agilent34401a.driver import Driver, QueuedError
from agilent34401a.errors import TransportTimeoutError, UnrecognisedIdentityError
from agilent34401a.sim import AGILENT_IDENTITY, HEWLETT_PACKARD_IDENTITY, Simulator


class FailingSelfTest(Simulator):
    def __init__(self) -> None:
        super().__init__()
        self.self_test_passes = False


class WithQueuedErrors(Simulator):
    def __init__(self) -> None:
        super().__init__()
        self.write("BAD:COMMAND")
        self.write('FUNC "NOPE"')


class ForgetsTheTimeout(Simulator):
    """A Simulator whose self-test always outlasts the timeout it is given."""

    def __init__(self) -> None:
        super().__init__(time_scale=1000, sleep=lambda _seconds: None)


def record_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    writes: list[str] = []
    original = Simulator.write

    def record(simulator: Simulator, command: str) -> None:
        writes.append(command)
        original(simulator, command)

    monkeypatch.setattr(Simulator, "write", record)
    return writes


def use(monkeypatch: pytest.MonkeyPatch, simulator_class: Callable[[], Simulator]) -> None:
    monkeypatch.setattr(cli, "Simulator", simulator_class)


def test_idn_prints_the_meters_identity_and_succeeds(capsys):
    assert main(["idn", "--simulate"]) == 0

    captured = capsys.readouterr()
    assert captured.out == f"{HEWLETT_PACKARD_IDENTITY}\n"
    assert captured.err == ""


def test_idn_prints_an_agilent_identity_unchanged(monkeypatch, capsys):
    use(monkeypatch, lambda: Simulator(identity=AGILENT_IDENTITY))

    assert main(["idn", "--simulate"]) == 0

    assert capsys.readouterr().out == f"{AGILENT_IDENTITY}\n"


def test_idn_fails_when_the_device_is_not_a_34401a(monkeypatch, capsys):
    def refuse(_driver):
        message = "Expected an Agilent/HP 34401A"
        raise UnrecognisedIdentityError(message)

    monkeypatch.setattr(Driver, "identify", refuse)

    assert main(["idn", "--simulate"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("agilent34401a-cli: error:")


def test_reset_resets_the_meter_and_says_so(monkeypatch, capsys):
    writes = record_writes(monkeypatch)

    assert main(["reset", "--simulate"]) == 0

    assert "*RST" in writes
    assert capsys.readouterr().out == "Meter reset\n"


def test_nothing_but_reset_ever_sends_rst(monkeypatch):
    writes = record_writes(monkeypatch)

    for command in (["idn"], ["read"], ["selftest"], ["errors"]):
        assert main([*command, "--simulate"]) == 0

    assert "*RST" not in writes


def test_reset_fails_when_the_meter_queues_an_error(monkeypatch, capsys):
    monkeypatch.setattr(Driver, "drain_errors", lambda _driver: [QueuedError(-200, "Execution error")])

    assert main(["reset", "--simulate"]) == 1

    assert "Meter error -200: Execution error" in capsys.readouterr().err


def test_selftest_reports_a_pass(capsys):
    assert main(["selftest", "--simulate"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "Self-test passed\n"
    assert captured.err == ""


def test_selftest_fails_with_a_non_zero_exit_code_and_the_errors_the_meter_queued(monkeypatch, capsys):
    use(monkeypatch, FailingSelfTest)

    assert main(["selftest", "--simulate"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Self-test failed" in captured.err
    assert "-330" in captured.err


def test_selftest_that_never_finishes_fails_without_a_traceback(monkeypatch, capsys):
    use(monkeypatch, ForgetsTheTimeout)

    assert main(["selftest", "--simulate"]) == 1

    assert capsys.readouterr().err.startswith("agilent34401a-cli: error:")


def test_errors_prints_every_queued_error_oldest_first_and_empties_the_queue(monkeypatch, capsys):
    use(monkeypatch, WithQueuedErrors)

    assert main(["errors", "--simulate"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines == ["-113: Undefined header", "-224: Illegal parameter value"]


def test_errors_says_so_when_the_queue_is_empty(capsys):
    assert main(["errors", "--simulate"]) == 0

    assert capsys.readouterr().out == "No errors\n"


def test_errors_fails_when_the_meter_cannot_be_asked(monkeypatch, capsys):
    def timeout(_driver):
        message = "Timed out waiting for the Meter"
        raise TransportTimeoutError(message)

    monkeypatch.setattr(Driver, "drain_errors", timeout)

    assert main(["errors", "--simulate"]) == 1

    assert "Timed out" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["idn", "reset", "selftest", "errors"])
def test_every_admin_command_takes_the_connection_options(command, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main([command, "--help"])

    assert exit_info.value.code == 0
    assert "--gpib-address" in capsys.readouterr().out
