"""The calibration guard (ADR-0006) is a safety feature, so it is tested against every trick we could think of."""

import pytest

from agilent34401a.raw_scpi import analyse


@pytest.mark.parametrize("command", ["*IDN?", "FUNC?", "VOLT:DC:NPLC?", "SYST:ERR?", "MEAS:VOLT:DC? 10,0.001", "READ?"])
def test_a_plain_query_expects_a_reply_and_changes_nothing(command):
    result = analyse(command)

    assert result.has_reply
    assert not result.changes_meter
    assert not result.writes_calibration


@pytest.mark.parametrize("command", ["*RST", 'FUNC "VOLT:AC"', "VOLT:DC:NPLC 10", "INIT", 'DISP:TEXT "HI"'])
def test_a_plain_command_expects_no_reply_and_may_change_the_meter(command):
    result = analyse(command)

    assert not result.has_reply
    assert result.changes_meter
    assert not result.writes_calibration


def test_a_compound_with_both_a_command_and_a_query_has_a_reply_and_changes_the_meter():
    result = analyse('FUNC "VOLT:AC";FUNC?')

    assert result.has_reply
    assert result.changes_meter


@pytest.mark.parametrize("command", ["", "   ", "\t", ";", " ; ;\n"])
def test_a_command_with_nothing_in_it_is_empty(command):
    result = analyse(command)

    assert result.is_empty
    assert not result.has_reply
    assert not result.changes_meter
    assert not result.writes_calibration


@pytest.mark.parametrize(
    "command",
    [
        "CAL:SEC:STAT OFF,HP034401",
        "CAL:SEC:STAT ON,HP034401",
        "CAL:SEC:CODE NEWCODE",
        "CAL:VAL 1.0",
        "CAL:STR 'Calibrated 2026'",
        'CAL:STR "Calibrated 2026"',
        "CAL?",
        "CAL",
    ],
)
def test_calibration_commands_are_calibration_writes(command):
    assert analyse(command).writes_calibration


@pytest.mark.parametrize("command", ["CAL:COUN?", "CAL:STR?", "CAL:SEC:STAT?", "CAL:VAL?"])
def test_read_only_calibration_queries_are_not_calibration_writes(command):
    result = analyse(command)

    assert result.has_reply
    assert not result.changes_meter
    assert not result.writes_calibration


@pytest.mark.parametrize("command", ["CALC:FUNC NULL", "CALC:STAT ON", "CALC:AVER:AVER?", "CALCULATE:STATE ON"])
def test_the_calculate_subsystem_is_not_calibration(command):
    assert not analyse(command).writes_calibration


@pytest.mark.parametrize(
    "command",
    [
        "cal:sec:stat off,hp034401",
        "Cal:Val 1.0",
        "CALibration:SECure:STATe OFF,HP034401",
        "calibration:string 'x'",
        "CALIBRATION:VALUE 1",
        "CALI:VAL 1",
        "CALIB:VAL 1",
        "CAL1:VAL 1",
    ],
)
def test_calibration_writes_are_recognised_in_any_case_and_in_long_or_abbreviated_form(command):
    assert analyse(command).writes_calibration


@pytest.mark.parametrize("command", ["calibration:count?", "CALibration:COUNt?", "cal:coun?", "CALibration:STRing?"])
def test_read_only_calibration_queries_are_allowed_in_any_case_and_form(command):
    assert not analyse(command).writes_calibration


@pytest.mark.parametrize("command", [":CAL:VAL 1", "::CAL:VAL 1", ": CAL:VAL 1", ":\tCAL:VAL 1"])
def test_a_leading_colon_does_not_hide_a_calibration_write(command):
    assert analyse(command).writes_calibration


@pytest.mark.parametrize(
    "command",
    [
        "  CAL:VAL 1",
        "\tCAL:VAL 1",
        "\x00CAL:VAL 1",
        "\x0bCAL:VAL 1",
        "\x1fCAL:SEC:STAT OFF,1",
        "CAL:VAL\t1",
        "CAL:VAL\x001",
        "CAL\t:VAL 1",
        "CAL :VAL 1",
        "CAL: VAL 1",
        "CAL::VAL 1",
        "CAL:",
        "CAL:COUN?\t1",
        "CAL:COUN? 1",
    ],
)
def test_white_space_and_control_characters_do_not_hide_a_calibration_write(command):
    assert analyse(command).writes_calibration


@pytest.mark.parametrize("command", ["CAL:COUN?  ", "\tCAL:COUN?", "CAL:COUN?\t", "  cal:str?  "])
def test_white_space_around_a_read_only_calibration_query_is_harmless(command):
    assert not analyse(command).writes_calibration


@pytest.mark.parametrize(
    "command",
    [
        "*IDN?;CAL:VAL 1",
        "CAL:VAL 1;*IDN?",
        "*RST;CAL:SEC:STAT OFF,1",
        "FUNC?;:CAL:SEC:STAT OFF,1",
        "FUNC?;;;CAL:SEC:STAT OFF,1",
        "FUNC?; CAL:SEC:STAT OFF,1",
        "FUNC?;\tCAL:SEC:STAT OFF,1",
        "FUNC?\nCAL:SEC:STAT OFF,1",
        "FUNC?\rCAL:SEC:STAT OFF,1",
        "FUNC?\r\nCAL:SEC:STAT OFF,1",
        "CAL:COUN?;CAL:VAL 1",
        "CAL:COUN?;:CAL:STR 'x'",
        "CAL?;CAL:COUN?",
    ],
)
def test_a_calibration_write_in_a_compound_command_is_found_wherever_it_is(command):
    assert analyse(command).writes_calibration


@pytest.mark.parametrize(
    "command",
    [
        "CAL:COUN?;SEC:STAT OFF,1",  # relative: this is CAL:SEC:STAT
        "CAL:SEC:STAT?;CODE NEWCODE",  # relative: this is CAL:SEC:CODE
        "CAL:COUN?;VAL 1",
        "CAL:COUN?;STR 'x'",
        "CAL:COUN?;\tSTR 'x'",
        "CAL:SEC:STAT?;STAT OFF,1",
        "CAL:COUN?;SYST:ERR?",  # relative too, and not a read-only calibration query
    ],
)
def test_a_command_relative_to_a_calibration_command_is_a_calibration_command(command):
    assert analyse(command).writes_calibration


@pytest.mark.parametrize(
    "command",
    [
        "CAL:COUN?;:SYST:ERR?",
        "CAL:COUN?;STR?",  # relative: this is CAL:STR?
        "FUNC?;:CAL:COUN?",
        "*IDN?;CAL:STR?",
        "CAL:COUN?\nCAL:STR?",
        "SYST:ERR?;ERR?",
    ],
)
def test_compound_commands_made_only_of_harmless_parts_are_harmless(command):
    assert not analyse(command).writes_calibration


@pytest.mark.parametrize("command", ["*CAL?", "*cal?", "*CAL", "*IDN?;*CAL?"])
def test_the_common_calibration_command_is_a_calibration_write(command):
    assert analyse(command).writes_calibration


@pytest.mark.parametrize("command", ["CAL?", "cal?", "CALibration?", ":CAL?"])
def test_the_query_that_performs_a_calibration_is_a_calibration_write(command):
    assert analyse(command).writes_calibration


def test_a_calibration_write_after_a_semicolon_inside_quotes_is_still_refused():
    assert analyse('DISP:TEXT "a;:CAL:VAL 1"').writes_calibration


def test_a_quote_cannot_desynchronise_the_check_from_the_meter():
    assert analyse('CAL:STR? "a;b";SEC:STAT OFF,1').writes_calibration


@pytest.mark.parametrize("command", ["CAL:COUN? x", 'CAL:STR? "x"', "CAL:SEC:STAT? 1"])
def test_a_read_only_calibration_query_with_arguments_is_refused(command):
    assert analyse(command).writes_calibration


@pytest.mark.parametrize("command", ["CAL:VAL 1\u00a0", "\u00a0CAL:VAL 1"])
def test_non_ascii_white_space_does_not_hide_a_calibration_write(command):
    assert analyse(command).writes_calibration
