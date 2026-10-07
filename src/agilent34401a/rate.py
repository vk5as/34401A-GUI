"""Reading rate for the status bar."""

from collections import deque

_DEFAULT_WINDOW = 10
_MIN_TIMESTAMPS = 2  # a rate needs two points


class ReadingRate:
    """Readings per second over the most recent Readings, from the times they arrived."""

    def __init__(self, window: int = _DEFAULT_WINDOW) -> None:
        if window < _MIN_TIMESTAMPS:
            message = f"The rate window must hold at least {_MIN_TIMESTAMPS} Readings, got {window}"
            raise ValueError(message)
        self._timestamps: deque[float] = deque(maxlen=window)

    def add(self, timestamp: float) -> None:
        """Record that a Reading arrived at `timestamp` (seconds, any monotonic clock)."""
        self._timestamps.append(timestamp)

    def reset(self) -> None:
        self._timestamps.clear()

    def per_second(self) -> float | None:
        """Return the Readings per second, or None until two Readings with different timestamps have arrived."""
        if len(self._timestamps) < _MIN_TIMESTAMPS:
            return None
        elapsed = self._timestamps[-1] - self._timestamps[0]
        if elapsed <= 0:
            return None
        return (len(self._timestamps) - 1) / elapsed
