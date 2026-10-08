"""The command line's RS-232 side: the serial options every Connection shares, and the `probe` subcommand.

`cli.py` registers these; they live here so that file only grows by a line or two per feature.
"""

import argparse
import queue
import sys

from agilent34401a.backend import Backend
from agilent34401a.probe import ProbeJob, ProbeProgress, ProbeResult, start_probe
from agilent34401a.serial_config import (
    BAUD_RATES,
    FlowControl,
    Framing,
    Parity,
    SerialSettings,
    Terminator,
)

PROG = "agilent34401a-cli"
INTERRUPTED = 130  # the exit code a shell gives a program that Ctrl-C stopped
_POLL_S = 0.2
_ON, _OFF = "on", "off"
_OPTION_DESTINATIONS = {
    "--baud": "baud",
    "--data-bits": "data_bits",
    "--parity": "parity",
    "--stop-bits": "stop_bits",
    "--flow-control": "flow_control",
    "--terminator": "terminator",
    "--dtr": "dtr",
    "--rts": "rts",
}


def add_serial_options(parser: argparse.ArgumentParser) -> None:
    """Add the RS-232 options: `--serial-port` selects RS-232, and the rest say how to set the port up."""
    group = parser.add_argument_group(
        "RS-232",
        "Use --serial-port instead of GPIB. The other options default to the Meter's factory settings: "
        "9600 baud, 8 data bits, no parity, 1 stop bit, no Flow Control.",
    )
    group.add_argument("--serial-port", help="the serial port, such as COM3 or /dev/ttyUSB0; selects RS-232")
    group.add_argument("--baud", type=int, choices=BAUD_RATES, help="the baud rate (default 9600)")
    group.add_argument("--data-bits", type=int, choices=(7, 8), help="data bits per character (default 8)")
    group.add_argument("--parity", choices=[parity.value for parity in Parity], help="parity (default none)")
    group.add_argument("--stop-bits", type=int, choices=(1, 2), help="stop bits (default 1)")
    group.add_argument(
        "--flow-control",
        choices=[flow.value for flow in FlowControl],
        help="Flow Control: none, xonxoff, rtscts or dtrdsr (default none)",
    )
    group.add_argument(
        "--terminator",
        choices=[terminator.value for terminator in Terminator],
        help="the line terminator sent after each command: lf, cr or crlf (default lf)",
    )
    group.add_argument(
        "--dtr", choices=(_ON, _OFF), help="hold the DTR line asserted or unasserted (default: leave it)"
    )
    group.add_argument(
        "--rts", choices=(_ON, _OFF), help="hold the RTS line asserted or unasserted (default: leave it)"
    )


def serial_options_given(args: argparse.Namespace) -> list[str]:
    """Name the serial setup options (not the port) that were given on the command line."""
    return [flag for flag, destination in _OPTION_DESTINATIONS.items() if getattr(args, destination, None) is not None]


def serial_settings(args: argparse.Namespace, parser: argparse.ArgumentParser) -> SerialSettings | None:
    """Build the serial settings from the command line, or None when no `--serial-port` was given.

    Setup options without a port are an error rather than silently ignored.
    """
    if args.serial_port is None:
        loose = serial_options_given(args)
        if loose:
            parser.error(f"{', '.join(loose)} can only be used with --serial-port")
        return None
    try:
        defaults = SerialSettings(port=args.serial_port)
    except ValueError as error:
        parser.error(f"argument --serial-port: {error}")
    framing = Framing(
        data_bits=defaults.framing.data_bits if args.data_bits is None else args.data_bits,
        parity=defaults.framing.parity if args.parity is None else Parity(args.parity),
        stop_bits=defaults.framing.stop_bits if args.stop_bits is None else args.stop_bits,
    )
    return SerialSettings(
        port=args.serial_port,
        baud=defaults.baud if args.baud is None else args.baud,
        framing=framing,
        flow_control=defaults.flow_control if args.flow_control is None else FlowControl(args.flow_control),
        terminator=defaults.terminator if args.terminator is None else Terminator(args.terminator),
        dtr=None if args.dtr is None else args.dtr == _ON,
        rts=None if args.rts is None else args.rts == _ON,
    )


def add_probe_options(parser: argparse.ArgumentParser) -> None:
    """Add what `probe` takes: the Backend, the port and its fixed settings, and the Flow Control option."""
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in Backend],
        help="the VISA Backend: auto (vendor VISA if it loads, else pyvisa-py), ivi (Keysight/NI VISA) or py (pyvisa-py)",
    )
    add_serial_options(parser)
    parser.add_argument(
        "--include-flow-control",
        action="store_true",
        help="also try every Flow Control, after the one given; this takes four times as long",
    )
    parser.add_argument("--quiet", action="store_true", help="do not show the settings being tried")


def show_progress(progress: ProbeProgress) -> None:
    """Say on stderr which settings Probe is about to try."""
    sys.stderr.write(f"{progress.message}\n")


def run_probe(args: argparse.Namespace) -> int:
    """Probe the port for the Meter's settings; print them (exit 0), or say what to check (exit 1)."""
    parser: argparse.ArgumentParser = args.parser
    settings = serial_settings(args, parser)
    if settings is None:
        parser.error("the following arguments are required: --serial-port")
    backend = Backend.AUTO if args.backend is None else Backend(args.backend)

    job = start_probe(settings, backend, args.include_flow_control)
    job.start()
    try:
        result = _wait(job, quiet=args.quiet)
    except KeyboardInterrupt:
        job.cancel()
        sys.stderr.write("Probe cancelled.\n")
        job.join()
        return INTERRUPTED
    return _report(result)


def _wait(job: ProbeJob, *, quiet: bool) -> ProbeResult:
    """Pass the job's progress on until its result arrives; polling lets Ctrl-C be heard on every platform."""
    while True:
        try:
            event = job.events.get(timeout=_POLL_S)
        except queue.Empty:
            continue
        if isinstance(event, ProbeResult):
            return event
        if not quiet:
            show_progress(event)


def _report(result: ProbeResult) -> int:
    found = result.found
    if found is None:
        advice = "" if result.included_flow_control or result.problem else " Try again with --include-flow-control."
        sys.stderr.write(f"{PROG}: error: {result.message}{advice}\n")
        return 1
    sys.stdout.write(f"{result.message}\n")
    if result.identity is not None:
        sys.stdout.write(f"{result.identity.raw.strip()}\n")
    sys.stdout.write(f"Use: {options_for(found)}\n")
    return 0


def options_for(settings: SerialSettings) -> str:
    """Return the command-line options that select `settings`."""
    return (
        f"--serial-port {settings.port} --baud {settings.baud} --data-bits {settings.framing.data_bits} "
        f"--parity {settings.framing.parity.value} --stop-bits {settings.framing.stop_bits} "
        f"--flow-control {settings.flow_control.value}"
    )
