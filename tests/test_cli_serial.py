import pytest

from agilent34401a import cli, cli_serial, connection
from agilent34401a.backend import Backend
from agilent34401a.cli import main
from agilent34401a.connection import ConnectionSettings
from agilent34401a.probe import ProbeProgress
from agilent34401a.serial_config import FlowControl, Framing, Parity, SerialSettings, Terminator
from agilent34401a.sim import Simulator
from agilent34401a.sim_serial import SimulatedSerialMeter
from agilent34401a.transport import Transport


class RecordingSimulator(Simulator):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)
        super().write(command)


def _line(monkeypatch: pytest.MonkeyPatch, line: SimulatedSerialMeter) -> list[ConnectionSettings]:
    """Make the CLI open its Connections on a simulated RS-232 line, recording what it was asked to open."""
    opened: list[ConnectionSettings] = []

    def open_line(settings: ConnectionSettings) -> Transport:
        opened.append(settings)
        assert settings.serial is not None
        return line.open(settings.serial)

    monkeypatch.setattr(cli, "open_transport", open_line)
    monkeypatch.setattr(connection, "open_transport", open_line)
    return opened


def test_read_takes_every_serial_parameter_from_the_command_line(monkeypatch):
    opened = _line(monkeypatch, SimulatedSerialMeter(baud=4800, framing=Framing(7, Parity.EVEN, 1)))

    status = main(
        [
            "read",
            "--serial-port",
            "/dev/ttyUSB0",
            "--baud",
            "4800",
            "--data-bits",
            "7",
            "--parity",
            "even",
            "--stop-bits",
            "1",
            "--flow-control",
            "none",
            "--terminator",
            "crlf",
            "--dtr",
            "on",
            "--rts",
            "off",
            "--backend",
            "py",
        ]
    )

    assert status == 0
    assert opened == [
        ConnectionSettings(
            backend=Backend.PYVISA_PY,
            serial=SerialSettings(
                port="/dev/ttyUSB0",
                baud=4800,
                framing=Framing(7, Parity.EVEN, 1),
                flow_control=FlowControl.NONE,
                terminator=Terminator.CRLF,
                dtr=True,
                rts=False,
            ),
        )
    ]


def test_read_over_rs232_defaults_to_the_meters_factory_serial_settings(monkeypatch, capsys):
    opened = _line(monkeypatch, SimulatedSerialMeter())

    assert main(["read", "--serial-port", "COM3"]) == 0

    assert opened[0].serial == SerialSettings(port="COM3")
    assert opened[0].resource_name == "ASRL3::INSTR"
    assert capsys.readouterr().out == "1.000000 V\n"


def test_the_cli_puts_an_rs232_meter_in_remote_and_returns_it_to_local_when_done(monkeypatch):
    simulator = RecordingSimulator()
    _line(monkeypatch, SimulatedSerialMeter(simulator))

    assert main(["read", "--serial-port", "COM3"]) == 0

    assert simulator.writes[:2] == ["SYST:REM", "*IDN?"]
    assert simulator.writes[-1] == "SYST:LOC"
    assert not simulator.remote


def test_the_cli_reports_a_meter_that_does_not_hear_the_serial_settings(monkeypatch, capsys):
    _line(monkeypatch, SimulatedSerialMeter(baud=9600))

    assert main(["read", "--serial-port", "COM3", "--baud", "300"]) == 1

    assert "No reply" in capsys.readouterr().err


@pytest.mark.parametrize(
    "options",
    [
        ["--serial-port", "COM3", "--simulate"],
        ["--serial-port", "COM3", "--resource", "TCPIP::10.0.0.5::5025::SOCKET"],
        ["--serial-port", "COM3", "--gpib-address", "5"],
        ["--serial-port", "COM3", "--gpib-board", "1"],
        ["--baud", "9600"],
        ["--data-bits", "8"],
        ["--parity", "none"],
        ["--stop-bits", "1"],
        ["--flow-control", "none"],
        ["--terminator", "lf"],
        ["--dtr", "on"],
        ["--rts", "on"],
        ["--serial-port", "COM3", "--baud", "1234"],
        ["--serial-port", "COM3", "--data-bits", "9"],
        ["--serial-port", "COM3", "--parity", "mark"],
        ["--serial-port", "COM3", "--stop-bits", "3"],
        ["--serial-port", "   "],
    ],
)
def test_read_refuses_contradictory_or_impossible_serial_options(monkeypatch, options, capsys):
    opened = _line(monkeypatch, SimulatedSerialMeter())

    with pytest.raises(SystemExit) as exit_info:
        main(["read", *options])

    assert exit_info.value.code == 2
    assert opened == []
    assert "error" in capsys.readouterr().err


def test_probe_prints_the_settings_it_found_and_how_to_use_them(monkeypatch, capsys):
    _line(monkeypatch, SimulatedSerialMeter(baud=2400, framing=Framing(7, Parity.ODD, 1)))

    assert main(["probe", "--serial-port", "COM3"]) == 0

    out = capsys.readouterr().out
    assert "Found the Meter at COM3, 2400 baud, 7O1" in out
    assert "--serial-port COM3 --baud 2400 --data-bits 7 --parity odd --stop-bits 1 --flow-control none" in out
    assert "HEWLETT-PACKARD,34401A" in out


def test_probe_shows_which_settings_it_is_trying_on_stderr(monkeypatch, capsys):
    _line(monkeypatch, SimulatedSerialMeter(baud=4800))

    main(["probe", "--serial-port", "COM3"])

    err = capsys.readouterr().err
    assert "Trying 9600 baud 8N1 (1/18)" in err
    assert "Trying 4800 baud 8N1 (4/18)" in err


def test_probe_can_be_quiet_about_its_progress(monkeypatch, capsys):
    _line(monkeypatch, SimulatedSerialMeter(baud=4800))

    main(["probe", "--serial-port", "COM3", "--quiet"])

    assert capsys.readouterr().err == ""


def test_probe_that_finds_nothing_says_what_to_check_and_fails(monkeypatch, capsys):
    _line(monkeypatch, SimulatedSerialMeter(flow_control=FlowControl.RTS_CTS))

    assert main(["probe", "--serial-port", "COM3", "--quiet"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "null-modem" in captured.err
    assert "--include-flow-control" in captured.err


def test_probe_can_include_flow_control_in_its_search(monkeypatch, capsys):
    _line(monkeypatch, SimulatedSerialMeter(flow_control=FlowControl.RTS_CTS))

    assert main(["probe", "--serial-port", "COM3", "--include-flow-control", "--quiet"]) == 0

    assert "--flow-control rtscts" in capsys.readouterr().out


def test_probe_starts_from_the_flow_control_given(monkeypatch):
    line = SimulatedSerialMeter(flow_control=FlowControl.DTR_DSR)
    _line(monkeypatch, line)

    main(["probe", "--serial-port", "COM3", "--flow-control", "dtrdsr", "--quiet"])

    assert line.opened[0].flow_control is FlowControl.DTR_DSR


def test_probe_needs_a_serial_port(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["probe"])

    assert exit_info.value.code == 2
    assert "--serial-port" in capsys.readouterr().err


def test_probe_reports_a_port_that_cannot_be_opened(monkeypatch, capsys):
    _line(monkeypatch, SimulatedSerialMeter(missing_ports={"COM9"}))

    assert main(["probe", "--serial-port", "COM9", "--quiet"]) == 1

    assert "no such serial port" in capsys.readouterr().err


def test_interrupting_probe_cancels_it_and_exits_with_the_interrupt_code(monkeypatch, capsys):
    line = SimulatedSerialMeter(baud=300)
    _line(monkeypatch, line)

    def interrupt(progress: ProbeProgress) -> None:
        if progress.attempt == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_serial, "show_progress", interrupt)

    assert main(["probe", "--serial-port", "COM3"]) == cli_serial.INTERRUPTED

    assert "Probe cancelled" in capsys.readouterr().err
    assert len(line.opened) < 18


def test_help_lists_the_probe_subcommand(capsys):
    main([])

    assert "probe" in capsys.readouterr().out
