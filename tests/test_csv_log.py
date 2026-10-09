import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agilent34401a.csv_log import COLUMNS, CsvWriter, LoggedReading, csv_row, limit_result_text, math_mode_text
from agilent34401a.math_operations import LimitResult, MathOperation, MathSettings
from agilent34401a.meter import Function, Reading, Setup, parse_reading

AT = datetime(2026, 3, 4, 5, 6, 7, 891000, tzinfo=timezone(timedelta(hours=9, minutes=30)))
OVERLOAD = "+9.90000000E+37"


def reading(value: float = 1.5, function: Function = Function.DC_VOLTAGE, raw: str | None = None) -> Reading:
    return Reading(value=value, function=function, raw=f"{value:+.8E}" if raw is None else raw)


def logged(taken: Reading | None = None, **kwargs) -> LoggedReading:
    return LoggedReading(taken or reading(), **{"elapsed_s": 0.0, **kwargs})


def parsed(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


# --- one row ----------------------------------------------------------------------------------------------------


def test_the_columns_are_the_ones_the_spec_names():
    assert COLUMNS == (
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


def test_a_row_holds_the_wall_clock_time_the_elapsed_time_and_what_the_meter_measured():
    setup = Setup.default(Function.DC_VOLTAGE).with_range(10.0)
    row = csv_row(LoggedReading(reading(1.5), elapsed_s=2.5, taken_at=AT, setup=setup))

    assert row == [
        "2026-03-04T05:06:07.891+09:30",
        "2.500",
        "DC V",
        "10",
        "1.5",
        "V",
        "+1.50000000E+00",
        "",
        "",
    ]


def test_value_is_written_in_the_base_unit_with_every_digit_the_float_holds():
    row = csv_row(logged(reading(1.2345678e-05)))

    assert row[COLUMNS.index("value")] == "1.2345678e-05"


def test_the_raw_reading_is_written_exactly_as_the_meter_sent_it_without_the_line_ending():
    row = csv_row(logged(reading(1.5, raw="+1.50000000E+00\r\n")))

    assert row[COLUMNS.index("raw")] == "+1.50000000E+00"


def test_an_overload_reading_has_the_meters_own_number_for_a_value_and_keeps_the_raw_reading():
    row = csv_row(logged(parse_reading(OVERLOAD, Function.DC_VOLTAGE)))

    assert float(row[COLUMNS.index("value")]) == 9.9e37
    assert row[COLUMNS.index("raw")] == OVERLOAD
    assert row[COLUMNS.index("unit")] == "V"


def test_a_negative_overload_keeps_its_sign_in_the_value_column():
    row = csv_row(logged(parse_reading("-9.90000000E+37", Function.DC_VOLTAGE)))

    assert float(row[COLUMNS.index("value")]) == -9.9e37
    assert row[COLUMNS.index("raw")] == "-9.90000000E+37"


@pytest.mark.parametrize(
    ("function", "label", "unit"),
    [
        (Function.AC_CURRENT, "AC I", "A"),
        (Function.RESISTANCE_4W, "4-wire Ω", "Ω"),
        (Function.FREQUENCY, "Frequency", "Hz"),
        (Function.DC_VOLTAGE_RATIO, "DC V ratio", ""),
    ],
)
def test_function_and_unit_columns_name_the_function_and_its_unit(function, label, unit):
    row = csv_row(logged(reading(function=function)))

    assert row[COLUMNS.index("function")] == label
    assert row[COLUMNS.index("unit")] == unit


def test_autorange_is_written_as_auto_and_a_function_with_no_range_choice_as_nothing():
    autorange = csv_row(logged(setup=Setup.default(Function.DC_VOLTAGE)))
    fixed = csv_row(logged(reading(function=Function.DIODE), setup=Setup.default(Function.DIODE)))

    assert autorange[COLUMNS.index("range")] == "auto"
    assert fixed[COLUMNS.index("range")] == ""


def test_a_range_is_written_as_a_number_in_the_units_of_the_functions_input():
    setup = Setup.default(Function.RESISTANCE_2W).with_range(1e6)

    row = csv_row(logged(reading(function=Function.RESISTANCE_2W), setup=setup))

    assert row[COLUMNS.index("range")] == "1000000"


def test_time_and_range_are_empty_when_they_are_not_known():
    row = csv_row(LoggedReading(reading(), elapsed_s=0.0))

    assert row[COLUMNS.index("timestamp_iso")] == ""
    assert row[COLUMNS.index("range")] == ""


def test_a_time_without_a_zone_is_written_as_it_is():
    naive = datetime(2026, 1, 2, 3, 4, 5)  # noqa: DTZ001 - the point is that no zone is attached

    assert csv_row(logged(taken_at=naive))[0] == "2026-01-02T03:04:05.000"


def test_math_mode_and_limit_result_are_empty_when_the_meter_is_not_doing_math():
    setup = Setup.default(Function.DC_VOLTAGE)

    assert math_mode_text(setup) == ""
    assert limit_result_text(reading()) == ""
    row = csv_row(logged(setup=setup))
    assert row[COLUMNS.index("math_mode")] == ""
    assert row[COLUMNS.index("limit_result")] == ""


@pytest.mark.parametrize(
    ("operation", "text"),
    [
        (MathOperation.NULL, "NULL"),
        (MathOperation.DB, "DB"),
        (MathOperation.DBM, "DBM"),
        (MathOperation.STATISTICS, "STATS"),
        (MathOperation.LIMIT_TEST, "LIMIT"),
    ],
)
def test_math_mode_names_the_math_operation_in_effect(operation, text):
    setup = Setup.default(Function.DC_VOLTAGE).with_math(MathSettings(operation=operation))

    assert math_mode_text(setup) == text
    assert csv_row(logged(setup=setup))[COLUMNS.index("math_mode")] == text


@pytest.mark.parametrize(
    ("result", "text"), [(LimitResult.HIGH, "HI"), (LimitResult.LOW, "LO"), (LimitResult.PASS, "PASS"), (None, "")]
)
def test_limit_result_says_how_the_reading_did_in_the_limit_test(result, text):
    taken = Reading(1.5, Function.DC_VOLTAGE, "+1.5E+00", math=MathOperation.LIMIT_TEST, limit=result)

    assert limit_result_text(taken) == text
    assert csv_row(logged(taken))[COLUMNS.index("limit_result")] == text


def test_a_reading_in_dbm_has_the_unit_dbm_and_the_value_the_meter_gave():
    taken = Reading(13.01, Function.DC_VOLTAGE, "+1.30100000E+01", math=MathOperation.DBM)
    setup = Setup.default(Function.DC_VOLTAGE).with_math(MathSettings(operation=MathOperation.DBM))

    row = csv_row(logged(taken, setup=setup))

    assert row[COLUMNS.index("unit")] == "dBm"
    assert row[COLUMNS.index("value")] == "13.01"
    assert row[COLUMNS.index("math_mode")] == "DBM"


def test_a_reading_that_names_its_math_operation_fills_math_mode_even_when_its_setup_is_not_known():
    taken = Reading(0.75, Function.DC_VOLTAGE, "+7.5E-01", math=MathOperation.NULL)

    assert csv_row(logged(taken))[COLUMNS.index("math_mode")] == "NULL"


# --- the writer -------------------------------------------------------------------------------------------------


def test_the_writer_starts_with_the_header_even_if_no_reading_follows():
    stream = io.StringIO()

    CsvWriter(stream)

    assert parsed(stream.getvalue()) == [list(COLUMNS)]


def test_rows_follow_the_header_in_the_order_they_were_written_with_unix_line_endings():
    stream = io.StringIO()
    writer = CsvWriter(stream)

    writer.write(logged(reading(1.0), elapsed_s=0.0))
    writer.write(logged(reading(2.0), elapsed_s=0.5))

    assert stream.getvalue().count("\r") == 0
    assert [row[1:5] for row in parsed(stream.getvalue())[1:]] == [
        ["0.000", "DC V", "", "1.0"],
        ["0.500", "DC V", "", "2.0"],
    ]
    assert writer.rows_written == 2


def test_a_field_with_a_comma_or_quote_is_quoted_so_it_reads_back_unchanged():
    stream = io.StringIO()
    writer = CsvWriter(stream)

    writer.write(logged(reading(1.0, raw='1,0 "odd"\n')))

    assert parsed(stream.getvalue())[1][COLUMNS.index("raw")] == '1,0 "odd"'
    assert '"1,0 ""odd"""' in stream.getvalue()


class FlushCounting(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0
        self.closed_by_writer = False

    def flush(self) -> None:
        self.flushes += 1
        super().flush()

    def close(self) -> None:
        self.closed_by_writer = True
        super().close()


def test_every_row_is_flushed_as_it_is_written():
    stream = FlushCounting()
    writer = CsvWriter(stream)
    after_header = stream.flushes

    writer.write(logged())
    writer.write(logged())

    assert after_header >= 1
    assert stream.flushes == after_header + 2


def test_closing_the_writer_leaves_a_stream_it_was_given_open():
    stream = FlushCounting()
    writer = CsvWriter(stream)

    writer.close()

    assert not stream.closed_by_writer


def test_a_stream_that_fails_while_writing_raises_the_error_to_the_caller():
    class Full(io.StringIO):
        def write(self, _text: str) -> int:
            message = "No space left on device"
            raise OSError(message)

    with pytest.raises(OSError, match="No space"):
        CsvWriter(Full())


# --- files ------------------------------------------------------------------------------------------------------


def test_a_file_has_every_row_on_disk_while_it_is_still_open(tmp_path: Path):
    path = tmp_path / "log.csv"
    writer = CsvWriter.open(path)
    try:
        writer.write(logged(reading(1.0)))

        # Read by someone else before the writer is closed: a crash now would lose nothing.
        assert [row[4] for row in parsed(path.read_text(encoding="utf-8"))[1:]] == ["1.0"]
    finally:
        writer.close()


def test_closing_a_file_writer_closes_the_file_and_may_be_done_twice(tmp_path: Path):
    writer = CsvWriter.open(tmp_path / "log.csv")

    writer.close()
    writer.close()

    with pytest.raises(ValueError, match="closed"):
        writer.write(logged())


def test_a_file_is_written_as_utf_8_so_the_ohm_sign_survives(tmp_path: Path):
    path = tmp_path / "log.csv"
    with CsvWriter.open(path) as writer:
        writer.write(logged(reading(100.0, Function.RESISTANCE_2W)))

    assert "Ω" in path.read_bytes().decode("utf-8")


def test_opening_a_file_replaces_what_was_there(tmp_path: Path):
    path = tmp_path / "log.csv"
    path.write_text("old,data\n", encoding="utf-8")

    CsvWriter.open(path).close()

    assert parsed(path.read_text(encoding="utf-8")) == [list(COLUMNS)]


def test_a_folder_that_does_not_exist_is_an_error_naming_it(tmp_path: Path):
    with pytest.raises(OSError, match="nowhere"):
        CsvWriter.open(tmp_path / "nowhere" / "log.csv")


def test_a_path_that_is_a_folder_cannot_be_opened(tmp_path: Path):
    with pytest.raises(OSError):  # noqa: PT011 - the message is the platform's
        CsvWriter.open(tmp_path)


def test_the_writer_remembers_where_it_writes(tmp_path: Path):
    path = tmp_path / "log.csv"
    with CsvWriter.open(path) as writer:
        assert writer.path == path
