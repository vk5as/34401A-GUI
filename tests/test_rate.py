import pytest

from agilent34401a.rate import ReadingRate


def test_rate_is_unknown_until_two_readings_have_arrived():
    meter = ReadingRate()
    assert meter.per_second() is None

    meter.add(10.0)

    assert meter.per_second() is None


def test_rate_is_the_readings_per_second_between_the_first_and_last_reading():
    meter = ReadingRate()

    for timestamp in (0.0, 0.5, 1.0, 1.5, 2.0):
        meter.add(timestamp)

    assert meter.per_second() == pytest.approx(2.0)


def test_rate_only_looks_at_the_most_recent_readings():
    meter = ReadingRate(window=3)

    for timestamp in (0.0, 100.0, 100.1, 100.2):
        meter.add(timestamp)

    assert meter.per_second() == pytest.approx(10.0)


def test_rate_is_unknown_when_all_readings_share_a_timestamp():
    meter = ReadingRate()

    meter.add(5.0)
    meter.add(5.0)

    assert meter.per_second() is None


def test_reset_forgets_every_reading():
    meter = ReadingRate()
    meter.add(0.0)
    meter.add(1.0)

    meter.reset()

    assert meter.per_second() is None


def test_window_must_hold_at_least_two_readings():
    with pytest.raises(ValueError, match="window"):
        ReadingRate(window=1)
