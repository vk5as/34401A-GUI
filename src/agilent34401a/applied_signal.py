"""The Applied Signal: what the Simulator pretends is connected to the Meter's terminals for one Function."""

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class AppliedSignal:
    """A value with optional noise, drift and a sine component, all in the unit of the Function it is applied to.

    `noise` is the standard deviation of Gaussian noise. `drift` is how much the value changes every simulated
    second. The sine component swings `sine_amplitude` either side of the value at `sine_frequency_hz`, starting
    upwards from the value at time 0.
    """

    value: float
    noise: float = 0.0
    drift: float = 0.0
    sine_amplitude: float = 0.0
    sine_frequency_hz: float = 1.0

    def __post_init__(self) -> None:
        if self.noise < 0:
            message = f"The noise must be zero or more, got {self.noise}"
            raise ValueError(message)
        if self.sine_amplitude < 0:
            message = f"The sine amplitude must be zero or more, got {self.sine_amplitude}"
            raise ValueError(message)
        if self.sine_frequency_hz <= 0:
            message = f"The sine frequency must be above zero, got {self.sine_frequency_hz}"
            raise ValueError(message)

    @property
    def peak(self) -> float:
        """The largest magnitude the value and the sine component reach together, before noise and drift."""
        return abs(self.value) + self.sine_amplitude

    def at(self, seconds: float, source: random.Random) -> float:
        """Return the signal `seconds` of simulated time after the start, drawing noise from `source`."""
        signal = self.value + self.drift * seconds
        if self.sine_amplitude:
            signal += self.sine_amplitude * math.sin(2 * math.pi * self.sine_frequency_hz * seconds)
        if self.noise:
            signal += source.gauss(0.0, self.noise)
        return signal
