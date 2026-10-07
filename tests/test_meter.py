import pytest

from agilent34401a.errors import MalformedReplyError, MeterError
from agilent34401a.meter import Function, Resolution, format_reading, parse_reading


def test_reading_parses_a_normal_dc_voltage_reply():
    reading = parse_reading("+1.23456789E+00", Function.DC_VOLTAGE)

    assert reading.value == pytest.approx(1.23456789)
    assert reading.function is Function.DC_VOLTAGE
    assert reading.is_overload is False


def test_reading_keeps_the_raw_reading_exactly_as_the_meter_returned_it():
    reading = parse_reading("  -4.50000000E-03\r\n", Function.DC_VOLTAGE)

    assert reading.value == pytest.approx(-0.0045)
    assert reading.raw == "  -4.50000000E-03\r\n"


@pytest.mark.parametrize("raw", ["+9.90000000E+37", "-9.90000000E+37", "+9.9E+37"])
def test_reading_recognises_overload_in_both_directions(raw):
    reading = parse_reading(raw, Function.DC_VOLTAGE)

    assert reading.is_overload is True
    assert reading.raw == raw


def test_reading_just_inside_the_range_is_not_an_overload():
    assert parse_reading("+9.89999999E+37", Function.DC_VOLTAGE).is_overload is False


@pytest.mark.parametrize("raw", ["", "   ", "OVLD", "1.2.3", "nan", "+inf", "-INF", "1,2", "1_0"])
def test_malformed_reply_is_rejected_with_the_raw_text_in_the_message(raw):
    with pytest.raises(MalformedReplyError) as error_info:
        parse_reading(raw, Function.DC_VOLTAGE)

    assert repr(raw) in str(error_info.value)
    assert isinstance(error_info.value, MeterError)


def test_dc_voltage_reads_in_volts():
    assert Function.DC_VOLTAGE.unit == "V"


@pytest.mark.parametrize(
    ("function", "raw", "expected"),
    [
        (Function.AC_VOLTAGE, "+1.23456789E+00", "1.234568 V"),
        (Function.DC_CURRENT, "+2.50000000E-03", "2.500000 mA"),
        (Function.RESISTANCE_2W, "+4.70000000E+03", "4.700000 kΩ"),
        (Function.RESISTANCE_4W, "+9.99900000E+01", "99.99000 Ω"),
        (Function.FREQUENCY, "+1.00000000E+03", "1.000000 kHz"),
        (Function.PERIOD, "+1.00000000E-03", "1.000000 ms"),
        (Function.DIODE, "+5.80000000E-01", "580.0000 mV"),
        (Function.CONTINUITY, "+1.20000000E+01", "12.00000 Ω"),
    ],
)
def test_every_function_is_shown_with_its_own_unit(function, raw, expected):
    assert format_reading(parse_reading(raw, function)) == expected


@pytest.mark.parametrize(("raw", "expected"), [("+1.23456789E+00", "1.234568"), ("+5.00000000E-01", "0.500000")])
def test_a_ratio_is_shown_without_a_unit_or_prefix(raw, expected):
    assert format_reading(parse_reading(raw, Function.DC_VOLTAGE_RATIO)) == expected


@pytest.mark.parametrize("function", list(Function))
def test_overload_is_shown_as_ovld_for_every_function(function):
    assert format_reading(parse_reading("+9.90000000E+37", function)) == "OVLD"


def _format(value: float, resolution: Resolution = Resolution.SIX_HALF) -> str:
    reading = parse_reading(f"{value:+.8E}", Function.DC_VOLTAGE)
    return format_reading(reading, resolution)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.234567, "1.234567 V"),
        (-1.234567, "-1.234567 V"),
        (0.0, "0.000000 V"),
        (0.0123456, "12.34560 mV"),
        (0.000123456, "123.4560 µV"),
        (2.5e-9, "2.500000 nV"),
        (12345.67, "12.34567 kV"),
        (2.5e6, "2.500000 MV"),
        (100.0, "100.0000 V"),
    ],
)
def test_reading_is_shown_with_an_engineering_prefix_and_unit(value, expected):
    assert _format(value) == expected


def test_reading_that_rounds_up_to_the_next_prefix_is_shown_with_that_prefix():
    assert _format(0.99999999) == "1.000000 V"
    assert _format(999.99999) == "1.000000 kV"


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [(Resolution.SIX_HALF, "1.234568 V"), (Resolution.FIVE_HALF, "1.23457 V"), (Resolution.FOUR_HALF, "1.2346 V")],
)
def test_resolution_sets_the_number_of_digits_shown(resolution, expected):
    assert _format(1.23456789, resolution) == expected


@pytest.mark.parametrize("raw", ["+9.90000000E+37", "-9.90000000E+37"])
def test_overload_is_shown_as_ovld(raw):
    assert format_reading(parse_reading(raw, Function.DC_VOLTAGE)) == "OVLD"


def test_value_beyond_the_prefix_table_keeps_the_largest_prefix():
    assert _format(1.5e12) == "1500.000 GV"


def test_negative_zero_is_shown_without_a_sign():
    assert format_reading(parse_reading("-0.00000000E+00", Function.DC_VOLTAGE)) == "0.000000 V"
