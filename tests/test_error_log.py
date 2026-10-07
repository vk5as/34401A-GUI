from datetime import UTC, datetime

import pytest

from agilent34401a.driver import QueuedError
from agilent34401a.error_log import ErrorLog, LoggedError

NOON = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


def test_the_log_keeps_every_error_with_the_time_it_was_reported_oldest_first():
    log = ErrorLog()

    log.add([QueuedError(-113, "Undefined header"), QueuedError(-222, "Data out of range")], NOON)
    log.add([QueuedError(-109, "Missing parameter")], datetime(2026, 10, 8, 12, 0, 5, tzinfo=UTC))

    assert [(entry.timestamp.second, entry.code) for entry in log.entries] == [(0, -113), (0, -222), (5, -109)]
    assert len(log) == 3


def test_an_entry_reads_as_time_code_and_message():
    entry = LoggedError(NOON, -113, "Undefined header")

    assert entry.format() == "2026-10-08 12:00:00  -113  Undefined header"


def test_the_log_as_text_has_one_line_per_error():
    log = ErrorLog()
    log.add([QueuedError(-113, "Undefined header"), QueuedError(-109, "Missing parameter")], NOON)

    assert log.text().splitlines() == [
        "2026-10-08 12:00:00  -113  Undefined header",
        "2026-10-08 12:00:00  -109  Missing parameter",
    ]


def test_the_log_drops_its_oldest_errors_beyond_its_limit():
    log = ErrorLog(max_entries=2)

    log.add([QueuedError(code, "x") for code in (1, 2, 3)], NOON)

    assert [entry.code for entry in log.entries] == [2, 3]


def test_clearing_empties_the_log():
    log = ErrorLog()
    log.add([QueuedError(-113, "Undefined header")], NOON)

    log.clear()

    assert len(log) == 0
    assert log.text() == ""


def test_adding_returns_the_entries_it_made_so_a_view_can_append_them():
    log = ErrorLog()

    added = log.add([QueuedError(-113, "Undefined header")], NOON)

    assert added == [LoggedError(NOON, -113, "Undefined header")]


def test_a_limit_below_one_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        ErrorLog(max_entries=0)
