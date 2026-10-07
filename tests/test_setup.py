import pytest

from agilent34401a.errors import InvalidSetupError
from agilent34401a.meter import (
    NPLC_VALUES,
    Function,
    Resolution,
    Setup,
    describe_setup,
    format_range,
    measurement_time,
    reading_timeout,
)

ALL_FUNCTIONS = list(Function)
NPLC_FUNCTIONS = [
    Function.DC_VOLTAGE,
    Function.DC_CURRENT,
    Function.RESISTANCE_2W,
    Function.RESISTANCE_4W,
    Function.DC_VOLTAGE_RATIO,
]


def test_there_are_eleven_functions():
    assert len(ALL_FUNCTIONS) == 11


@pytest.mark.parametrize(
    ("function", "unit", "ranges"),
    [
        (Function.DC_VOLTAGE, "V", (0.1, 1.0, 10.0, 100.0, 1000.0)),
        (Function.AC_VOLTAGE, "V", (0.1, 1.0, 10.0, 100.0, 750.0)),
        (Function.DC_CURRENT, "A", (0.01, 0.1, 1.0, 3.0)),
        (Function.AC_CURRENT, "A", (1.0, 3.0)),
        (Function.RESISTANCE_2W, "Ω", (100.0, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8)),
        (Function.RESISTANCE_4W, "Ω", (100.0, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8)),
        (Function.FREQUENCY, "Hz", (0.1, 1.0, 10.0, 100.0, 750.0)),
        (Function.PERIOD, "s", (0.1, 1.0, 10.0, 100.0, 750.0)),
        (Function.CONTINUITY, "Ω", ()),
        (Function.DIODE, "V", ()),
        (Function.DC_VOLTAGE_RATIO, "", (0.1, 1.0, 10.0, 100.0, 1000.0)),
    ],
)
def test_each_function_has_its_unit_and_valid_ranges(function, unit, ranges):
    assert function.unit == unit
    assert function.ranges == ranges


def test_every_function_has_a_distinct_scpi_name_and_label():
    assert len({function.value for function in Function}) == 11
    assert len({function.label for function in Function}) == 11


def test_default_setup_is_autorange_at_the_meters_reset_integration_time():
    setup = Setup.default(Function.DC_VOLTAGE)

    assert setup.range is None
    assert setup.resolution is Resolution.SIX_HALF
    assert setup.nplc == 10


@pytest.mark.parametrize("function", ALL_FUNCTIONS)
def test_every_function_has_a_valid_default_setup(function):
    setup = Setup.default(function)

    assert setup.function is function
    assert (setup.nplc is not None) == (function in NPLC_FUNCTIONS)


@pytest.mark.parametrize("function", ALL_FUNCTIONS)
def test_every_valid_range_makes_a_valid_setup(function):
    for range_value in function.ranges:
        assert Setup.default(function).with_range(range_value).range == range_value


@pytest.mark.parametrize("function", ALL_FUNCTIONS)
def test_a_range_the_function_does_not_have_is_rejected(function):
    with pytest.raises(InvalidSetupError, match="Range"):
        Setup.default(function).with_range(12345.0)


def test_functions_without_ranges_cannot_be_given_one():
    with pytest.raises(InvalidSetupError, match="Range"):
        Setup.default(Function.DIODE).with_range(1.0)


def test_range_can_be_set_back_to_autorange():
    setup = Setup.default(Function.DC_VOLTAGE).with_range(10.0).with_range(None)

    assert setup.range is None


@pytest.mark.parametrize(
    ("resolution", "nplc"),
    [(Resolution.FOUR_HALF, 0.02), (Resolution.FIVE_HALF, 1), (Resolution.SIX_HALF, 10)],
)
def test_choosing_a_resolution_sets_its_usual_integration_time(resolution, nplc):
    setup = Setup.default(Function.DC_VOLTAGE).with_resolution(resolution)

    assert setup.resolution is resolution
    assert setup.nplc == nplc


@pytest.mark.parametrize(
    ("nplc", "resolution"),
    [
        (0.02, Resolution.FOUR_HALF),
        (0.2, Resolution.FIVE_HALF),
        (1, Resolution.FIVE_HALF),
        (10, Resolution.SIX_HALF),
        (100, Resolution.SIX_HALF),
    ],
)
def test_integration_time_decides_the_resolution(nplc, resolution):
    setup = Setup.default(Function.RESISTANCE_2W).with_nplc(nplc)

    assert setup.nplc == nplc
    assert setup.resolution is resolution


def test_integration_time_values_are_the_five_the_meter_accepts():
    assert NPLC_VALUES == (0.02, 0.2, 1, 10, 100)


@pytest.mark.parametrize("nplc", [0, 0.01, 5, 101, -1])
def test_an_integration_time_the_meter_does_not_accept_is_rejected(nplc):
    with pytest.raises(InvalidSetupError, match="Integration Time"):
        Setup.default(Function.DC_VOLTAGE).with_nplc(nplc)


@pytest.mark.parametrize("function", [f for f in ALL_FUNCTIONS if f not in NPLC_FUNCTIONS])
def test_integration_time_does_not_apply_to_other_functions(function):
    with pytest.raises(InvalidSetupError, match="Integration Time"):
        Setup.default(function).with_nplc(10)


@pytest.mark.parametrize(
    ("function", "fixed"),
    [
        (Function.AC_VOLTAGE, Resolution.SIX_HALF),
        (Function.AC_CURRENT, Resolution.SIX_HALF),
        (Function.CONTINUITY, Resolution.FIVE_HALF),
        (Function.DIODE, Resolution.FIVE_HALF),
        (Function.FREQUENCY, Resolution.FIVE_HALF),
        (Function.PERIOD, Resolution.FIVE_HALF),
    ],
)
def test_resolution_is_fixed_for_functions_that_cannot_change_it(function, fixed):
    setup = Setup.default(function)

    assert setup.resolution is fixed
    other = next(resolution for resolution in Resolution if resolution is not fixed)
    with pytest.raises(InvalidSetupError, match="Resolution"):
        setup.with_resolution(other)


def test_resolution_matching_the_fixed_one_is_accepted():
    setup = Setup.default(Function.AC_VOLTAGE).with_resolution(Resolution.SIX_HALF)

    assert setup.resolution is Resolution.SIX_HALF


def test_a_setup_built_by_hand_is_validated():
    with pytest.raises(InvalidSetupError):
        Setup(Function.DC_VOLTAGE, range=5.0, resolution=Resolution.SIX_HALF, nplc=10)
    with pytest.raises(InvalidSetupError):
        Setup(Function.DC_VOLTAGE, range=None, resolution=Resolution.FOUR_HALF, nplc=10)


@pytest.mark.parametrize(
    ("function", "value", "expected"),
    [
        (Function.DC_VOLTAGE, 0.1, "100 mV"),
        (Function.DC_VOLTAGE, 1000.0, "1 kV"),
        (Function.AC_VOLTAGE, 750.0, "750 V"),
        (Function.DC_CURRENT, 0.01, "10 mA"),
        (Function.AC_CURRENT, 3.0, "3 A"),
        (Function.RESISTANCE_2W, 100.0, "100 Ω"),
        (Function.RESISTANCE_4W, 1e8, "100 MΩ"),
        (Function.FREQUENCY, 0.1, "100 mV"),
        (Function.DC_VOLTAGE_RATIO, 10.0, "10 V"),
    ],
)
def test_ranges_are_labelled_with_engineering_prefixes(function, value, expected):
    assert format_range(function, value) == expected


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (Setup.default(Function.DC_VOLTAGE), "DC V · Autorange · 6½ digits · 10 NPLC"),
        (
            Setup.default(Function.RESISTANCE_4W).with_range(1e4).with_nplc(0.2),
            "4-wire Ω · 10 kΩ range · 5½ digits · 0.2 NPLC",
        ),
        (Setup.default(Function.AC_VOLTAGE).with_range(0.1), "AC V · 100 mV range · 6½ digits"),
        (Setup.default(Function.FREQUENCY), "Frequency · Autorange · 5½ digits"),
        (Setup.default(Function.DIODE), "Diode · 5½ digits"),
    ],
)
def test_setup_is_described_in_one_line(setup, expected):
    assert describe_setup(setup) == expected


def test_ratio_readings_have_no_unit_in_the_description():
    assert describe_setup(Setup.default(Function.DC_VOLTAGE_RATIO)).startswith("DC V ratio · Autorange")


def test_measurement_time_follows_integration_time_and_autozero():
    # 50 Hz line: one power-line cycle is 20 ms, and Autozero measures twice.
    assert measurement_time(Setup.default(Function.DC_VOLTAGE).with_nplc(100)) == pytest.approx(4.0)
    assert measurement_time(Setup.default(Function.DC_VOLTAGE).with_nplc(10)) == pytest.approx(0.4)
    assert measurement_time(Setup.default(Function.DC_VOLTAGE).with_nplc(0.02)) == pytest.approx(0.0008)


def test_measurement_time_of_a_ratio_covers_both_inputs():
    ratio = measurement_time(Setup.default(Function.DC_VOLTAGE_RATIO))

    assert ratio == pytest.approx(2 * measurement_time(Setup.default(Function.DC_VOLTAGE)))


@pytest.mark.parametrize(
    ("function", "expected"),
    [(Function.AC_VOLTAGE, 1.0), (Function.AC_CURRENT, 1.0), (Function.FREQUENCY, 0.1), (Function.PERIOD, 0.1)],
)
def test_measurement_time_of_ac_and_counter_functions(function, expected):
    assert measurement_time(Setup.default(function)) == pytest.approx(expected)


@pytest.mark.parametrize("function", ALL_FUNCTIONS)
def test_every_function_takes_some_time_to_measure(function):
    assert measurement_time(Setup.default(function)) > 0


@pytest.mark.parametrize("function", NPLC_FUNCTIONS)
def test_the_slowest_setup_never_times_out(function):
    setup = Setup.default(function).with_nplc(100)

    assert reading_timeout(setup) > measurement_time(setup)


def test_timeout_is_at_least_two_seconds_even_for_the_fastest_setup():
    assert reading_timeout(Setup.default(Function.DC_VOLTAGE).with_nplc(0.02)) == 2.0


def test_timeout_grows_with_integration_time():
    fast = reading_timeout(Setup.default(Function.DC_VOLTAGE).with_nplc(1))
    slow = reading_timeout(Setup.default(Function.DC_VOLTAGE).with_nplc(100))

    assert slow > fast


def test_autorange_allows_for_several_range_changes_per_reading():
    auto = Setup.default(Function.DC_VOLTAGE).with_nplc(100)
    fixed = auto.with_range(10.0)

    assert reading_timeout(auto) > 2 * reading_timeout(fixed)
