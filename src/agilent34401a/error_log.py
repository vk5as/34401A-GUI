"""The error log: every error the Meter has reported, with when the application learned of it."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from agilent34401a.driver import QueuedError

DEFAULT_MAX_ENTRIES = 1000


@dataclass(frozen=True)
class LoggedError:
    """One Meter error and the time it was reported."""

    timestamp: datetime
    code: int
    message: str

    def format(self) -> str:
        """Return the entry as one line of text: time, code, message."""
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S}  {self.code}  {self.message}"


class ErrorLog:
    """The most recent Meter errors, oldest first. Older ones fall off the front beyond `max_entries`."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            message = f"The error log must hold at least 1 entry, got {max_entries}"
            raise ValueError(message)
        self._entries: deque[LoggedError] = deque(maxlen=max_entries)

    @property
    def entries(self) -> tuple[LoggedError, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, errors: Iterable[QueuedError], when: datetime) -> list[LoggedError]:
        """Record `errors` as reported at `when`; return the entries made."""
        added = [LoggedError(when, error.code, error.message) for error in errors]
        self._entries.extend(added)
        return added

    def clear(self) -> None:
        self._entries.clear()

    def text(self) -> str:
        """Return the whole log, one line per error."""
        return "\n".join(entry.format() for entry in self._entries)
