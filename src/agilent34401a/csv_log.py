"""CSV for Readings: the row format and a writer that survives a crash. Used by Recording, History export and the CLI.

The columns are `timestamp_iso, elapsed_s, function, range, value, unit, raw, math_mode, limit_result`.

- `timestamp_iso` is the wall-clock time the Reading was taken, ISO 8601 to the millisecond (with its UTC offset when
  the time has one); `elapsed_s` is the seconds since the Recording, log or History began.
- `function` is the Function's label ("DC V"), `unit` its unit (dB or dBm under those Operations); `value` is in that unit,
  never scaled with a prefix.
- `range` is the Range as a plain number in the unit of the Function's input (volts for frequency), `auto` for
  Autorange, and empty for a Function with no Range choice or when the Setup is not known.
- An Overload's `value` is the Meter's own number (+9.9e+37 or -9.9e+37, so the column stays numeric and the sign is
  kept); the window shows it as OVLD, the CSV does not. `raw` always holds the Raw Reading exactly as the Meter sent it.
- `math_mode` is the Math Operation in effect (`NULL`, `DB`, `DBM`, `STATS` or `LIMIT`), empty for none; `limit_result`
  is `HI`, `LO` or `PASS` while a Limit Test runs, empty otherwise. They are made by `math_mode_text` and
  `limit_result_text`. With Null the `value` is the Reading minus the offset, and with dB or dBm it is in dB or dBm and
  `unit` says so; `raw` is still exactly what the Meter sent.

The file is UTF-8 (the Ω unit), with LF line endings on every platform, and is flushed after every row.

Anything that has Readings to save (a Burst, say) writes them with `write_csv`, or row by row with a `CsvWriter`.
"""

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TextIO, cast

from agilent34401a.history import History
from agilent34401a.meter import Reading, Setup

COLUMNS = (
    "timestamp_iso",
    "elapsed_s",
    "function",
    "range",
    "value",
    "unit",
    "raw",
    "math_mode",
    "limit_result",
)
AUTORANGE_TEXT = "auto"
_ENCODING = "utf-8"
_LINE_ENDING = "\n"


@dataclass(frozen=True)
class LoggedReading:
    """One Reading and the context its row needs. `taken_at` and `setup` are None where they are not known."""

    reading: Reading
    elapsed_s: float
    taken_at: datetime | None = None
    setup: Setup | None = None


def math_mode_text(setup: Setup) -> str:
    """Name the Math Operation that was active when the Reading was taken (NULL, DB, DBM, STATS, LIMIT), or "" for none."""
    return "" if setup.math.operation is None else setup.math.operation.csv_name


def limit_result_text(reading: Reading) -> str:
    """Say how the Reading did in a Limit Test ("HI", "LO", "PASS"), or "" when no Limit Test was running."""
    return "" if reading.limit is None else reading.limit.label


def csv_row(item: LoggedReading) -> list[str]:
    """Return the fields of one row, in the order of `COLUMNS`."""
    reading = item.reading
    setup = item.setup
    return [
        "" if item.taken_at is None else item.taken_at.isoformat(timespec="milliseconds"),
        f"{item.elapsed_s:.3f}",
        reading.function.label,
        "" if setup is None else _range_text(setup),
        repr(reading.value),
        reading.unit,
        reading.raw.strip(),
        _math_mode(item),
        limit_result_text(reading),
    ]


def _math_mode(item: LoggedReading) -> str:
    if item.setup is not None:
        return math_mode_text(item.setup)
    return "" if item.reading.math is None else item.reading.math.csv_name  # the Reading knows when the Setup is not


def _range_text(setup: Setup) -> str:
    if not setup.function.ranges:
        return ""
    if setup.range is None:
        return AUTORANGE_TEXT
    return str(int(setup.range)) if setup.range == int(setup.range) else repr(setup.range)


class CsvWriter:
    """Writes Readings as CSV rows to a text stream, flushing after each so a crash loses at most the row in hand.

    The header goes out when the writer is made. Failures of the stream (a full disk) are raised as `OSError`.
    Use `CsvWriter.open(path)` to write a file, which the writer then closes; a stream you pass in stays yours.
    """

    def __init__(self, stream: TextIO, *, path: Path | None = None, owns_stream: bool = False) -> None:
        self._stream = stream
        self._owns_stream = owns_stream
        self._closed = False
        self.path = path
        self.rows_written = 0
        self._csv = csv.writer(stream, lineterminator=_LINE_ENDING)
        try:
            self._csv.writerow(COLUMNS)
            stream.flush()
        except OSError:
            self.close()
            raise

    @classmethod
    def open(cls, path: Path) -> "CsvWriter":
        """Create (or replace) the file at `path` and write the header to it. Raises `OSError` if it cannot."""
        stream = path.open("w", encoding=_ENCODING, newline="")
        return cls(cast("TextIO", stream), path=path, owns_stream=True)

    @property
    def closed(self) -> bool:
        return self._closed

    def write(self, item: LoggedReading) -> None:
        """Append one row and push it to the file."""
        if self._closed:
            message = "The CSV file is closed"
            raise ValueError(message)
        self._csv.writerow(csv_row(item))
        self._stream.flush()
        self.rows_written += 1

    def close(self) -> None:
        """Finish writing; closes the file if the writer opened it. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if self._owns_stream:
            self._stream.close()

    def __enter__(self) -> "CsvWriter":  # noqa: PYI034 - typing.Self needs Python 3.11
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


def write_csv(path: Path, items: Iterable[LoggedReading]) -> int:
    """Save `items` to a new CSV file at `path`; return how many rows it holds. Raises `OSError` if it cannot."""
    with CsvWriter.open(path) as writer:
        for item in items:
            writer.write(item)
        return writer.rows_written


def export_history(history: History, path: Path) -> int:
    """Save the Readings in `history` to a new CSV file at `path`; return how many rows it holds.

    `elapsed_s` counts from the first Reading since the History was cleared, as the Chart's time axis does, even if
    that Reading has since been forgotten. Raises `OSError` if the file cannot be written.
    """
    started_at = history.started_at
    return write_csv(
        path,
        (
            LoggedReading(
                entry.reading,
                elapsed_s=entry.timestamp - (entry.timestamp if started_at is None else started_at),
                taken_at=entry.taken_at,
                setup=entry.setup,
            )
            for entry in history
        ),
    )


class Recording:
    """Readings arriving one by one, written to CSV as they come, with the time counted from the first.

    Create one with `Recording.start(path)` for a file, or `Recording(CsvWriter(stream))` for a stream you keep; call `add` for
    each Reading and `stop` when done. `add` raises `OSError` if the file cannot be written (a full disk, say), which
    the caller should treat as the end of the Recording.
    """

    def __init__(self, writer: CsvWriter) -> None:
        self._writer = writer
        self._first_timestamp: float | None = None

    @classmethod
    def start(cls, path: Path) -> "Recording":
        """Begin a Recording to a new file at `path`, replacing any file there. Raises `OSError` if it cannot."""
        return cls(CsvWriter.open(path))

    @property
    def path(self) -> Path | None:
        return self._writer.path

    @property
    def rows_written(self) -> int:
        return self._writer.rows_written

    @property
    def active(self) -> bool:
        return not self._writer.closed

    def add(
        self, reading: Reading, timestamp: float, *, setup: Setup | None = None, taken_at: datetime | None = None
    ) -> None:
        """Write one Reading. `timestamp` is its `time.monotonic()` seconds, of which only differences matter."""
        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        self._writer.write(LoggedReading(reading, timestamp - self._first_timestamp, taken_at, setup))

    def stop(self) -> None:
        """End the Recording and close its file. Safe to call more than once."""
        self._writer.close()
