import random
import statistics

import pytest

from agilent34401a.applied_signal import AppliedSignal
from agilent34401a.meter import Function
from agilent34401a.sim import DEMO_SIGNALS, Simulator


def rng(seed: int = 1) -> random.Random:
    return random.Random(seed)  # noqa: S311 - a repeatable source is the point


def test_a_plain_value_is_constant_over_time():
    signal = AppliedSignal(3.3)

    assert [signal.at(seconds, rng()) for seconds in (0.0, 1.0, 1000.0)] == [3.3, 3.3, 3.3]


def test_drift_adds_its_rate_for_every_simulated_second():
    signal = AppliedSignal(1.0, drift=0.01)

    assert signal.at(0.0, rng()) == pytest.approx(1.0)
    assert signal.at(10.0, rng()) == pytest.approx(1.1)


def test_a_negative_drift_falls():
    assert AppliedSignal(1.0, drift=-0.5).at(2.0, rng()) == pytest.approx(0.0)


def test_sine_swings_around_the_value_at_its_frequency():
    signal = AppliedSignal(5.0, sine_amplitude=2.0, sine_frequency_hz=0.5)

    assert signal.at(0.0, rng()) == pytest.approx(5.0)
    assert signal.at(0.5, rng()) == pytest.approx(7.0)  # a quarter of the 2 s period
    assert signal.at(1.0, rng()) == pytest.approx(5.0)
    assert signal.at(1.5, rng()) == pytest.approx(3.0)


def test_noise_is_gaussian_around_the_value_with_the_given_standard_deviation():
    signal = AppliedSignal(10.0, noise=0.5)
    source = rng(42)

    samples = [signal.at(0.0, source) for _ in range(5000)]

    assert statistics.fmean(samples) == pytest.approx(10.0, abs=0.05)
    assert statistics.stdev(samples) == pytest.approx(0.5, rel=0.05)


def test_noise_is_repeatable_from_a_seed():
    signal = AppliedSignal(0.0, noise=1.0)

    assert [signal.at(0.0, rng(7)) for _ in range(2)] == [signal.at(0.0, rng(7)) for _ in range(2)]


def test_a_signal_without_noise_does_not_use_the_random_source():
    class Forbidden(random.Random):
        def gauss(self, *_args: float) -> float:
            raise AssertionError

    assert AppliedSignal(1.0, drift=1.0, sine_amplitude=1.0).at(3.0, Forbidden()) == pytest.approx(4.0)


def test_value_noise_drift_and_sine_add_up():
    signal = AppliedSignal(1.0, drift=0.1, sine_amplitude=0.5, sine_frequency_hz=1.0)

    assert signal.at(0.25, rng()) == pytest.approx(1.0 + 0.025 + 0.5)


@pytest.mark.parametrize(
    "bad",
    [{"noise": -1.0}, {"sine_amplitude": -1.0}, {"sine_frequency_hz": 0.0}, {"sine_frequency_hz": -2.0}],
)
def test_nonsense_signals_are_refused(bad):
    with pytest.raises(ValueError, match="must be"):
        AppliedSignal(1.0, **bad)


def test_the_peak_is_the_largest_magnitude_the_value_and_the_sine_reach_together():
    assert AppliedSignal(-3.0, sine_amplitude=1.0).peak == 4.0


# --- in the Simulator -------------------------------------------------------------------------------------------


def read(simulator: Simulator) -> float:
    return float(simulator.query("READ?"))


def test_a_simulator_with_a_plain_signal_still_reads_the_same_value_every_time():
    simulator = Simulator(signals={Function.DC_VOLTAGE: 2.0})

    assert {read(simulator) for _ in range(5)} == {2.0}


def test_a_simulator_reads_noise_on_the_applied_signal_repeatably_from_its_seed():
    def readings(seed: int) -> list[float]:
        simulator = Simulator(signals={Function.DC_VOLTAGE: AppliedSignal(1.0, noise=0.01)}, seed=seed)
        return [read(simulator) for _ in range(5)]

    assert readings(3) == readings(3)
    assert readings(3) != readings(4)
    assert len(set(readings(3))) > 1


def test_a_simulator_takes_its_random_source_from_the_caller():
    simulator = Simulator(signals={Function.DC_VOLTAGE: AppliedSignal(0.0, noise=0.01)}, random_source=rng(5))
    expected = random.Random(5)  # noqa: S311 - a repeatable source is the point

    assert read(simulator) == pytest.approx(round(expected.gauss(0.0, 0.01), 8), abs=1e-7)


def test_the_simulator_clock_advances_by_the_time_each_reading_takes_even_when_it_runs_instantly():
    simulator = Simulator(time_scale=0)
    assert simulator.signal_time == 0.0

    read(simulator)  # DC voltage at 10 NPLC with Autozero: 0.4 s

    assert simulator.signal_time == pytest.approx(0.4)


def test_drift_shows_in_successive_readings_as_the_clock_advances():
    simulator = Simulator(signals={Function.DC_VOLTAGE: AppliedSignal(1.0, drift=0.1)}, time_scale=0)

    first, second, third = (read(simulator) for _ in range(3))

    assert second - first == pytest.approx(0.04, abs=1e-5)
    assert third - second == pytest.approx(0.04, abs=1e-5)


def test_a_sine_applied_signal_follows_the_simulator_clock():
    simulator = Simulator(
        signals={Function.DC_VOLTAGE: AppliedSignal(0.0, sine_amplitude=1.0, sine_frequency_hz=0.625)}, time_scale=0
    )
    simulator.write("VOLT:DC:NPLC 10")  # 0.4 s a Reading, so a 1.6 s period is exactly four Readings

    values = [read(simulator) for _ in range(5)]

    assert values == pytest.approx([0.0, 1.0, 0.0, -1.0, 0.0], abs=1e-6)


def test_the_signal_can_be_changed_while_the_simulator_runs():
    simulator = Simulator()

    simulator.set_signal(Function.DC_VOLTAGE, AppliedSignal(4.0))

    assert read(simulator) == pytest.approx(4.0)


def test_a_float_is_still_accepted_as_a_signal_when_set_later():
    simulator = Simulator()

    simulator.set_signal(Function.DC_VOLTAGE, 2.5)

    assert read(simulator) == pytest.approx(2.5)


def test_autorange_follows_a_signal_that_swings_into_a_higher_range():
    simulator = Simulator(
        signals={Function.DC_VOLTAGE: AppliedSignal(0.0, sine_amplitude=5.0, sine_frequency_hz=0.625)}, time_scale=0
    )

    values = [read(simulator) for _ in range(4)]

    assert values == pytest.approx([0.0, 5.0, 0.0, -5.0], abs=1e-4)  # the 10 V range holds the peaks: no Overload


def test_a_fixed_range_overloads_when_the_signal_swings_beyond_it():
    simulator = Simulator(
        signals={Function.DC_VOLTAGE: AppliedSignal(0.0, sine_amplitude=5.0, sine_frequency_hz=0.625)}, time_scale=0
    )
    simulator.write("VOLT:DC:RANG 1")

    values = [read(simulator) for _ in range(2)]

    assert values[0] == pytest.approx(0.0)
    assert values[1] > 9e37


def test_the_demo_signals_make_every_listed_function_wander_a_little_around_its_default():
    simulator = Simulator(signals=DEMO_SIGNALS, seed=1, time_scale=0)

    for function in DEMO_SIGNALS:
        simulator.write(f'FUNC "{function.value}"')
        values = [read(simulator) for _ in range(20)]
        assert len(set(values)) > 5
        assert all(value < 9e37 for value in values)  # a signal that wanders must still fit the Autorange it settles in
        assert statistics.fmean(values) == pytest.approx(DEMO_SIGNALS[function].value, rel=0.01)
