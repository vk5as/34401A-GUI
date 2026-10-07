"""Command-line interface (`agilent34401a-cli`)."""

import argparse
import sys
from collections.abc import Sequence

from agilent34401a import __version__
from agilent34401a.driver import Driver
from agilent34401a.errors import MeterError
from agilent34401a.meter import format_reading
from agilent34401a.sim import Simulator
from agilent34401a.transport import Transport

_PROG = "agilent34401a-cli"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Remote control for the Agilent/HP 34401A digital multimeter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(title="commands", dest="command")
    read_parser = subparsers.add_parser("read", help="take one Reading and print it")
    read_parser.add_argument("--simulate", action="store_true", help="use the built-in Simulator instead of a Meter")

    args = parser.parse_args(argv)
    if args.command == "read":
        if not args.simulate:
            # Connection options (Backend, resource, serial parameters) arrive with the VISA Transport.
            read_parser.error("only the Simulator is available so far; pass --simulate")
        return _read(Simulator())
    parser.print_help()
    return 0


def _read(transport: Transport) -> int:
    try:
        driver = Driver(transport)
        driver.identify()
        sys.stdout.write(f"{format_reading(driver.read())}\n")
    except MeterError as error:
        sys.stderr.write(f"{_PROG}: error: {error}\n")
        return 1
    finally:
        transport.close()
    return 0
