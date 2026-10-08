import threading
from typing import TYPE_CHECKING

from agilent34401a.driver import Identity
from agilent34401a.errors import TransportError
from agilent34401a.probe import (
    NOT_FOUND_GUIDANCE,
    ProbeJob,
    ProbeProgress,
    ProbeResult,
    probe_candidates,
    reply_timeout,
    run_probe,
)
from agilent34401a.serial_config import METER_FRAMINGS, FlowControl, Framing, Parity, SerialSettings
from agilent34401a.sim import Simulator
from agilent34401a.sim_serial import SimulatedSerialMeter
from agilent34401a.transport import Transport

SEVEN_O_ONE = Framing(7, Parity.ODD, 1)
if TYPE_CHECKING:
    import queue

BASE = SerialSettings(port="SIM")


def test_probe_tries_every_baud_rate_with_every_meter_framing_the_likeliest_first():
    candidates = probe_candidates(BASE)

    assert len(candidates) == 6 * 3
    assert len(set(candidates)) == 18
    assert [(c.baud, c.framing.label) for c in candidates[:4]] == [
        (9600, "8N1"),
        (9600, "7E1"),
        (9600, "7O1"),
        (4800, "8N1"),
    ]
    assert [c.baud for c in candidates][::3] == [9600, 4800, 2400, 1200, 600, 300]
    assert {c.framing for c in candidates} == set(METER_FRAMINGS)


def test_probe_candidates_keep_the_port_terminator_and_lines_of_the_settings_given():
    base = SerialSettings(port="COM4", dtr=True, rts=False)

    assert all(
        (c.port, c.dtr, c.rts, c.terminator) == ("COM4", True, False, base.terminator) for c in probe_candidates(base)
    )


def test_without_the_flow_control_option_probe_keeps_the_flow_control_it_was_given():
    base = SerialSettings(port="SIM", flow_control=FlowControl.RTS_CTS)

    assert {c.flow_control for c in probe_candidates(base)} == {FlowControl.RTS_CTS}


def test_including_flow_control_widens_the_search_after_the_given_flow_control_is_exhausted():
    candidates = probe_candidates(BASE, include_flow_control=True)

    assert len(candidates) == 18 * 4
    assert {c.flow_control for c in candidates[:18]} == {FlowControl.NONE}
    assert {c.flow_control for c in candidates} == set(FlowControl)
    assert len(set(candidates)) == 72


def test_the_given_flow_control_comes_first_when_the_search_is_widened():
    base = SerialSettings(port="SIM", flow_control=FlowControl.DTR_DSR)

    candidates = probe_candidates(base, include_flow_control=True)

    assert {c.flow_control for c in candidates[:18]} == {FlowControl.DTR_DSR}


def test_the_time_allowed_for_a_reply_grows_as_the_baud_rate_falls():
    assert reply_timeout(300) > reply_timeout(9600)
    assert (
        reply_timeout(300) < 5
    )  # long enough for *IDN?'s reply at 300 baud (about 1.2 s), short enough to be bearable


def test_probe_finds_a_meter_that_answers_only_at_one_baud_rate_and_framing():
    simulated = SimulatedSerialMeter(baud=1200, framing=SEVEN_O_ONE)

    result = run_probe(simulated.open, BASE)

    assert result.found == SerialSettings(port="SIM", baud=1200, framing=SEVEN_O_ONE)
    assert result.identity is not None
    assert result.identity.model == "34401A"
    assert not result.cancelled


def test_probe_finds_the_meter_at_its_factory_settings_on_the_first_try():
    simulated = SimulatedSerialMeter()

    result = run_probe(simulated.open, BASE)

    assert result.found == BASE
    assert result.tried == 1
    assert len(simulated.opened) == 1


def test_probe_does_not_find_a_meter_that_needs_flow_control_unless_asked_to_include_it():
    simulated = SimulatedSerialMeter(baud=2400, flow_control=FlowControl.XON_XOFF)

    assert run_probe(simulated.open, BASE).found is None

    widened = run_probe(simulated.open, BASE, include_flow_control=True)
    assert widened.found == SerialSettings(port="SIM", baud=2400, flow_control=FlowControl.XON_XOFF)


def test_probe_reports_progress_before_each_attempt():
    simulated = SimulatedSerialMeter(baud=4800)
    progress: list[ProbeProgress] = []

    run_probe(simulated.open, BASE, progress=progress.append)

    assert [(p.attempt, p.total) for p in progress] == [(1, 18), (2, 18), (3, 18), (4, 18)]
    assert progress[3].settings == SerialSettings(port="SIM", baud=4800)
    assert progress[0].settings.baud == 9600


def test_when_nothing_answers_probe_has_tried_everything_and_says_what_to_check():
    unreachable = SimulatedSerialMeter(flow_control=FlowControl.RTS_CTS)  # not found without Flow Control in the search

    result = run_probe(unreachable.open, BASE)

    assert result.found is None
    assert not result.cancelled
    assert result.tried == result.total == 18
    assert result.message.startswith("No Meter answered")
    for hint in ("null-modem", "I/O menu", "RS-232", "Flow Control"):
        assert hint in result.message


def test_the_guidance_names_the_things_to_check():
    for hint in ("null-modem", "I/O menu", "Flow Control"):
        assert hint in NOT_FOUND_GUIDANCE


def test_a_probe_that_did_not_include_flow_control_suggests_trying_it():
    unreachable = SimulatedSerialMeter(flow_control=FlowControl.DTR_DSR)

    narrow = run_probe(unreachable.open, BASE)
    wide = run_probe(unreachable.open, BASE, include_flow_control=True)

    assert "Include Flow Control" in narrow.message
    assert wide.found is not None


def test_a_port_that_cannot_be_opened_ends_probe_at_once_with_the_reason():
    simulated = SimulatedSerialMeter(missing_ports={"SIM"})

    result = run_probe(simulated.open, BASE)

    assert result.found is None
    assert result.tried == 1
    assert "no such serial port" in result.message
    assert "null-modem" not in result.message


def test_a_framing_the_port_refuses_is_skipped_rather_than_ending_probe():
    simulated = SimulatedSerialMeter(baud=4800)

    def refuse_seven_bits(settings: SerialSettings) -> Transport:
        if settings.framing.data_bits == 7:
            message = "Could not open: (22, 'Invalid argument')"
            raise TransportError(message)
        return simulated.open(settings)

    result = run_probe(refuse_seven_bits, BASE)

    assert result.found == SerialSettings(port="SIM", baud=4800)


def test_probe_stops_between_attempts_when_cancelled():
    simulated = SimulatedSerialMeter(baud=300)
    cancel = threading.Event()

    def cancel_on_third(progress: ProbeProgress) -> None:
        if progress.attempt == 3:
            cancel.set()

    result = run_probe(simulated.open, BASE, progress=cancel_on_third, cancel=cancel)

    assert result.cancelled
    assert result.found is None
    assert len(simulated.opened) == 2  # the third attempt was never opened
    assert result.message == "Probe cancelled."


def test_probe_cancelled_before_it_starts_opens_nothing():
    simulated = SimulatedSerialMeter()
    cancel = threading.Event()
    cancel.set()

    result = run_probe(simulated.open, BASE, cancel=cancel)

    assert result.cancelled
    assert simulated.opened == []


class Spy:
    """Wraps the ports a simulated line opens, to see that every one is closed and the Meter is left in Local."""

    def __init__(self, simulated: SimulatedSerialMeter) -> None:
        self.simulated = simulated
        self.open_ports: list[Transport] = []
        self.closed = 0

    def open(self, settings: SerialSettings) -> Transport:
        port = self.simulated.open(settings)
        original = port.close

        def close() -> None:
            self.closed += 1
            original()

        port.close = close  # type: ignore[method-assign]
        self.open_ports.append(port)
        return port


def test_probe_closes_every_port_it_opens_and_leaves_the_meter_in_local():
    simulated = SimulatedSerialMeter(baud=600)
    spy = Spy(simulated)

    result = run_probe(spy.open, BASE)

    assert result.found is not None
    assert spy.closed == len(spy.open_ports) == result.tried
    assert not simulated.simulator.remote


def test_a_device_that_is_not_a_34401a_is_not_a_found_meter():
    simulated = SimulatedSerialMeter(Simulator(identity="ACME,Widget,1,2"), baud=9600)

    result = run_probe(simulated.open, BASE)

    assert result.found is None


def test_a_job_probes_on_its_own_thread_and_reports_progress_then_the_result():
    simulated = SimulatedSerialMeter(baud=2400)
    job = ProbeJob(simulated.open, BASE)

    job.start()
    events = drain_until_result(job.events)

    assert isinstance(events[-1], ProbeResult)
    assert events[-1].found == SerialSettings(port="SIM", baud=2400)
    assert all(isinstance(e, ProbeProgress) for e in events[:-1])
    assert len(events) - 1 == 7  # 9600 x3, 4800 x3, 2400 8N1
    job.join(5)
    assert not job.is_alive()


def test_a_job_can_be_cancelled_from_another_thread():
    reached = threading.Event()
    release = threading.Event()
    simulated = SimulatedSerialMeter(baud=300)

    def slow_open(settings: SerialSettings) -> Transport:
        if len(simulated.opened) == 2:
            reached.set()
            assert release.wait(5)
        return simulated.open(settings)

    job = ProbeJob(slow_open, BASE)
    job.start()
    assert reached.wait(5)

    job.cancel()
    release.set()
    events = drain_until_result(job.events)

    result = events[-1]
    assert isinstance(result, ProbeResult)
    assert result.cancelled
    assert result.tried < 18
    job.join(5)
    assert not job.is_alive()


def test_a_job_whose_opener_fails_unexpectedly_still_ends_with_a_result():
    def explode(_settings: SerialSettings) -> Transport:
        message = "boom"
        raise RuntimeError(message)

    job = ProbeJob(explode, BASE)
    job.start()
    events = drain_until_result(job.events)

    result = events[-1]
    assert isinstance(result, ProbeResult)
    assert result.found is None
    assert "boom" in result.message


def drain_until_result(events: "queue.Queue[ProbeProgress | ProbeResult]") -> list[ProbeProgress | ProbeResult]:
    collected: list[ProbeProgress | ProbeResult] = []
    while True:
        event = events.get(timeout=5)
        collected.append(event)
        if isinstance(event, ProbeResult):
            return collected


def test_an_identity_type_is_what_a_found_meter_reports():
    result = run_probe(SimulatedSerialMeter().open, BASE)

    assert isinstance(result.identity, Identity)
