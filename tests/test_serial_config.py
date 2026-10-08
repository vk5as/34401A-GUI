import pytest

from agilent34401a.serial_config import (
    BAUD_RATES,
    METER_FRAMINGS,
    FlowControl,
    Framing,
    Parity,
    SerialSettings,
    Terminator,
)


def test_the_default_serial_settings_are_the_meters_factory_settings():
    settings = SerialSettings(port="/dev/ttyUSB0")

    assert settings.baud == 9600
    assert settings.framing.label == "8N1"
    assert settings.flow_control is FlowControl.NONE
    assert settings.terminator is Terminator.LF
    assert settings.dtr is None
    assert settings.rts is None


def test_the_meter_supports_six_baud_rates_from_300_to_9600():
    assert BAUD_RATES == (300, 600, 1200, 2400, 4800, 9600)


@pytest.mark.parametrize("baud", [0, 150, 19200, -9600])
def test_a_baud_rate_the_meter_cannot_use_is_rejected(baud):
    with pytest.raises(ValueError, match="baud"):
        SerialSettings(port="COM1", baud=baud)


@pytest.mark.parametrize("port", ["", "   "])
def test_a_blank_serial_port_is_rejected(port):
    with pytest.raises(ValueError, match="port"):
        SerialSettings(port=port)


@pytest.mark.parametrize("data_bits", [5, 6, 9])
def test_a_framing_takes_seven_or_eight_data_bits(data_bits):
    with pytest.raises(ValueError, match="data bits"):
        Framing(data_bits=data_bits)


@pytest.mark.parametrize("stop_bits", [0, 3])
def test_a_framing_takes_one_or_two_stop_bits(stop_bits):
    with pytest.raises(ValueError, match="stop bits"):
        Framing(stop_bits=stop_bits)


@pytest.mark.parametrize(
    ("framing", "label"),
    [
        (Framing(8, Parity.NONE, 1), "8N1"),
        (Framing(7, Parity.EVEN, 1), "7E1"),
        (Framing(7, Parity.ODD, 1), "7O1"),
        (Framing(8, Parity.EVEN, 2), "8E2"),
    ],
)
def test_a_framing_is_named_by_its_data_bits_parity_and_stop_bits(framing, label):
    assert framing.label == label


def test_a_framing_can_be_read_back_from_its_name():
    assert Framing.from_label("7e1") == Framing(7, Parity.EVEN, 1)
    with pytest.raises(ValueError, match="Framing"):
        Framing.from_label("9X1")


def test_the_meters_own_framings_are_8n1_7e1_and_7o1_with_8n1_first():
    assert [framing.label for framing in METER_FRAMINGS] == ["8N1", "7E1", "7O1"]


@pytest.mark.parametrize(
    ("port", "resource"),
    [
        ("/dev/ttyUSB0", "ASRL/dev/ttyUSB0::INSTR"),
        ("COM3", "ASRL3::INSTR"),
        ("com12", "ASRL12::INSTR"),
        ("ASRL4::INSTR", "ASRL4::INSTR"),
        ("ASRL/dev/ttyS0::INSTR", "ASRL/dev/ttyS0::INSTR"),
        ("ASRL5", "ASRL5::INSTR"),
    ],
)
def test_a_serial_port_names_the_visa_resource_to_open(port, resource):
    assert SerialSettings(port=port).resource_name == resource


def test_serial_settings_describe_themselves_for_the_status_bar():
    settings = SerialSettings(
        port="COM3", baud=4800, framing=Framing(7, Parity.EVEN, 1), flow_control=FlowControl.DTR_DSR
    )

    assert settings.describe() == "COM3, 4800 baud, 7E1, DTR/DSR"


def test_every_flow_control_has_a_label_and_can_be_found_by_its_key():
    assert [flow.label for flow in FlowControl] == ["None", "XON/XOFF", "RTS/CTS", "DTR/DSR"]
    assert FlowControl("rtscts") is FlowControl.RTS_CTS


def test_terminators_are_the_line_endings_sent_after_each_command():
    assert [terminator.text for terminator in Terminator] == ["\n", "\r", "\r\n"]
    assert [terminator.label for terminator in Terminator] == ["LF", "CR", "CR+LF"]
