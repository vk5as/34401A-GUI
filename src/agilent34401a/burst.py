"""Timing a Burst: the Trigger Delay in force, how long a Burst takes, and the timeouts that follow from the Setup."""

from agilent34401a.meter import Setup

# The Meter's automatic Trigger Delay depends on the Function, Range and AC Filter (a few milliseconds, up to 1.5 s for
# the slowest AC Filter). The time a Reading takes in `measurement_time` already includes the AC settling, so the
# model counts the automatic delay as nothing and leaves the margin of every timeout to cover the rest.
_AUTO_DELAY_S = 0.0


def trigger_delay_seconds(setup: Setup) -> float:
    """Seconds between a trigger and the start of its first Reading: the fixed Trigger Delay, or the automatic one."""
    delay = setup.trigger.delay
    return _AUTO_DELAY_S if delay is None else delay
