"""Fault injection in the socket Simulator: slow replies, dropped connections and garbage bytes (ADR-0003)."""

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from agilent34401a.sim import HEWLETT_PACKARD_IDENTITY
from agilent34401a.sim_faults import Effect, Fault, parse_fault
from agilent34401a.sim_server import SimulatorServer, main

TIMEOUT_S = 5.0
IDENTITY_LINE = f"{HEWLETT_PACKARD_IDENTITY}\n"


@pytest.fixture
def server() -> Iterator[SimulatorServer]:
    with SimulatorServer(port=0) as running:
        yield running


def connect(server: SimulatorServer) -> socket.socket:
    return socket.create_connection(("127.0.0.1", server.port), timeout=TIMEOUT_S)


def read_line(client: socket.socket) -> str:
    data = b""
    while not data.endswith(b"\n"):
        chunk = client.recv(1)
        if not chunk:
            break
        data += chunk
    return data.decode()


def ask(client: socket.socket, command: str) -> str:
    client.sendall(f"{command}\n".encode())
    return read_line(client)


def read_bytes(client: socket.socket, quiet_s: float = 0.2) -> bytes:
    """Everything the server sends until it has been silent for `quiet_s`, or hangs up."""
    client.settimeout(quiet_s)
    data = b""
    try:
        while chunk := client.recv(4096):
            data += chunk
    except TimeoutError:
        pass
    finally:
        client.settimeout(TIMEOUT_S)
    return data


def hung_up(client: socket.socket) -> bool:
    try:
        return client.recv(1) == b""
    except ConnectionResetError:
        return True


def test_a_slow_reply_arrives_late_but_arrives(server):
    server.inject(Fault.slow(1.0))
    with connect(server) as client:
        client.sendall(b"*IDN?\n")
        client.settimeout(0.05)
        with pytest.raises(TimeoutError):
            client.recv(1)  # nothing yet: the Meter is taking its time

        client.settimeout(TIMEOUT_S)
        assert read_line(client) == IDENTITY_LINE


def test_a_fault_applies_once_by_default_and_the_next_reply_is_prompt(server):
    server.inject(Fault.slow(2.0))
    with connect(server) as client:
        ask(client, "*IDN?")

        started = time.monotonic()
        assert ask(client, "FUNC?") == '"VOLT"\n'
        assert time.monotonic() - started < 1.0  # well under the 2 s the first reply was held for


def test_a_slow_fault_can_apply_to_every_reply(server):
    server.inject(Fault.slow(0.1, times=None))
    with connect(server) as client:
        for _ in range(3):
            started = time.monotonic()
            ask(client, "FUNC?")
            assert time.monotonic() - started >= 0.09


def test_a_slow_fault_can_name_the_command_it_hits(server):
    server.inject(Fault.slow(2.0, command="READ"))
    with connect(server) as client:
        started = time.monotonic()
        assert ask(client, "*IDN?") == IDENTITY_LINE
        assert time.monotonic() - started < 1.0  # well under the 2 s a READ? is held for

        client.sendall(b"READ?\n")
        client.settimeout(0.05)
        with pytest.raises(TimeoutError):
            client.recv(1)


def test_the_connection_drops_after_the_given_number_of_replies(server):
    server.inject(Fault.drop(after=2))
    with connect(server) as client:
        assert ask(client, "*IDN?") == IDENTITY_LINE
        assert ask(client, "FUNC?") == '"VOLT"\n'

        client.sendall(b"FUNC?\n")

        assert hung_up(client)


def test_the_connection_drops_on_a_command_pattern(server):
    server.inject(Fault.drop(command=r"^READ\?"))
    with connect(server) as client:
        assert ask(client, "*IDN?") == IDENTITY_LINE

        client.sendall(b"READ?\n")

        assert hung_up(client)


def test_a_drop_that_names_a_command_also_catches_a_command_that_has_no_reply(server):
    server.inject(Fault.drop(command="FUNC"))
    with connect(server) as client:
        client.sendall(b'FUNC "RES"\n')

        assert hung_up(client)


def test_a_dropped_connection_can_be_reopened_because_the_fault_fired_only_once(server):
    server.inject(Fault.drop())
    with connect(server) as client:
        client.sendall(b"*IDN?\n")
        assert hung_up(client)

    with connect(server) as client:
        assert ask(client, "*IDN?") == IDENTITY_LINE


def test_a_reset_slams_the_connection_instead_of_closing_it_politely(server):
    server.inject(Fault.reset())
    with connect(server) as client:
        client.sendall(b"*IDN?\n")

        with pytest.raises(ConnectionResetError):
            client.recv(1)


def test_faults_count_across_connections_and_start_after_a_number_of_matches(server):
    server.inject(Fault.drop(after=1))
    with connect(server) as first:
        assert ask(first, "*IDN?") == IDENTITY_LINE
    with connect(server) as second:
        second.sendall(b"*IDN?\n")

        assert hung_up(second)


def test_every_makes_a_fault_hit_each_nth_match(server):
    server.inject(Fault.slow(0.25, every=2, times=None))
    with connect(server) as client:
        client.sendall(b"FUNC?\n")  # the first match is skipped over, the second is hit
        assert read_line(client) == '"VOLT"\n'

        client.sendall(b"FUNC?\n")
        client.settimeout(0.05)
        with pytest.raises(TimeoutError):
            client.recv(1)


def test_clearing_the_faults_restores_a_healthy_meter(server):
    server.inject(Fault.drop(times=None))
    server.clear_faults()
    with connect(server) as client:
        assert ask(client, "*IDN?") == IDENTITY_LINE


def test_stopping_the_server_does_not_wait_for_a_slow_reply():
    server = SimulatorServer(port=0)
    server.start()
    server.inject(Fault.slow(60))
    client = connect(server)
    try:
        client.sendall(b"*IDN?\n")
        time.sleep(0.1)  # let the handler reach its delay

        started = time.monotonic()
        stopper = threading.Thread(target=server.stop)
        stopper.start()
        stopper.join(timeout=TIMEOUT_S)

        assert not stopper.is_alive()
        assert time.monotonic() - started < TIMEOUT_S
    finally:
        client.close()


def test_noise_replaces_the_reply_with_bytes_that_are_not_text(server):
    server.inject(Fault.noise(seed=7))
    with connect(server) as client:
        client.sendall(b"*IDN?\n")

        data = read_bytes(client)

    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1
    assert any(byte >= 0x80 for byte in data)
    with pytest.raises(UnicodeDecodeError):
        data.decode("ascii")


def test_noise_is_the_same_for_the_same_seed_and_differs_between_seeds():
    def noise_for(seed: int) -> bytes:
        with SimulatorServer(port=0) as server:
            server.inject(Fault.noise(seed=seed))
            with connect(server) as client:
                client.sendall(b"*IDN?\n")
                return read_bytes(client)

    assert noise_for(1) == noise_for(1)
    assert noise_for(1) != noise_for(2)


def test_a_truncated_reply_is_the_first_part_of_the_real_one_with_its_terminator(server):
    server.inject(Fault.truncated())
    with connect(server) as client:
        reply = ask(client, "*IDN?")

    assert reply.endswith("\n")
    assert 0 < len(reply) - 1 < len(IDENTITY_LINE) - 1
    assert IDENTITY_LINE.startswith(reply[:-1])


def test_an_unterminated_reply_never_ends_its_line(server):
    server.inject(Fault.unterminated())
    with connect(server) as client:
        client.sendall(b"*IDN?\n")

        assert read_bytes(client) == IDENTITY_LINE.rstrip("\n").encode()


def test_the_reply_after_an_unterminated_one_runs_on_from_it(server):
    server.inject(Fault.unterminated())
    with connect(server) as client:
        client.sendall(b"*IDN?\nFUNC?\n")

        assert read_line(client) == IDENTITY_LINE.rstrip("\n") + '"VOLT"\n'


def test_a_reply_of_the_wrong_type_swaps_a_number_for_text_and_text_for_a_number(server):
    server.inject(Fault.wrong_type(times=2))
    with connect(server) as client:
        number_query = ask(client, "READ?")
        text_query = ask(client, "*IDN?")

    assert number_query.startswith('"')
    float(text_query)  # a number where the identity should be


def test_a_fault_is_described_on_the_command_line_as_an_effect_and_its_options():
    fault = parse_fault("slow:delay=0.5,command=READ,after=2,times=3,every=2,seed=9")

    assert (fault.effect, fault.delay_s, fault.command, fault.after, fault.times, fault.every, fault.seed) == (
        Effect.SLOW,
        0.5,
        "READ",
        2,
        3,
        2,
        9,
    )


def test_a_fault_on_the_command_line_keeps_happening_unless_told_otherwise():
    assert parse_fault("drop").times is None
    assert parse_fault("drop:times=2").times == 2


@pytest.mark.parametrize("name", [effect.value for effect in Effect])
def test_every_effect_can_be_named_on_the_command_line(name):
    assert parse_fault(name).effect.value == name


@pytest.mark.parametrize(
    "spec",
    ["", "melt", "drop:after", "drop:after=lots", "drop:colour=red", "slow:delay=-1", "drop:command=(", "drop:every=0"],
)
def test_a_fault_that_makes_no_sense_is_refused_with_a_reason(spec):
    with pytest.raises(ValueError, match=r"\w"):
        parse_fault(spec)


def test_the_standalone_simulator_arms_the_faults_it_is_given():
    started: list[SimulatorServer] = []
    original = SimulatorServer.serve_forever

    def serving(self: SimulatorServer) -> None:
        started.append(self)
        original(self)

    result: list[int] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SimulatorServer, "serve_forever", serving)
        thread = threading.Thread(target=lambda: result.append(main(["--port", "0", "--fault", "drop:after=1"])))
        thread.start()
        deadline = time.monotonic() + TIMEOUT_S
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            with connect(started[0]) as client:
                assert ask(client, "*IDN?") == IDENTITY_LINE
                client.sendall(b"*IDN?\n")

                assert hung_up(client)
        finally:
            started[0].stop()
            thread.join(timeout=TIMEOUT_S)

    assert result == [0]


def test_the_standalone_simulator_refuses_a_fault_it_cannot_parse(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--fault", "melt"])

    assert exit_info.value.code == 2
    assert "melt" in capsys.readouterr().err
