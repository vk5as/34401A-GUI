"""Resynchronising a Connection after a reply went missing, came late or came back as noise."""

import pytest

from agilent34401a.errors import MalformedReplyError, ResyncFailedError, TransportError, TransportTimeoutError
from agilent34401a.resync import Sentinel, resynchronise
from agilent34401a.sim import Simulator

IDENTITY = "HEWLETT-PACKARD,34401A,0,10-5-2"
SENTINEL = Sentinel("*IDN?", IDENTITY)
TIMED_OUT = TransportTimeoutError("no reply")


class WireTransport:
    """A Transport whose incoming replies are scripted, in the order a read would meet them."""

    def __init__(self, *incoming: str | Exception) -> None:
        self.timeout = 3.0
        self.incoming = list(incoming)
        self.written: list[str] = []
        self.timeouts_at_reads: list[float] = []
        self.cleared = 0
        self.write_error: Exception | None = None

    def write(self, command: str) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.written.append(command)

    def read(self) -> str:
        self.timeouts_at_reads.append(self.timeout)
        if not self.incoming:
            raise TIMED_OUT
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self.cleared += 1

    def close(self) -> None:
        pass


def test_a_connection_that_is_in_step_answers_the_sentinel_and_is_left_alone():
    transport = WireTransport(IDENTITY)

    resynchronise(transport, SENTINEL)

    assert transport.written == ["*IDN?"]
    assert transport.cleared == 1


def test_stale_replies_in_front_of_the_sentinels_are_thrown_away():
    transport = WireTransport("+1.00000000E+00", '"VOLT"', IDENTITY)

    resynchronise(transport, SENTINEL)

    assert transport.incoming == []


def test_noise_in_front_of_the_sentinels_reply_is_thrown_away():
    transport = WireTransport(MalformedReplyError("not text"), "+1.0E+00", IDENTITY)

    resynchronise(transport, SENTINEL)

    assert transport.incoming == []


def test_a_late_reply_is_waited_for_through_a_timeout_or_two():
    transport = WireTransport(TIMED_OUT, "+1.00000000E+00", TIMED_OUT, IDENTITY)

    resynchronise(transport, SENTINEL, patience=3)

    assert transport.incoming == []


def test_a_meter_that_never_answers_the_sentinel_is_a_failed_resync():
    transport = WireTransport()

    with pytest.raises(ResyncFailedError) as error_info:
        resynchronise(transport, SENTINEL, patience=2)

    assert isinstance(error_info.value, TransportError)
    assert transport.written == ["*IDN?"]
    assert len(transport.timeouts_at_reads) == 2


def test_a_meter_that_only_ever_says_the_wrong_thing_is_a_failed_resync():
    transport = WireTransport(*["+1.0E+00"] * 500)

    with pytest.raises(ResyncFailedError):
        resynchronise(transport, SENTINEL)


def test_a_stale_copy_of_the_sentinels_own_reply_does_not_leave_the_real_one_behind():
    # The query that went missing was itself *IDN?, so its late reply looks exactly like the answer to the sentinel.
    transport = WireTransport(IDENTITY, IDENTITY)

    resynchronise(transport, SENTINEL)

    assert transport.incoming == []


def test_the_transports_timeout_is_put_back_afterwards_and_is_short_while_settling():
    transport = WireTransport(IDENTITY)

    resynchronise(transport, SENTINEL, settle_s=0.05)

    assert transport.timeout == 3.0
    assert transport.timeouts_at_reads == [3.0, 0.05]


def test_the_transports_timeout_is_put_back_after_a_failure_too():
    transport = WireTransport()

    with pytest.raises(ResyncFailedError):
        resynchronise(transport, SENTINEL, patience=1)

    assert transport.timeout == 3.0


def test_a_connection_that_cannot_be_written_to_is_a_transport_error():
    transport = WireTransport()
    transport.write_error = TransportError("broken pipe")

    with pytest.raises(TransportError, match="broken pipe"):
        resynchronise(transport, SENTINEL)

    assert transport.timeout == 3.0


class UnclearableSimulator(Simulator):
    """A Simulator on a Connection that has no device clear, so what the Meter still holds is not dropped by it."""

    def clear(self) -> None:
        pass


def test_resynchronising_a_connection_that_cannot_clear_still_discards_the_stale_reply():
    simulator = UnclearableSimulator()
    simulator.write("READ?")  # a Reading nobody collected
    identity = Simulator().query("*IDN?")

    resynchronise(simulator, Sentinel("*IDN?", identity))

    assert simulator.query("FUNC?") == '"VOLT"'
