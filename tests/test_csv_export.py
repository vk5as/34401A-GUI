import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agilent34401a.csv_log import COLUMNS, export_history
from agilent34401a.history import History
from agilent34401a.meter import Function, Reading, Setup, parse_reading

START = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def reading(value: float, function: Function = Function.DC_VOLTAGE) -> Reading:
    return Reading(value=value, function=function, raw=f"{value:+.8E}")


def rows_of(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_an_exported_history_has_a_row_per_reading_oldest_first_with_the_header(tmp_path: Path):
    history = History(10)
    setup = Setup.default(Function.DC_VOLTAGE).with_range(10.0)
    for index, value in enumerate([1.0, 2.0, 3.0]):
        history.add(reading(value), 100.0 + index, setup=setup, taken_at=START + timedelta(seconds=index))

    count = export_history(history, tmp_path / "history.csv")

    rows = rows_of(tmp_path / "history.csv")
    assert count == 3
    assert list(rows[0]) == list(COLUMNS)
    assert [(row["timestamp_iso"], row["elapsed_s"], row["value"], row["range"]) for row in rows] == [
        ("2026-03-04T05:06:07.000+00:00", "0.000", "1.0", "10"),
        ("2026-03-04T05:06:08.000+00:00", "1.000", "2.0", "10"),
        ("2026-03-04T05:06:09.000+00:00", "2.000", "3.0", "10"),
    ]


def test_elapsed_time_counts_from_the_first_reading_even_after_it_was_forgotten(tmp_path: Path):
    history = History(2)
    for index in range(4):
        history.add(reading(float(index)), 50.0 + index * 0.5)

    export_history(history, tmp_path / "history.csv")

    assert [row["elapsed_s"] for row in rows_of(tmp_path / "history.csv")] == ["1.000", "1.500"]


def test_an_exported_history_keeps_overloads_and_readings_of_different_functions(tmp_path: Path):
    history = History(10)
    history.add(parse_reading("+9.90000000E+37", Function.DC_VOLTAGE), 0.0)
    history.add(reading(100.0, Function.RESISTANCE_2W), 1.0)

    export_history(history, tmp_path / "history.csv")

    assert [(row["function"], row["value"], row["unit"]) for row in rows_of(tmp_path / "history.csv")] == [
        ("DC V", "OVLD", "V"),
        ("2-wire Ω", "100.0", "Ω"),
    ]


def test_an_empty_history_exports_just_the_header(tmp_path: Path):
    assert export_history(History(10), tmp_path / "history.csv") == 0

    assert rows_of(tmp_path / "history.csv") == []
    assert (tmp_path / "history.csv").read_text(encoding="utf-8").strip() == ",".join(COLUMNS)
