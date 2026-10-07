"""Command-line interface (`agilent34401a-cli`)."""

import argparse
import math
import sys
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

from agilent34401a import __version__, cli_admin, cli_log, cli_raw
from agilent34401a.backend import Backend
from agilent34401a.connection import ConnectionSettings, open_transport
from agilent34401a.driver import Driver, QueuedError
from agilent34401a.errors import InvalidSetupError, MeterError
from agilent34401a.meter import Function, Resolution, Setup, format_reading, reading_timeout
from agilent34401a.sim import Simulator
from agilent34401a.transport import Transport

_PROG = "agilent34401a-cli"
_USAGE_ERROR = 2

_FUNCTIONS = {
    "dcv": Function.DC_VOLTAGE,
    "acv": Function.AC_VOLTAGE,
    "dci": Function.DC_CURRENT,
    "aci": Function.AC_CURRENT,
    "res": Function.RESISTANCE_2W,
    "fres": Function.RESISTANCE_4W,
    "freq": Function.FREQUENCY,
    "period": Function.PERIOD,
    "cont": Function.CONTINUITY,
    "diode": Function.DIODE,
    "ratio": Function.DC_VOLTAGE_RATIO,
}
_AUTO = "auto"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Remote control for the Agilent/HP 34401A digital multimeter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(title="commands", dest="command")
    # Each subcommand lives in its own _add_<name>_command and registers here with one line.
    _add_read_command(subparsers)
    _add_raw_command(subparsers)
    _add_log_command(subparsers)
    _add_admin_commands(subparsers)

    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


def add_connection_options(parser: argparse.ArgumentParser) -> None:
    """Add `--simulate` and the Connection options every subcommand that talks to a Meter shares."""
    parser.add_argument("--simulate", action="store_true", help="use the built-in Simulator instead of a Meter")
    connection = parser.add_argument_group(
        "Connection", "Which Meter to talk to; the default is GPIB board 0, address 22."
    )
    connection.add_argument(
        "--backend",
        choices=[backend.value for backend in Backend],
        help="the VISA Backend: auto (vendor VISA if it loads, else pyvisa-py), ivi (Keysight/NI VISA) or py (pyvisa-py)",
    )
    connection.add_argument("--gpib-board", type=int, help="the GPIB board index (default 0)")
    connection.add_argument("--gpib-address", type=int, help="the Meter's GPIB address (default 22)")
    connection.add_argument("--resource", help="a raw VISA resource string, instead of the GPIB board and address")


def add_command(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    name: str,
    handler: Callable[[argparse.Namespace], int],
    *,
    summary: str,
    description: str | None = None,
) -> argparse.ArgumentParser:
    """Register a subcommand whose `handler` receives the parsed arguments and returns the exit code."""
    parser = subparsers.add_parser(name, help=summary, description=description or summary)
    parser.set_defaults(handler=handler, parser=parser)
    return parser


def run_on_meter(args: argparse.Namespace, action: Callable[[Driver, Transport], int]) -> int:
    """Connect as `args` ask, identify the Meter, run `action`, and always close the Transport.

    A failure talking to the Meter is printed on stderr and gives exit code 1; the Transport is closed either way.
    """
    settings = _connection_settings(args, args.parser)
    try:
        transport = Simulator() if settings is None else open_transport(settings)
    except MeterError as error:
        sys.stderr.write(f"{_PROG}: error: {error}\n")
        return 1
    try:
        driver = Driver(transport)
        driver.identify()
        return action(driver, transport)
    except MeterError as error:
        sys.stderr.write(f"{_PROG}: error: {error}\n")
        return 1
    finally:
        transport.close()


def _add_read_command(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = add_command(
        subparsers,
        "read",
        _run_read,
        summary="take one Reading and print it",
        description="Take one Reading. Options you leave out keep the Meter's current Setup.",
    )
    add_connection_options(parser)
    parser.add_argument(
        "--function",
        choices=sorted(_FUNCTIONS),
        help="the Function to measure; it keeps its own Range and Resolution unless those are given too",
    )
    parser.add_argument(
        "--range", dest="range_", metavar="RANGE", help=f"the Range in volts, amps or ohms, or '{_AUTO}' for Autorange"
    )
    parser.add_argument(
        "--resolution",
        type=float,
        choices=[resolution.value for resolution in Resolution],
        help="the Resolution in digits",
    )


def _add_admin_commands(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Register `idn`, `reset`, `selftest` and `errors`; what they do lives in `cli_admin`."""
    commands = (
        ("idn", cli_admin.idn, "print the Meter's identity and firmware revision", None),
        (
            "reset",
            cli_admin.reset,
            "reset the Meter to its power-on Setup (*RST)",
            "Reset the Meter. This is the only command that changes the Meter's Setup without being told what to change.",
        ),
        (
            "selftest",
            cli_admin.selftest,
            "run the Meter's self-test (about 10 s)",
            "Run the Meter's self-test, which takes about ten seconds. Exit code 0 means it passed.",
        ),
        (
            "errors",
            cli_admin.errors,
            "print and clear the Meter's error queue",
            "Print every error in the Meter's error queue, oldest first, which also empties it.",
        ),
    )
    for name, action, summary, description in commands:
        parser = add_command(subparsers, name, partial(_run_admin, action), summary=summary, description=description)
        add_connection_options(parser)


def _run_admin(action: Callable[[Driver], int], args: argparse.Namespace) -> int:
    return run_on_meter(args, lambda driver, _transport: action(driver))


def _run_read(args: argparse.Namespace) -> int:
    range_ = _parse_range(args.range_, args.parser)
    return run_on_meter(
        args, lambda driver, transport: _read(driver, transport, args.function, range_, args.resolution)
    )


def _add_raw_command(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = add_command(
        subparsers,
        "raw",
        _run_raw,
        summary="send a raw SCPI command or query and print the reply",
        description=(
            "Send one raw SCPI command or query (several may be joined with ';') and print the Meter's reply. "
            "Commands that would change the Meter's calibration are refused unless --allow-calibration is given."
        ),
    )
    add_connection_options(parser)
    parser.add_argument("command", help="the SCPI command or query, quoted so the shell passes it as one argument")
    parser.add_argument(
        "--allow-calibration",
        action="store_true",
        help="send calibration commands (CAL:SEC, CAL:VAL, CAL, CAL:STR) instead of refusing them; this can "
        "invalidate the Meter's calibration",
    )


def _run_raw(args: argparse.Namespace) -> int:
    return run_on_meter(
        args,
        lambda driver, transport: cli_raw.send_raw(
            driver, transport, args.command, allow_calibration=args.allow_calibration, prog=_PROG
        ),
    )


def _add_log_command(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = add_command(
        subparsers,
        "log",
        _run_log,
        summary="take Readings and write them as CSV",
        description=(
            "Take Readings with the Meter's current Setup and write them as CSV, to standard output or a file. "
            "Give --count, --duration or both; the log ends at whichever comes first. The columns are "
            "timestamp_iso, elapsed_s, function, range, value, unit, raw, math_mode and limit_result."
        ),
    )
    add_connection_options(parser)
    parser.add_argument("-n", "--count", type=_positive_int, help="stop after this many Readings")
    parser.add_argument("--duration", type=_positive_seconds, metavar="SECONDS", help="stop after this many seconds")
    parser.add_argument("-o", "--output", type=Path, help="write the CSV to this file instead of standard output")


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        message = f"expected a whole number of at least 1, got {text!r}"
        raise argparse.ArgumentTypeError(message)
    return value


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        message = f"expected a number of seconds above 0, got {text!r}"
        raise argparse.ArgumentTypeError(message)
    return value


def _run_log(args: argparse.Namespace) -> int:
    return run_on_meter(
        args,
        lambda driver, transport: cli_log.log(
            driver, transport, count=args.count, duration_s=args.duration, output=args.output, prog=_PROG
        ),
    )


def _connection_settings(args: argparse.Namespace, parser: argparse.ArgumentParser) -> ConnectionSettings | None:
    """Build the Connection settings from the command line, or None when the Simulator was asked for."""
    chosen = {
        "--backend": args.backend,
        "--gpib-board": args.gpib_board,
        "--gpib-address": args.gpib_address,
        "--resource": args.resource,
    }
    given = [name for name, value in chosen.items() if value is not None]
    if args.simulate:
        if given:
            parser.error(f"--simulate cannot be combined with {', '.join(given)}")
        return None
    if args.resource is not None and ("--gpib-board" in given or "--gpib-address" in given):
        parser.error("--resource cannot be combined with --gpib-board or --gpib-address")
    defaults = ConnectionSettings()
    try:
        return ConnectionSettings(
            backend=defaults.backend if args.backend is None else Backend(args.backend),
            resource=args.resource,
            gpib_board=defaults.gpib_board if args.gpib_board is None else args.gpib_board,
            gpib_address=defaults.gpib_address if args.gpib_address is None else args.gpib_address,
        )
    except ValueError as error:
        parser.error(str(error))


def _parse_range(text: str | None, parser: argparse.ArgumentParser) -> float | str | None:
    """Return None when no Range was asked for, `_AUTO` for Autorange, or the Range itself."""
    if text is None:
        return None
    if text.lower() == _AUTO:
        return _AUTO
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        parser.error(f"argument --range: expected a number or '{_AUTO}', got {text!r}")
    return value


def _read(
    driver: Driver, transport: Transport, function: str | None, range_: float | str | None, digits: float | None
) -> int:
    if function is not None and _report(driver.select_function(_FUNCTIONS[function])):
        return 1
    current = driver.read_setup()
    try:
        wanted = _wanted_setup(current, range_, digits)
    except InvalidSetupError as error:
        sys.stderr.write(f"{_PROG}: error: {error}\n")
        return _USAGE_ERROR
    if wanted != current and _report(driver.apply(wanted)):
        return 1
    transport.timeout = reading_timeout(driver.setup)
    sys.stdout.write(f"{format_reading(driver.read(), driver.setup.resolution)}\n")
    return 0


def _report(errors: list[QueuedError]) -> bool:
    """Print what the Meter's error queue held; return whether it held anything."""
    for queued in errors:
        sys.stderr.write(f"{_PROG}: error: Meter error {queued.code}: {queued.message}\n")
    return bool(errors)


def _wanted_setup(current: Setup, range_: float | str | None, digits: float | None) -> Setup:
    setup = current
    if range_ is not None:
        setup = setup.with_range(None if range_ == _AUTO else float(range_))
    if digits is not None:
        setup = setup.with_resolution(Resolution(digits))
    return setup
