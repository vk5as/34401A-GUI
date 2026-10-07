"""What the `idn`, `reset`, `selftest` and `errors` subcommands do once connected.

Each function takes the Driver for an identified Meter and returns the process exit code: 0 for success and 1 when
the Meter failed or reported a problem. Failures talking to the Meter are raised as `MeterError` and turned into
exit code 1 by `cli.run_on_meter`.
"""

import sys

from agilent34401a.driver import Driver, QueuedError

_PROG = "agilent34401a-cli"


def idn(driver: Driver) -> int:
    """Print the Meter's identity exactly as it answered `*IDN?`."""
    sys.stdout.write(f"{driver.identify().raw.strip()}\n")
    return 0


def reset(driver: Driver) -> int:
    """Reset the Meter (`*RST`), the only way the application ever does so, and fail if it queued errors."""
    if _report(driver.reset()):
        return 1
    sys.stdout.write("Meter reset\n")
    return 0


def selftest(driver: Driver) -> int:
    """Run the Meter's self-test, which takes about ten seconds, and fail if it did not pass."""
    if driver.self_test():
        sys.stdout.write("Self-test passed\n")
        return 0
    sys.stderr.write(f"{_PROG}: error: Self-test failed\n")
    _report(driver.drain_errors())
    return 1


def errors(driver: Driver) -> int:
    """Print every error in the Meter's queue, oldest first, which also empties the queue."""
    queued = driver.drain_errors()
    for entry in queued:
        sys.stdout.write(f"{entry.code}: {entry.message}\n")
    if not queued:
        sys.stdout.write("No errors\n")
    return 0


def _report(queued: list[QueuedError]) -> bool:
    """Print what the Meter's error queue held on stderr; return whether it held anything."""
    for entry in queued:
        sys.stderr.write(f"{_PROG}: error: Meter error {entry.code}: {entry.message}\n")
    return bool(queued)
