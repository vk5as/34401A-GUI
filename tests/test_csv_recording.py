import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agilent34401a.csv_log import CsvWriter, Recording
from agilent34401a.meter import Function, Reading, Setup

START = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def reading(value: float, function: Function = Function.DC_VOLTAGE) -> Reading:
    return Reading(value=value, function=function, raw=f"{value:+.8E}")


def rows_of(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


def test_elapsed_time_counts_from_the_first_reading_recorded(tmp_path: Path):
    path = tmp_path / "rec.csv"
    recording = Recording.start(path)
    try:
        recording.add(reading(1.0), 500.0, taken_at=START)
        recording.add(reading(2.0), 500.25, taken_at=START + timedelta(seconds=0.25))
    finally:
        recording.stop()

    rows = rows_of(path.read_text(encoding="utf-8"))
    assert [(row["timestamp_iso"], row["elapsed_s"], row["value"]) for row in rows] == [
        ("2026-03-04T05:06:07.000+00:00", "0.000", "1.0"),
        ("2026-03-04T05:06:07.250+00:00", "0.250", "2.0"),
    ]


def test_a_recording_keeps_going_across_function_changes(tmp_path: Path):
    path = tmp_path / "rec.csv"
    recording = Recording.start(path)
    try:
        recording.add(reading(1.0), 0.0, setup=Setup.default(Function.DC_VOLTAGE))
        recording.add(reading(100.0, Function.RESISTANCE_2W), 1.0, setup=Setup.default(Function.RESISTANCE_2W))
    finally:
        recording.stop()

    assert [(row["function"], row["range"], row["unit"]) for row in rows_of(path.read_text(encoding="utf-8"))] == [
        ("DC V", "auto", "V"),
        ("2-wire Ω", "auto", "Ω"),
    ]


def test_a_recording_knows_its_file_and_how_many_rows_it_has_written(tmp_path: Path):
    path = tmp_path / "rec.csv"
    recording = Recording.start(path)
    try:
        recording.add(reading(1.0), 0.0)
        recording.add(reading(2.0), 1.0)

        assert (recording.path, recording.rows_written) == (path, 2)
    finally:
        recording.stop()


def test_stopping_a_recording_closes_the_file_and_may_be_done_twice(tmp_path: Path):
    recording = Recording.start(tmp_path / "rec.csv")

    recording.stop()
    recording.stop()

    assert not recording.active


def test_a_recording_to_a_stream_writes_there_and_leaves_it_open():
    stream = io.StringIO()
    recording = Recording(CsvWriter(stream))

    recording.add(reading(1.0), 0.0)
    recording.stop()

    assert [row["value"] for row in rows_of(stream.getvalue())] == ["1.0"]
    assert not stream.closed


def test_a_recording_that_cannot_start_raises_os_error(tmp_path: Path):
    with pytest.raises(OSError, match="nowhere"):
        Recording.start(tmp_path / "nowhere" / "rec.csv")
