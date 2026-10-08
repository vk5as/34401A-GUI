"""Fault injection in the socket Simulator: slow replies, dropped connections and garbage bytes (ADR-0003)."""

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from agilent34401a.sim import HEWLETT_PACKARD_IDENTITY
from agilent34401a.sim_faults import Fault
from agilent34401a.sim_server import SimulatorServer

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
    server.inject(Fault.slow(0.3))
    with connect(server) as client:
        client.sendall(b"*IDN?\n")
        client.settimeout(0.05)
        with pytest.raises(TimeoutError):
            client.recv(1)  # nothing yet: the Meter is taking its time

        client.settimeout(TIMEOUT_S)
        assert read_line(client) == IDENTITY_LINE


def test_a_fault_applies_once_by_default_and_the_next_reply_is_prompt(server):
    server.inject(Fault.slow(0.2))
    with connect(server) as client:
        ask(client, "*IDN?")

        started = time.monotonic()
        assert ask(client, "FUNC?") == '"VOLT"\n'
        assert time.monotonic() - started < 0.15


def test_a_slow_fault_can_apply_to_every_reply(server):
    server.inject(Fault.slow(0.1, times=None))
    with connect(server) as client:
        for _ in range(3):
            started = time.monotonic()
            ask(client, "FUNC?")
            assert time.monotonic() - started >= 0.09


def test_a_slow_fault_can_name_the_command_it_hits(server):
    server.inject(Fault.slow(0.3, command="READ"))
    with connect(server) as client:
        started = time.monotonic()
        assert ask(client, "*IDN?") == IDENTITY_LINE
        assert time.monotonic() - started < 0.25

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
