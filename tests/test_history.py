import math
from dataclasses import replace

import pytest

from agilent34401a.history import History, Statistics
from agilent34401a.math_operations import LimitResult, MathOperation
from agilent34401a.meter import Function, Reading, parse_reading

OVERLOAD = "+9.90000000E+37"


def reading(value: float, function: Function = Function.DC_VOLTAGE) -> Reading:
    return Reading(value=value, function=function, raw=f"{value:+.8E}")


def overload(function: Function = Function.DC_VOLTAGE) -> Reading:
    return parse_reading(OVERLOAD, function)


def filled(values: list[float], length: int = 100, function: Function = Function.DC_VOLTAGE) -> History:
    history = History(length)
    for index, value in enumerate(values):
        history.add(reading(value, function), float(index))
    return history


def values_of(history: History) -> list[float]:
    return [entry.reading.value for entry in history]


# --- bounds -----------------------------------------------------------------------------------------------------


def test_history_starts_empty():
    history = History(10)

    assert len(history) == 0
    assert list(history) == []


def test_history_keeps_readings_with_their_timestamps_in_the_order_they_arrived():
    history = filled([1.0, 2.0, 3.0])

    assert [(entry.reading.value, entry.timestamp) for entry in history] == [(1.0, 0.0), (2.0, 1.0), (3.0, 2.0)]


def test_history_is_a_bounded_ring_that_forgets_the_oldest_reading_first():
    history = filled([1.0, 2.0, 3.0, 4.0, 5.0], length=3)

    assert values_of(history) == [3.0, 4.0, 5.0]
    assert len(history) == 3


def test_history_default_length_matches_the_settings_default():
    assert History().length == 10_000


def test_sample_numbers_count_from_one_and_survive_the_oldest_readings_being_forgotten():
    history = filled([1.0, 2.0, 3.0, 4.0], length=2)

    assert [entry.sample for entry in history] == [3, 4]


def test_clear_empties_the_history_and_restarts_sample_numbers():
    history = filled([1.0, 2.0, 3.0])

    history.clear()
    entry = history.add(reading(9.0), 10.0)

    assert values_of(history) == [9.0]
    assert entry.sample == 1


def test_shortening_the_history_drops_the_oldest_readings_at_once():
    history = filled([1.0, 2.0, 3.0, 4.0])

    history.length = 2

    assert values_of(history) == [3.0, 4.0]


def test_lengthening_the_history_keeps_what_it_has_and_makes_room():
    history = filled([1.0, 2.0], length=2)

    history.length = 4
    history.add(reading(3.0), 2.0)
    history.add(reading(4.0), 3.0)

    assert values_of(history) == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.parametrize("length", [0, -5])
def test_history_length_must_be_at_least_one(length):
    with pytest.raises(ValueError, match="at least 1"):
        History(length)
    history = History(3)
    with pytest.raises(ValueError, match="at least 1"):
        history.length = length


def test_version_changes_whenever_the_content_changes():
    history = History(2)
    seen = {history.version}

    history.add(reading(1.0), 0.0)
    seen.add(history.version)
    history.add(reading(2.0), 1.0)
    seen.add(history.version)
    history.clear()
    seen.add(history.version)

    assert len(seen) == 4


# --- Break Markers ----------------------------------------------------------------------------------------------


def test_no_break_marker_while_the_function_stays_the_same():
    history = filled([1.0, 2.0, 3.0])

    assert history.break_markers() == ()


def test_break_marker_sits_before_the_first_reading_of_a_new_function():
    history = History(10)
    history.add(reading(1.0), 0.0)
    history.add(reading(2.0), 1.0)
    history.add(reading(100.0, Function.RESISTANCE_2W), 2.0)
    history.add(reading(101.0, Function.RESISTANCE_2W), 3.0)

    assert [entry.sample for entry in history.break_markers()] == [3]


def test_changing_function_to_one_with_the_same_unit_still_breaks_the_history():
    history = History(10)
    history.add(reading(1.0), 0.0)
    history.add(reading(1.0, Function.AC_VOLTAGE), 1.0)

    assert [entry.sample for entry in history.break_markers()] == [2]


def test_switching_to_dbm_or_back_breaks_the_history_because_the_unit_changed():
    history = History(10)
    history.add(reading(1.0), 0.0)
    history.add(replace(reading(2.2), math=MathOperation.DBM), 1.0)
    history.add(replace(reading(2.3), math=MathOperation.DBM), 2.0)
    history.add(reading(1.0), 3.0)

    assert [entry.sample for entry in history.break_markers()] == [2, 4]


def test_switching_null_on_or_off_breaks_the_history_because_the_readings_no_longer_compare():
    history = History(10)
    history.add(reading(1.0), 0.0)
    history.add(replace(reading(0.25), math=MathOperation.NULL), 1.0)
    history.add(replace(reading(0.26), math=MathOperation.NULL), 2.0)
    history.add(reading(1.0), 3.0)

    assert [entry.sample for entry in history.break_markers()] == [2, 4]


def test_statistics_and_limit_tests_leave_the_readings_comparable():
    history = History(10)
    history.add(reading(1.0), 0.0)
    history.add(replace(reading(1.0), math=MathOperation.STATISTICS), 1.0)
    history.add(replace(reading(1.0), math=MathOperation.LIMIT_TEST, limit=LimitResult.PASS), 2.0)

    assert history.break_markers() == ()


def test_each_change_of_function_gets_its_own_break_marker():
    history = History(10)
    for index, function in enumerate(
        [Function.DC_VOLTAGE, Function.FREQUENCY, Function.FREQUENCY, Function.DC_VOLTAGE]
    ):
        history.add(reading(1.0, function), float(index))

    assert [entry.sample for entry in history.break_markers()] == [2, 4]


def test_a_break_marker_is_dropped_with_the_readings_around_it_once_they_are_forgotten():
    history = History(2)
    history.add(reading(1.0), 0.0)
    history.add(reading(2.0, Function.FREQUENCY), 1.0)
    history.add(reading(3.0, Function.FREQUENCY), 2.0)

    # The first visible Reading has nothing before it to break from.
    assert history.break_markers() == ()


def test_the_first_reading_after_clear_has_no_break_marker():
    history = History(5)
    history.add(reading(1.0), 0.0)
    history.clear()
    history.add(reading(2.0, Function.FREQUENCY), 1.0)

    assert history.break_markers() == ()


def test_overload_readings_do_not_cause_break_markers():
    history = History(10)
    history.add(reading(1.0), 0.0)
    history.add(overload(), 1.0)
    history.add(reading(2.0), 2.0)

    assert history.break_markers() == ()


# --- statistics -------------------------------------------------------------------------------------------------


def test_statistics_of_an_empty_history_have_no_numbers():
    statistics = History(5).statistics()

    assert statistics == Statistics(
        function=None, count=0, overloads=0, mean=None, std_dev=None, minimum=None, maximum=None, peak_to_peak=None
    )


def test_statistics_cover_n_mean_min_max_and_peak_to_peak():
    statistics = filled([1.0, 2.0, 3.0, 4.0]).statistics()

    assert statistics.function is Function.DC_VOLTAGE
    assert statistics.count == 4
    assert statistics.mean == pytest.approx(2.5)
    assert statistics.minimum == 1.0
    assert statistics.maximum == 4.0
    assert statistics.peak_to_peak == 3.0


def test_standard_deviation_is_the_sample_standard_deviation_like_the_meters_own():
    statistics = filled([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]).statistics()

    assert statistics.std_dev == pytest.approx(math.sqrt(32 / 7))


def test_a_single_reading_has_no_standard_deviation_but_does_have_the_other_numbers():
    statistics = filled([3.0]).statistics()

    assert (statistics.count, statistics.mean, statistics.minimum, statistics.maximum, statistics.peak_to_peak) == (
        1,
        3.0,
        3.0,
        3.0,
        0.0,
    )
    assert statistics.std_dev is None


def test_statistics_stay_accurate_for_a_tiny_spread_on_a_large_offset():
    offset = 1e9
    statistics = filled([offset + 1e-3, offset - 1e-3, offset + 1e-3, offset - 1e-3]).statistics()

    assert statistics.std_dev == pytest.approx(1.1547e-3, rel=1e-3)


def test_overload_readings_are_left_out_of_the_numbers_and_counted_apart():
    history = History(10)
    for index, item in enumerate([reading(1.0), overload(), reading(3.0), overload(), overload()]):
        history.add(item, float(index))

    statistics = history.statistics()

    assert statistics.count == 2
    assert statistics.overloads == 3
    assert statistics.mean == pytest.approx(2.0)
    assert statistics.maximum == 3.0


def test_a_history_of_only_overload_readings_has_a_count_of_zero_and_no_numbers():
    history = History(10)
    history.add(overload(), 0.0)

    statistics = history.statistics()

    assert (statistics.count, statistics.overloads, statistics.mean, statistics.peak_to_peak) == (0, 1, None, None)
    assert statistics.function is Function.DC_VOLTAGE


def test_statistics_only_cover_the_latest_function_so_volts_are_never_averaged_with_ohms():
    history = History(10)
    for index, value in enumerate([1.0, 2.0, 3.0]):
        history.add(reading(value), float(index))
    for index, value in enumerate([1000.0, 2000.0]):
        history.add(reading(value, Function.RESISTANCE_2W), float(10 + index))

    statistics = history.statistics()

    assert statistics.function is Function.RESISTANCE_2W
    assert statistics.count == 2
    assert statistics.mean == pytest.approx(1500.0)


def test_statistics_follow_the_history_as_old_readings_are_forgotten():
    history = filled([100.0, 1.0, 2.0, 3.0], length=3)

    assert history.statistics().maximum == 3.0
    assert history.statistics().count == 3


def test_statistics_are_recalculated_only_when_the_history_changed():
    history = filled([1.0, 2.0])
    first = history.statistics()

    assert history.statistics() is first
    history.add(reading(3.0), 5.0)
    assert history.statistics() is not first


# --- time origin ------------------------------------------------------------------------------------------------


def started_at(history: History) -> float | None:
    return history.started_at  # a function call, so a check on it is not remembered by the type checker


def test_started_at_is_the_timestamp_of_the_first_reading_since_the_history_was_cleared():
    history = History(2)
    assert started_at(history) is None  # nothing yet

    history.add(reading(1.0), 5.0)
    history.add(reading(2.0), 6.0)
    history.add(reading(3.0), 7.0)  # the first Reading is forgotten, but the History still began at 5.0
    assert started_at(history) == 5.0

    history.clear()
    history.add(reading(4.0), 20.0)
    assert started_at(history) == 20.0
