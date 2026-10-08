import pytest

from agilent34401a.errors import TransportError, TransportTimeoutError
from agilent34401a.serial_config import FlowControl, Framing, Parity, SerialSettings
from agilent34401a.sim import Simulator
from agilent34401a.sim_serial import SimulatedSerialMeter
from agilent34401a.transport import LocalControl, RemoteControl

SEVEN_E_ONE = Framing(7, Parity.EVEN, 1)


def meter() -> SimulatedSerialMeter:
    return SimulatedSerialMeter(baud=4800, framing=SEVEN_E_ONE, flow_control=FlowControl.DTR_DSR)


def matching() -> SerialSettings:
    return SerialSettings(port="SIM", baud=4800, framing=SEVEN_E_ONE, flow_control=FlowControl.DTR_DSR)


def test_a_port_set_up_as_the_meter_is_gets_its_answers():
    transport = meter().open(matching())

    assert "34401A" in transport.query("*IDN?")


def test_the_simulated_meter_is_the_same_meter_whatever_the_settings():
    simulated = meter()
    simulated.open(matching()).write('FUNC "RES"')

    assert simulated.simulator.query("FUNC?") == '"RES"'


def test_a_port_at_another_baud_rate_hears_nothing():
    transport = meter().open(
        SerialSettings(port="SIM", baud=9600, framing=SEVEN_E_ONE, flow_control=FlowControl.DTR_DSR)
    )

    transport.write("*IDN?")
    with pytest.raises(TransportTimeoutError):
        transport.read()


def test_a_port_at_another_flow_control_hears_nothing():
    transport = meter().open(SerialSettings(port="SIM", baud=4800, framing=SEVEN_E_ONE))

    with pytest.raises(TransportTimeoutError):
        transport.query("*IDN?")


def test_a_port_at_the_right_baud_rate_but_another_framing_receives_garbage():
    transport = meter().open(SerialSettings(port="SIM", baud=4800, flow_control=FlowControl.DTR_DSR))

    reply = transport.query("*IDN?")

    assert "34401A" not in reply
    assert reply


def test_commands_that_were_not_understood_do_not_change_the_meter():
    simulated = meter()
    wrong = simulated.open(SerialSettings(port="SIM", baud=300))

    wrong.write("SYST:RWL")

    assert not simulated.simulator.front_panel_locked


def test_every_port_that_was_opened_is_recorded_in_order():
    simulated = meter()
    first, second = matching(), SerialSettings(port="SIM", baud=300)

    simulated.open(first)
    simulated.open(second)

    assert simulated.opened == [first, second]


def test_a_simulated_serial_port_goes_remote_and_back_to_local_with_commands():
    simulated = meter()
    transport = simulated.open(matching())
    assert isinstance(transport, RemoteControl)
    assert isinstance(transport, LocalControl)

    transport.go_to_remote()
    assert simulated.simulator.remote

    transport.go_to_local()
    assert not simulated.simulator.remote


def test_closing_a_port_leaves_the_meter_for_the_next_port():
    simulated = meter()
    first = simulated.open(matching())
    first.close()
    first.close()

    with pytest.raises(TransportError, match="closed"):
        first.query("*IDN?")
    assert "34401A" in simulated.open(matching()).query("*IDN?")


def test_the_simulated_meter_can_be_given_a_simulator_of_its_own():
    simulator = Simulator(dc_voltage=2.5)

    transport = SimulatedSerialMeter(simulator).open(SerialSettings(port="SIM"))

    assert float(transport.query("READ?")) == pytest.approx(2.5, abs=1e-3)


def test_a_port_can_be_made_to_fail_to_open():
    simulated = SimulatedSerialMeter(missing_ports={"COM9"})

    with pytest.raises(TransportError, match="COM9"):
        simulated.open(SerialSettings(port="COM9"))
