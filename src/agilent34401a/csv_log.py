"""CSV for Readings: the row format and a writer that survives a crash. Used by Recording, History export and the CLI.

The columns are `timestamp_iso, elapsed_s, function, range, value, unit, raw, math_mode, limit_result`.

- `timestamp_iso` is the wall-clock time the Reading was taken, ISO 8601 to the millisecond (with its UTC offset when
  the time has one); `elapsed_s` is the seconds since the Recording, log or History began.
- `function` is the Function's label ("DC V"), `unit` its unit; `value` is in that unit, never scaled with a prefix.
- `range` is the Range as a plain number in the unit of the Function's input (volts for frequency), `auto` for
  Autorange, and empty for a Function with no Range choice or when the Setup is not known.
- An Overload has `OVLD` for its value; `raw` always holds the Raw Reading exactly as the Meter sent it.
- `math_mode` and `limit_result` are filled from `math_mode_text` and `limit_result_text`, the one place that has to
  learn about Math Operations. Until the Meter's Math is modelled they are empty.

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
OVERLOAD_TEXT = "OVLD"
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


def math_mode_text(setup: Setup) -> str:  # noqa: ARG001 - the Setup will carry the Math Operation
    """Name the Math Operation that was active when the Reading was taken, or "" for none.

    The Setup has no Math settings yet; when it does, this is the one place to turn them into the column's text.
    """
    return ""


def limit_result_text(reading: Reading) -> str:  # noqa: ARG001 - the Reading will carry the Limit Test result
    """Say how the Reading did in a Limit Test ("HI", "LO", "PASS"), or "" when no Limit Test was running."""
    return ""


def csv_row(item: LoggedReading) -> list[str]:
    """Return the fields of one row, in the order of `COLUMNS`."""
    reading = item.reading
    setup = item.setup
    return [
        "" if item.taken_at is None else item.taken_at.isoformat(timespec="milliseconds"),
        f"{item.elapsed_s:.3f}",
        reading.function.label,
        "" if setup is None else _range_text(setup),
        OVERLOAD_TEXT if reading.is_overload else repr(reading.value),
        reading.function.unit,
        reading.raw.strip(),
        "" if setup is None else math_mode_text(setup),
        limit_result_text(reading),
    ]


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
