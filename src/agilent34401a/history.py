"""The History: a bounded ring of recent Readings with Break Markers, and the statistics over it. No I/O."""

import math
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from agilent34401a.math_operations import MathOperation
from agilent34401a.meter import Function, Reading, Setup

DEFAULT_HISTORY_LENGTH = 10_000
"""How many Readings the History keeps unless told otherwise; the same as the setting's default."""


@dataclass(frozen=True)
class HistoryEntry:
    """One Reading in the History.

    `number` counts Readings from 1 since the History was last cleared, so it does not change when older Readings
    are forgotten. `timestamp` is the `time.monotonic()` seconds the Worker stamped on the Reading. A True
    `follows_break` means a Break Marker sits just before this Reading. `setup` and `taken_at` (the wall-clock time)
    are what a CSV export needs besides the Reading itself; they are None for a Reading added without them.
    """

    number: int
    reading: Reading
    timestamp: float
    follows_break: bool = False
    setup: Setup | None = None
    taken_at: datetime | None = None


@dataclass(frozen=True)
class Statistics:
    """The numbers over the latest run of Readings of one Function in the History.

    Overload Readings are not numbers: they are left out of `count` and everything after it, and counted in
    `overloads`. When there are no numbers (or, for `std_dev`, fewer than two) the figure is None.
    `std_dev` is the sample standard deviation, which is what the Meter's own Statistics reports.
    """

    function: Function | None
    count: int
    overloads: int
    mean: float | None
    std_dev: float | None
    minimum: float | None
    maximum: float | None
    peak_to_peak: float | None


_NO_STATISTICS = Statistics(
    function=None, count=0, overloads=0, mean=None, std_dev=None, minimum=None, maximum=None, peak_to_peak=None
)


def _check_length(length: int) -> int:
    if length < 1:
        message = f"The History must hold at least 1 Reading, not {length}"
        raise ValueError(message)
    return length


def _breaks(before: Reading, after: Reading) -> bool:
    """Whether Readings of these two kinds are not comparable: the Function, its unit or what Null/dB/dBm did changed."""
    return before.function is not after.function or _meaning(before) != _meaning(after)


def _meaning(reading: Reading) -> tuple[str, MathOperation | None]:
    """Return what the number in a Reading is: its unit, and the Operation that changed it, if one did."""
    changed_by = reading.math if reading.math is not None and reading.math.changes_the_reading else None
    return reading.unit, changed_by


class History:
    """The application's rolling record of recent Readings, oldest first.

    Fed from the GUI thread only; it does no locking. Read the Readings by iterating (each item is a
    `HistoryEntry`) or with `entries()`.
    """

    def __init__(self, length: int = DEFAULT_HISTORY_LENGTH) -> None:
        self._length = _check_length(length)
        self._entries: deque[HistoryEntry] = deque()
        self._count = 0  # Readings added since the last clear
        self._started_at: float | None = None
        self._version = 0
        self._statistics: tuple[int, Statistics] | None = None

    @property
    def length(self) -> int:
        """How many Readings are kept. Making it shorter forgets the oldest Readings at once."""
        return self._length

    @length.setter
    def length(self, length: int) -> None:
        self._length = _check_length(length)
        if len(self._entries) > length:
            while len(self._entries) > length:
                self._entries.popleft()
            self._version += 1

    @property
    def started_at(self) -> float | None:
        """The timestamp of the first Reading since the History was cleared, even once it has been forgotten."""
        return self._started_at

    @property
    def version(self) -> int:
        """A number that changes whenever the content does, so a view can tell if it needs redrawing."""
        return self._version

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[HistoryEntry]:
        return iter(self._entries)

    def entries(self) -> tuple[HistoryEntry, ...]:
        """Return a snapshot of the Readings, oldest first."""
        return tuple(self._entries)

    def add(
        self, reading: Reading, timestamp: float, *, setup: Setup | None = None, taken_at: datetime | None = None
    ) -> HistoryEntry:
        """Record a Reading, forgetting the oldest if the History is full, and return its entry."""
        follows_break = bool(self._entries) and _breaks(self._entries[-1].reading, reading)
        if self._started_at is None:
            self._started_at = timestamp
        self._count += 1
        entry = HistoryEntry(self._count, reading, timestamp, follows_break, setup, taken_at)
        self._entries.append(entry)
        if len(self._entries) > self._length:
            self._entries.popleft()
        self._version += 1
        return entry

    def clear(self) -> None:
        """Forget every Reading and start counting Readings from 1 again."""
        self._entries.clear()
        self._count = 0
        self._started_at = None
        self._version += 1

    def break_markers(self) -> tuple[HistoryEntry, ...]:
        """Return the entries a Break Marker sits in front of. The oldest Reading has nothing before it to break."""
        return tuple(entry for index, entry in enumerate(self._entries) if entry.follows_break and index > 0)

    def statistics(self) -> Statistics:
        """Return the statistics over the Readings since the latest Break Marker.

        Readings of different Functions are never mixed: volts and ohms have no common mean.
        """
        if self._statistics is None or self._statistics[0] != self._version:
            self._statistics = (self._version, _statistics_of(self._latest_run()))
        return self._statistics[1]

    def _latest_run(self) -> list[Reading]:
        run: list[Reading] = []
        for entry in reversed(self._entries):
            run.append(entry.reading)
            if entry.follows_break:
                break
        run.reverse()
        return run


def _statistics_of(readings: list[Reading]) -> Statistics:
    if not readings:
        return _NO_STATISTICS
    values = [reading.value for reading in readings if not reading.is_overload]
    overloads = len(readings) - len(values)
    function = readings[-1].function
    if not values:
        return Statistics(function, 0, overloads, None, None, None, None, None)
    count = len(values)
    mean = math.fsum(values) / count
    # Two passes keep a small spread on a large offset accurate, which one pass of sums of squares would not.
    std_dev = math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (count - 1)) if count > 1 else None
    minimum = min(values)
    maximum = max(values)
    return Statistics(function, count, overloads, mean, std_dev, minimum, maximum, maximum - minimum)
