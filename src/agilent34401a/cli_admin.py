"""What the `idn`, `reset`, `selftest`, `errors`, `save` and `recall` subcommands do once connected.

Each function takes the Driver for an identified Meter and returns the process exit code: 0 for success and 1 when
the Meter failed or reported a problem. Failures talking to the Meter are raised as `MeterError` and turned into
exit code 1 by `cli.run_on_meter`.
"""

import sys

from agilent34401a.driver import Driver, QueuedError
from agilent34401a.meter import describe_setup

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


def save(driver: Driver, location: int) -> int:
    """Store the Meter's Setup in Meter Memory `location` (`*SAV`), and fail if the Meter queued errors."""
    if _report(driver.save_to_meter(location)):
        return 1
    sys.stdout.write(f"Stored the Meter's Setup in Meter Memory location {location}\n")
    return 0


def recall(driver: Driver, location: int) -> int:
    """Replace the Meter's Setup with Meter Memory `location` (`*RCL`), then read back and print what it is now in.

    The Setup is read back whatever the Meter said (ADR-0004); a Meter that queued errors (a location that holds
    nothing, say) still fails the command.
    """
    errors = driver.recall_from_meter(location)
    setup = driver.read_setup()
    if _report(errors):
        return 1
    sys.stdout.write(f"Recalled Meter Memory location {location}: {describe_setup(setup)}\n")
    return 0
