"""Command-line interface (`agilent34401a-cli`)."""

import argparse
import math
import sys
from collections.abc import Sequence

from agilent34401a import __version__
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
    read_parser = subparsers.add_parser(
        "read",
        help="take one Reading and print it",
        description="Take one Reading. Options you leave out keep the Meter's current Setup.",
    )
    read_parser.add_argument("--simulate", action="store_true", help="use the built-in Simulator instead of a Meter")
    connection = read_parser.add_argument_group(
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
    read_parser.add_argument(
        "--function",
        choices=sorted(_FUNCTIONS),
        help="the Function to measure; it keeps its own Range and Resolution unless those are given too",
    )
    read_parser.add_argument(
        "--range", dest="range_", metavar="RANGE", help=f"the Range in volts, amps or ohms, or '{_AUTO}' for Autorange"
    )
    read_parser.add_argument(
        "--resolution",
        type=float,
        choices=[resolution.value for resolution in Resolution],
        help="the Resolution in digits",
    )

    args = parser.parse_args(argv)
    if args.command == "read":
        range_ = _parse_range(args.range_, read_parser)
        settings = _connection_settings(args, read_parser)
        try:
            transport = Simulator() if settings is None else open_transport(settings)
        except MeterError as error:
            sys.stderr.write(f"{_PROG}: error: {error}\n")
            return 1
        return _read(transport, args.function, range_, args.resolution)
    parser.print_help()
    return 0


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


def _read(transport: Transport, function: str | None, range_: float | str | None, digits: float | None) -> int:
    try:
        driver = Driver(transport)
        driver.identify()
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
    except MeterError as error:
        sys.stderr.write(f"{_PROG}: error: {error}\n")
        return 1
    finally:
        transport.close()
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
