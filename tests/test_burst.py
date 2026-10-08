"""Timing a Burst: how long it takes, how the Worker waits for it, and when each Reading was taken."""

import pytest

from agilent34401a.burst import BLOCKING_LIMIT_S, plan_burst, reading_offsets, trigger_delay_seconds
from agilent34401a.meter import Function, Setup, measurement_time, reading_timeout
from agilent34401a.trigger import TriggerSettings, TriggerSource


def fast_setup(trigger: TriggerSettings) -> Setup:
    return Setup.default(Function.DC_VOLTAGE).with_nplc(1).with_trigger(trigger)  # 0.04 s a Reading with Autozero


def test_an_automatic_trigger_delay_counts_as_nothing_and_a_fixed_one_as_itself():
    assert trigger_delay_seconds(fast_setup(TriggerSettings())) == 0
    assert trigger_delay_seconds(fast_setup(TriggerSettings(delay=0.25))) == 0.25


def test_a_burst_takes_the_delay_and_all_the_samples_of_each_trigger():
    setup = fast_setup(TriggerSettings(delay=0.1, sample_count=10, trigger_count=3))

    assert plan_burst(setup).duration_s == pytest.approx(3 * (0.1 + 10 * 0.04))


def test_the_timeout_is_sized_from_the_duration_and_the_setup_with_a_margin():
    setup = fast_setup(TriggerSettings(sample_count=100))
    plan = plan_burst(setup)

    assert plan.duration_s == pytest.approx(4.0)
    assert plan.timeout_s is not None
    assert plan.timeout_s > plan.duration_s
    assert plan.timeout_s >= plan.duration_s + reading_timeout(setup)


def test_a_slower_setup_gets_a_longer_timeout():
    quick = plan_burst(Setup.default(Function.DC_VOLTAGE).with_nplc(1).with_trigger(TriggerSettings(sample_count=50)))
    slow = plan_burst(Setup.default(Function.DC_VOLTAGE).with_nplc(100).with_trigger(TriggerSettings(sample_count=50)))

    assert slow.timeout_s is not None
    assert quick.timeout_s is not None
    assert slow.timeout_s > 10 * quick.timeout_s


def test_a_short_immediate_burst_is_waited_for_with_the_operation_complete_query():
    plan = plan_burst(fast_setup(TriggerSettings(sample_count=50)))

    assert not plan.polls


def test_a_long_burst_is_polled_so_that_it_can_be_cancelled():
    plan = plan_burst(fast_setup(TriggerSettings(sample_count=500)))

    assert plan.duration_s > BLOCKING_LIMIT_S
    assert plan.polls


def test_an_external_trigger_is_polled_and_never_times_out_because_the_trigger_may_never_come():
    plan = plan_burst(fast_setup(TriggerSettings(source=TriggerSource.EXTERNAL, sample_count=2)))

    assert plan.polls
    assert plan.timeout_s is None


def test_one_bus_trigger_is_waited_for_but_several_are_polled_because_each_needs_sending():
    one = plan_burst(fast_setup(TriggerSettings(source=TriggerSource.BUS, sample_count=5)))
    several = plan_burst(fast_setup(TriggerSettings(source=TriggerSource.BUS, sample_count=5, trigger_count=2)))

    assert not one.polls
    assert several.polls
    assert several.timeout_s is not None


def test_each_reading_is_stamped_after_the_delay_and_the_readings_before_it():
    setup = fast_setup(TriggerSettings(delay=0.5, sample_count=2, trigger_count=2))
    reading = measurement_time(setup)

    offsets = reading_offsets(setup, 4)

    assert offsets == pytest.approx([0.5 + reading, 0.5 + 2 * reading, 1.0 + 3 * reading, 1.0 + 4 * reading])
