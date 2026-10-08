"""The trigger model: Trigger Source, Trigger Delay, Sample Count and Trigger Count, and what fits in Reading Memory."""

import dataclasses
import math

import pytest

from agilent34401a.errors import BurstTooLargeError, InvalidSetupError
from agilent34401a.meter import Function, Setup
from agilent34401a.trigger import READING_MEMORY_SIZE, TriggerSettings, TriggerSource


def test_the_default_is_one_immediate_reading_with_an_automatic_delay():
    settings = TriggerSettings()

    assert settings.source is TriggerSource.IMMEDIATE
    assert settings.delay is None
    assert (settings.sample_count, settings.trigger_count) == (1, 1)
    assert settings.is_single_immediate


def test_a_setup_carries_the_trigger_settings_and_starts_with_the_default():
    setup = Setup.default(Function.DC_VOLTAGE)

    assert setup.trigger == TriggerSettings()
    assert setup.with_trigger(TriggerSettings(source=TriggerSource.BUS)).trigger.source is TriggerSource.BUS


@pytest.mark.parametrize("delay", [0, 0.5, 3600])
def test_a_fixed_trigger_delay_from_zero_to_an_hour_is_accepted(delay):
    assert TriggerSettings(delay=delay).delay == delay


@pytest.mark.parametrize("delay", [-0.001, 3600.001, math.nan, math.inf])
def test_a_trigger_delay_outside_zero_to_an_hour_is_refused(delay):
    with pytest.raises(InvalidSetupError, match="Trigger Delay"):
        TriggerSettings(delay=delay)


@pytest.mark.parametrize("count", [1, 512, 50000])
def test_sample_counts_and_trigger_counts_from_one_to_fifty_thousand_are_accepted(count):
    settings = TriggerSettings(sample_count=count, trigger_count=count)

    assert (settings.sample_count, settings.trigger_count) == (count, count)


@pytest.mark.parametrize("count", [0, -1, 50001])
def test_a_sample_count_outside_one_to_fifty_thousand_is_refused(count):
    with pytest.raises(InvalidSetupError, match="Sample Count"):
        TriggerSettings(sample_count=count)


@pytest.mark.parametrize("count", [0, -1, 50001])
def test_a_trigger_count_outside_one_to_fifty_thousand_is_refused(count):
    with pytest.raises(InvalidSetupError, match="Trigger Count"):
        TriggerSettings(trigger_count=count)


def test_the_trigger_count_can_be_infinite():
    settings = TriggerSettings(trigger_count=None)

    assert settings.trigger_count is None
    assert settings.readings is None


def test_the_number_of_readings_is_the_sample_count_times_the_trigger_count():
    assert TriggerSettings(sample_count=10, trigger_count=5).readings == 50


def test_settings_that_are_not_one_immediate_reading_are_not_single_immediate():
    for settings in (
        TriggerSettings(source=TriggerSource.BUS),
        TriggerSettings(sample_count=2),
        TriggerSettings(trigger_count=2),
        TriggerSettings(trigger_count=None),
    ):
        assert not settings.is_single_immediate


def test_a_fixed_delay_does_not_stop_the_settings_being_single_immediate():
    assert TriggerSettings(delay=0.1).is_single_immediate


def test_reading_memory_holds_512_readings():
    assert READING_MEMORY_SIZE == 512


def test_a_burst_of_exactly_reading_memory_fits():
    TriggerSettings(sample_count=512).check_fits_reading_memory()
    TriggerSettings(sample_count=16, trigger_count=32).check_fits_reading_memory()


@pytest.mark.parametrize(
    "settings",
    [
        TriggerSettings(sample_count=513),
        TriggerSettings(sample_count=2, trigger_count=257),
        TriggerSettings(trigger_count=None),
    ],
)
def test_a_burst_larger_than_reading_memory_is_refused_with_a_reason(settings):
    with pytest.raises(BurstTooLargeError, match="512"):
        settings.check_fits_reading_memory()


def test_a_burst_too_large_is_an_invalid_setup():
    assert issubclass(BurstTooLargeError, InvalidSetupError)


def test_trigger_settings_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        TriggerSettings().sample_count = 2  # type: ignore[misc]
