"""The `raw` subcommand's logic: send one raw SCPI command or query and print what the Meter answered.

Calibration writes are refused unless `--allow-calibration` is given (ADR-0006); the refusal comes from the Driver,
before anything is sent, so the CLI and the GUI console cannot disagree about what counts as one.
"""

import sys

from agilent34401a.driver import Driver
from agilent34401a.errors import CalibrationBlockedError
from agilent34401a.meter import reading_timeout
from agilent34401a.transport import Transport

USAGE_ERROR = 2


def send_raw(driver: Driver, transport: Transport, command: str, *, allow_calibration: bool, prog: str) -> int:
    """Send `command`, print its reply on stdout and the Meter's complaints on stderr; return the exit code.

    A command the CLI refuses to send (calibration, empty, several lines) is a usage error, exit code 2.
    """
    # A query can take as long as a Reading does, so wait as long as one would.
    transport.timeout = reading_timeout(driver.read_setup())
    try:
        result = driver.send_raw(command, allow_calibration=allow_calibration)
    except CalibrationBlockedError as error:
        sys.stderr.write(f"{prog}: error: {error}; pass --allow-calibration if you really mean to\n")
        return USAGE_ERROR
    except ValueError as error:
        sys.stderr.write(f"{prog}: error: {error}\n")
        return USAGE_ERROR
    if result.reply is not None:
        sys.stdout.write(f"{result.reply}\n")
    for queued in result.errors:
        sys.stderr.write(f"{prog}: error: Meter error {queued.code}: {queued.message}\n")
    return 1 if result.errors else 0
