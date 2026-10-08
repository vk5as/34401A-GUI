"""Timing a Burst: the Trigger Delay in force, how long a Burst takes, and the timeouts that follow from the Setup."""

from dataclasses import dataclass

from agilent34401a.meter import Setup, measurement_time, reading_timeout
from agilent34401a.trigger import TriggerSettings, TriggerSource

# The Meter's automatic Trigger Delay depends on the Function, Range and AC Filter (a few milliseconds, up to 1.5 s for
# the slowest AC Filter). The time a Reading takes in `measurement_time` already includes the AC settling, so the
# model counts the automatic delay as nothing and leaves the margin of every timeout to cover the rest.
_AUTO_DELAY_S = 0.0


def trigger_delay_seconds(setup: Setup) -> float:
    """Seconds between a trigger and the start of its first Reading: the fixed Trigger Delay, or the automatic one."""
    delay = setup.trigger.delay
    return _AUTO_DELAY_S if delay is None else delay


@dataclass
class Burst:
    """A Burst in progress on the Meter: what it was asked for and how many bus triggers have been sent.

    `previous` is the trigger settings the Meter had before, which `Driver.finish_burst` puts back. `setup` is the
    Setup the Readings are taken under, with the Burst's trigger settings in it.
    """

    trigger: TriggerSettings
    setup: Setup
    previous: TriggerSettings
    triggers_sent: int = 0

    @property
    def expected_readings(self) -> int:
        return self.trigger.readings or 0

    def bus_trigger_due(self, points: int) -> bool:
        """Whether the Meter is ready for the next bus trigger, given the Readings already in Reading Memory."""
        count = self.trigger.trigger_count or 0
        return (
            self.trigger.source is TriggerSource.BUS
            and self.triggers_sent < count
            and points >= self.triggers_sent * self.trigger.sample_count
        )


@dataclass(frozen=True)
class BurstProgress:
    """How far a Burst has got: Readings in Reading Memory so far, and whether the Meter says it is done."""

    points: int
    complete: bool


BLOCKING_LIMIT_S = 5.0
"""A Burst expected to take longer than this is polled, so that it can be cancelled, rather than waited for."""
_TIMEOUT_MARGIN = 1.5


@dataclass(frozen=True)
class BurstPlan:
    """How to run a Burst: how long it takes to measure, how long to wait for it, and whether to poll."""

    duration_s: float
    """Seconds the Meter spends measuring, not counting any wait for a trigger."""
    timeout_s: float | None
    """Seconds after which a Burst that has not completed has failed, or None when it may wait for ever."""
    polls: bool
    """Whether to poll the status byte (so the wait can be cancelled) instead of one blocking `*OPC?`."""


def plan_burst(setup: Setup) -> BurstPlan:
    """Work out how to run a Burst under `setup`, whose trigger settings are the Burst's."""
    trigger = setup.trigger
    triggers = trigger.trigger_count or 1
    duration = triggers * (trigger_delay_seconds(setup) + trigger.sample_count * measurement_time(setup))
    timeout = None if trigger.source is TriggerSource.EXTERNAL else duration * _TIMEOUT_MARGIN + reading_timeout(setup)
    several_bus_triggers = trigger.source is TriggerSource.BUS and triggers != 1
    polls = trigger.source is TriggerSource.EXTERNAL or several_bus_triggers or duration > BLOCKING_LIMIT_S
    return BurstPlan(duration, timeout, polls)


def reading_offsets(setup: Setup, count: int) -> list[float]:
    """Seconds after the Burst started at which each of its first `count` Readings was taken.

    The Meter keeps no time stamps, so these come from the Setup: each trigger waits out the Trigger Delay and then
    takes its Samples one after another. Triggers that arrive from outside are taken to follow one another at once.
    """
    trigger = setup.trigger
    delay = trigger_delay_seconds(setup)
    reading = measurement_time(setup)
    samples = trigger.sample_count
    return [
        (index // samples) * (delay + samples * reading) + delay + (index % samples + 1) * reading
        for index in range(count)
    ]
