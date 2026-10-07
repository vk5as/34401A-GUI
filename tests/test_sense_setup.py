"""The sense options of a Setup: AC Filter, Gate Time, Autozero and Input Impedance."""

import pytest

from agilent34401a.errors import InvalidSetupError
from agilent34401a.meter import (
    AcFilter,
    Autozero,
    Function,
    GateTime,
    InputImpedance,
    Resolution,
    Setup,
    Terminals,
    describe_setup,
    measurement_time,
    reading_timeout,
)

AC_FUNCTIONS = [Function.AC_VOLTAGE, Function.AC_CURRENT]
COUNTER_FUNCTIONS = [Function.FREQUENCY, Function.PERIOD]
AUTOZERO_FUNCTIONS = [
    Function.DC_VOLTAGE,
    Function.DC_CURRENT,
    Function.RESISTANCE_2W,
    Function.RESISTANCE_4W,
    Function.DC_VOLTAGE_RATIO,
]


def test_the_ac_filters_are_the_three_the_meter_has():
    assert [(ac_filter.hertz, ac_filter.label) for ac_filter in AcFilter] == [
        (3, "3 Hz"),
        (20, "20 Hz"),
        (200, "200 Hz"),
    ]


def test_the_gate_times_are_the_three_the_meter_has():
    assert [(gate_time.seconds, gate_time.label) for gate_time in GateTime] == [
        (0.01, "10 ms"),
        (0.1, "100 ms"),
        (1.0, "1 s"),
    ]


def test_the_two_input_impedances_are_labelled_the_way_the_meter_documents_them():
    assert [impedance.label for impedance in InputImpedance] == ["10 MΩ", ">10 GΩ"]


def test_terminals_are_front_or_rear():
    assert [terminals.label for terminals in Terminals] == ["Front", "Rear"]


def test_a_new_setup_has_the_options_a_reset_meter_has():
    assert Setup.default(Function.AC_VOLTAGE).ac_filter is AcFilter.MEDIUM
    assert Setup.default(Function.FREQUENCY).gate_time is GateTime.HUNDRED_MILLISECONDS
    assert Setup.default(Function.DC_CURRENT).autozero is Autozero.ON
    assert Setup.default(Function.DC_VOLTAGE).input_impedance is InputImpedance.TEN_MEGOHM


@pytest.mark.parametrize("function", list(Function))
def test_each_option_applies_only_to_the_functions_that_have_it(function):
    setup = Setup.default(function)

    assert (setup.ac_filter is not None) == (function in AC_FUNCTIONS)
    assert (setup.gate_time is not None) == (function in COUNTER_FUNCTIONS)
    assert (setup.autozero is not None) == (function in AUTOZERO_FUNCTIONS)
    assert (setup.input_impedance is not None) == (function is Function.DC_VOLTAGE)


@pytest.mark.parametrize("function", list(Function))
def test_functions_say_which_options_they_have(function):
    assert function.has_ac_filter == (function in AC_FUNCTIONS)
    assert function.has_gate_time == (function in COUNTER_FUNCTIONS)
    assert function.has_autozero == (function in AUTOZERO_FUNCTIONS)
    assert function.has_input_impedance == (function is Function.DC_VOLTAGE)


@pytest.mark.parametrize("function", AC_FUNCTIONS)
@pytest.mark.parametrize("ac_filter", list(AcFilter))
def test_an_ac_filter_can_be_chosen_for_ac_functions(function, ac_filter):
    assert Setup.default(function).with_ac_filter(ac_filter).ac_filter is ac_filter


@pytest.mark.parametrize("function", [f for f in Function if f not in AC_FUNCTIONS])
def test_an_ac_filter_is_rejected_for_other_functions(function):
    with pytest.raises(InvalidSetupError, match="AC Filter"):
        Setup.default(function).with_ac_filter(AcFilter.FAST)


@pytest.mark.parametrize("function", COUNTER_FUNCTIONS)
@pytest.mark.parametrize(
    ("gate_time", "resolution"),
    [
        (GateTime.TEN_MILLISECONDS, Resolution.FOUR_HALF),
        (GateTime.HUNDRED_MILLISECONDS, Resolution.FIVE_HALF),
        (GateTime.ONE_SECOND, Resolution.SIX_HALF),
    ],
)
def test_gate_time_decides_the_resolution_of_frequency_and_period(function, gate_time, resolution):
    setup = Setup.default(function).with_gate_time(gate_time)

    assert setup.gate_time is gate_time
    assert setup.resolution is resolution


@pytest.mark.parametrize("function", COUNTER_FUNCTIONS)
@pytest.mark.parametrize(
    ("resolution", "gate_time"),
    [
        (Resolution.FOUR_HALF, GateTime.TEN_MILLISECONDS),
        (Resolution.FIVE_HALF, GateTime.HUNDRED_MILLISECONDS),
        (Resolution.SIX_HALF, GateTime.ONE_SECOND),
    ],
)
def test_choosing_a_resolution_for_frequency_and_period_sets_the_gate_time(function, resolution, gate_time):
    setup = Setup.default(function).with_resolution(resolution)

    assert setup.gate_time is gate_time
    assert setup.resolution is resolution


@pytest.mark.parametrize("function", [f for f in Function if f not in COUNTER_FUNCTIONS])
def test_a_gate_time_is_rejected_for_other_functions(function):
    with pytest.raises(InvalidSetupError, match="Gate Time"):
        Setup.default(function).with_gate_time(GateTime.ONE_SECOND)


@pytest.mark.parametrize("function", AUTOZERO_FUNCTIONS)
@pytest.mark.parametrize("autozero", list(Autozero))
def test_autozero_can_be_chosen_for_the_functions_that_integrate(function, autozero):
    assert Setup.default(function).with_autozero(autozero).autozero is autozero


@pytest.mark.parametrize("function", [f for f in Function if f not in AUTOZERO_FUNCTIONS])
def test_autozero_is_rejected_for_other_functions(function):
    with pytest.raises(InvalidSetupError, match="Autozero"):
        Setup.default(function).with_autozero(Autozero.OFF)


def test_input_impedance_can_be_chosen_for_dc_voltage():
    setup = Setup.default(Function.DC_VOLTAGE).with_input_impedance(InputImpedance.HIGH_IMPEDANCE)

    assert setup.input_impedance is InputImpedance.HIGH_IMPEDANCE


@pytest.mark.parametrize("function", [f for f in Function if f is not Function.DC_VOLTAGE])
def test_input_impedance_is_rejected_for_other_functions(function):
    with pytest.raises(InvalidSetupError, match="Input Impedance"):
        Setup.default(function).with_input_impedance(InputImpedance.HIGH_IMPEDANCE)


def test_a_setup_built_by_hand_needs_the_options_its_function_has():
    with pytest.raises(InvalidSetupError, match="AC Filter"):
        Setup(Function.AC_VOLTAGE, None, Resolution.SIX_HALF, None)


def test_the_options_do_not_change_when_the_range_does():
    setup = Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.SLOW).with_range(1.0)

    assert setup.ac_filter is AcFilter.SLOW


def test_autozero_off_halves_the_time_of_a_reading():
    on = Setup.default(Function.DC_VOLTAGE).with_nplc(100)
    off = on.with_autozero(Autozero.OFF)

    assert measurement_time(on) == pytest.approx(4.0)
    assert measurement_time(off) == pytest.approx(2.0)


def test_autozero_once_reads_as_fast_as_off():
    setup = Setup.default(Function.DC_VOLTAGE).with_nplc(100)

    assert measurement_time(setup.with_autozero(Autozero.ONCE)) == measurement_time(setup.with_autozero(Autozero.OFF))


@pytest.mark.parametrize("function", AC_FUNCTIONS)
@pytest.mark.parametrize(("ac_filter", "seconds"), [(AcFilter.SLOW, 7.0), (AcFilter.MEDIUM, 1.0), (AcFilter.FAST, 0.1)])
def test_the_ac_filter_decides_how_long_an_ac_reading_takes_to_settle(function, ac_filter, seconds):
    assert measurement_time(Setup.default(function).with_ac_filter(ac_filter)) == pytest.approx(seconds)


@pytest.mark.parametrize("function", COUNTER_FUNCTIONS)
@pytest.mark.parametrize("gate_time", list(GateTime))
def test_the_gate_time_is_how_long_a_frequency_or_period_reading_takes(function, gate_time):
    assert measurement_time(Setup.default(function).with_gate_time(gate_time)) == pytest.approx(gate_time.seconds)


def test_the_slow_ac_filter_never_times_out():
    setup = Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.SLOW)

    assert reading_timeout(setup) > measurement_time(setup)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (Setup.default(Function.AC_VOLTAGE), "AC V · Autorange · 6½ digits · 20 Hz filter"),
        (
            Setup.default(Function.AC_CURRENT).with_ac_filter(AcFilter.FAST),
            "AC I · Autorange · 6½ digits · 200 Hz filter",
        ),
        (Setup.default(Function.FREQUENCY), "Frequency · Autorange · 5½ digits · 100 ms gate"),
        (
            Setup.default(Function.PERIOD).with_gate_time(GateTime.ONE_SECOND),
            "Period · Autorange · 6½ digits · 1 s gate",
        ),
        (
            Setup.default(Function.DC_VOLTAGE).with_autozero(Autozero.OFF),
            "DC V · Autorange · 6½ digits · 10 NPLC · Autozero off",
        ),
        (
            Setup.default(Function.DC_VOLTAGE).with_input_impedance(InputImpedance.HIGH_IMPEDANCE),
            "DC V · Autorange · 6½ digits · 10 NPLC · >10 GΩ input",
        ),
    ],
)
def test_the_description_names_the_options_that_differ_from_a_reset_meter(setup, expected):
    assert describe_setup(setup) == expected
