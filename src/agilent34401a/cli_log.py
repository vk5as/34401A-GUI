"""The `log` subcommand's logic: take Readings and write them as CSV to a file or stdout.

Rows are flushed as they are written, so an interrupted or failed log keeps everything taken so far. The Meter's
current Setup decides what is measured; set it up first (or with `read`). The first Reading is at `elapsed_s` 0.

A Reading that is lost (the Meter's reply is garbled, or does not come in time) is reported on stderr as a warning and
the log goes on after the Connection has been resynchronised, as Continuous does in the window. A lost Reading is not
a row, so `--count` still ends with that many good ones. After `max_consecutive_failures` (the Worker's limit, 5)
lost in a row, or as soon as the Meter does not answer the resynchronisation, the Meter is taken to be gone and the
log ends with an error and exit code 1.
"""

import io
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from agilent34401a.csv_log import CsvWriter, Recording
from agilent34401a.driver import Driver
from agilent34401a.errors import MalformedReplyError, RepeatedFailureError, TransportTimeoutError
from agilent34401a.meter import reading_timeout
from agilent34401a.resync import Sentinel, resynchronise
from agilent34401a.transport import Transport
from agilent34401a.worker import DEFAULT_MAX_CONSECUTIVE_FAILURES

USAGE_ERROR = 2
INTERRUPTED = 130  # 128 + SIGINT, the usual exit status of a program stopped with Ctrl+C


def _local_now() -> datetime:
    return datetime.now().astimezone()


def log(  # noqa: PLR0913 - the clocks and streams are what tests replace
    driver: Driver,
    transport: Transport,
    *,
    count: int | None,
    duration_s: float | None,
    output: Path | None,
    prog: str,
    stdout: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = _local_now,
) -> int:
    """Take Readings until `count` of them are in or `duration_s` seconds have passed, whichever is first.

    The CSV goes to the file `output`, or to `stdout` (the process's own by default) when that is None. Returns the
    exit code: 0 when the log is complete (even if some Readings were lost along the way: each is a warning), 1 when
    the file cannot be written, 2 when neither limit was given, and 130 when Ctrl+C ended it. A failure talking to the
    Meter, including `RepeatedFailureError` after too many lost Readings in a row, is raised for the caller to report,
    after the file is closed.
    """
    if count is None and duration_s is None:
        sys.stderr.write(f"{prog}: error: give --count or --duration (or both) to say when the log ends\n")
        return USAGE_ERROR
    sentinel = Sentinel("*IDN?", driver.identify().raw)
    transport.timeout = reading_timeout(driver.read_setup())
    try:
        recording = _start(output, sys.stdout if stdout is None else stdout)
    except OSError as error:
        sys.stderr.write(f"{prog}: error: cannot write {output}: {error}\n")
        return 1
    started = clock()
    lost = 0
    in_a_row = 0
    try:
        while (count is None or recording.rows_written < count) and (
            duration_s is None or clock() - started < duration_s
        ):
            try:
                reading = driver.read()
            except (MalformedReplyError, TransportTimeoutError) as error:
                lost += 1
                in_a_row += 1
                sys.stderr.write(f"{prog}: warning: a Reading was lost ({error}); carrying on\n")
                if in_a_row >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                    message = f"{in_a_row} replies in a row were lost or garbled, the last: {error}"
                    raise RepeatedFailureError(message) from error
                resynchronise(transport, sentinel)
                continue
            in_a_row = 0
            recording.add(reading, clock(), setup=driver.setup, taken_at=wall_clock())
    except KeyboardInterrupt:
        return INTERRUPTED
    except OSError as error:
        sys.stderr.write(f"{prog}: error: cannot write {output or 'standard output'}: {error}\n")
        return 1
    finally:
        recording.stop()
    if lost:
        which = "1 Reading was" if lost == 1 else f"{lost} Readings were"
        sys.stderr.write(f"{prog}: warning: {which} lost during the log\n")
    return 0


def _start(output: Path | None, stdout: TextIO) -> Recording:
    if output is not None:
        return Recording.start(output)
    if isinstance(stdout, io.TextIOWrapper):
        stdout.reconfigure(encoding="utf-8")  # the Ω unit would not survive a Windows code page
    return Recording(CsvWriter(stdout))
