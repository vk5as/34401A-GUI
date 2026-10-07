import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import pytest

from agilent34401a.sim import AGILENT_IDENTITY, HEWLETT_PACKARD_IDENTITY, Simulator
from agilent34401a.sim_server import SimulatorServer, main

TIMEOUT_S = 5.0


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


def test_server_listens_on_an_ephemeral_port_and_names_its_visa_resource(server):
    assert server.port > 0
    assert server.resource_name == f"TCPIP::127.0.0.1::{server.port}::SOCKET"


def test_a_query_gets_one_line_back(server):
    with connect(server) as client:
        assert ask(client, "*IDN?") == f"{HEWLETT_PACKARD_IDENTITY}\n"


def test_a_command_gets_no_reply_but_changes_the_meter(server):
    with connect(server) as client:
        client.sendall(b'FUNC "RES"\n')

        assert ask(client, "FUNC?") == '"RES"\n'


def test_pipelined_queries_are_answered_in_order(server):
    with connect(server) as client:
        client.sendall(b"*IDN?\nFUNC?\nSYST:ERR?\n")

        assert read_line(client) == f"{HEWLETT_PACKARD_IDENTITY}\n"
        assert read_line(client) == '"VOLT"\n'
        assert read_line(client) == '+0,"No error"\n'


def test_carriage_return_line_endings_are_accepted(server):
    with connect(server) as client:
        client.sendall(b"*IDN?\r\n")

        assert read_line(client) == f"{HEWLETT_PACKARD_IDENTITY}\n"


def test_bytes_that_are_not_text_do_not_kill_the_connection(server):
    with connect(server) as client:
        client.sendall(b"\xff\xfe\x00garbage\n")

        assert ask(client, "SYST:ERR?") == '-113,"Undefined header"\n'
        assert ask(client, "*IDN?") == f"{HEWLETT_PACKARD_IDENTITY}\n"


def test_blank_lines_are_ignored(server):
    with connect(server) as client:
        client.sendall(b"\n\n")

        assert ask(client, "*IDN?") == f"{HEWLETT_PACKARD_IDENTITY}\n"


def test_every_client_talks_to_the_same_meter(server):
    with connect(server) as first, connect(server) as second:
        first.sendall(b'FUNC "FRES"\n')
        ask(first, "SYST:ERR?")  # connections are served independently, so wait until the command has been handled

        assert ask(second, "FUNC?") == '"FRES"\n'


def test_the_server_serves_the_simulator_it_was_given():
    with (
        SimulatorServer(Simulator(identity=AGILENT_IDENTITY, dc_voltage=3.3), port=0) as server,
        connect(server) as client,
    ):
        assert ask(client, "*IDN?") == f"{AGILENT_IDENTITY}\n"
        assert float(ask(client, "READ?")) == pytest.approx(3.3)


def test_a_slow_reading_is_never_cut_short_by_the_simulators_own_timeout():
    # Timeouts belong to the client, as with a real Meter: the server just takes as long as the Meter would.
    simulator = Simulator(time_scale=1, sleep=lambda _seconds: None)
    simulator.write("VOLT:DC:NPLC 100")  # 4 s per Reading, beyond the Simulator's own 2 s default
    with SimulatorServer(simulator, port=0) as server, connect(server) as client:
        assert float(ask(client, "READ?")) == pytest.approx(1.0)


def test_stopping_the_server_closes_open_connections():
    server = SimulatorServer(port=0)
    server.start()
    client = connect(server)
    assert ask(client, "*IDN?")

    server.stop()

    assert client.recv(1) == b""
    client.close()


def test_stopping_twice_or_before_starting_is_harmless():
    server = SimulatorServer(port=0)
    server.stop()
    server.start()
    server.stop()
    server.stop()


def test_starting_twice_is_refused():
    with SimulatorServer(port=0) as server, pytest.raises(RuntimeError, match="already"):
        server.start()


def test_the_port_is_unknown_until_the_server_starts():
    with pytest.raises(RuntimeError, match="not started"):
        _ = SimulatorServer(port=0).port


def test_main_prints_the_resource_to_connect_to_and_serves_until_interrupted(monkeypatch, capsys):
    served = []
    monkeypatch.setattr(SimulatorServer, "serve_forever", lambda self: served.append(self.resource_name))

    assert main(["--port", "0", "--identity", "agilent"]) == 0

    out = capsys.readouterr().out
    assert served
    assert served[0] in out
    assert out.startswith("Simulator listening on TCPIP::127.0.0.1::")


def test_main_stops_cleanly_on_ctrl_c(monkeypatch):
    def interrupt(_self):
        raise KeyboardInterrupt

    monkeypatch.setattr(SimulatorServer, "serve_forever", interrupt)

    assert main(["--port", "0"]) == 0


def test_main_rejects_a_negative_time_scale():
    with pytest.raises(SystemExit) as exit_info:
        main(["--time-scale", "-1"])

    assert exit_info.value.code == 2


def test_running_the_module_starts_a_standalone_simulator():
    process = subprocess.Popen(
        [sys.executable, "-m", "agilent34401a.sim_server", "--port", "0", "--time-scale", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        stdout = process.stdout
        assert stdout is not None
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: lines.put(stdout.readline()), daemon=True).start()
        banner = lines.get(timeout=TIMEOUT_S)  # a child that dies before printing must fail the test, not hang it
        port = int(banner.strip().split("::")[2])
        with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT_S) as client:
            assert ask(client, "*IDN?") == f"{HEWLETT_PACKARD_IDENTITY}\n"
    finally:
        process.terminate()
        process.wait(timeout=TIMEOUT_S)
        if process.stdout is not None:
            process.stdout.close()


def test_a_client_that_vanishes_mid_conversation_does_not_disturb_the_server(server, capfd):
    rude = connect(server)
    rude.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close with a reset, not a goodbye
    rude.sendall(b"*IDN?\n")
    rude.close()

    with connect(server) as client:
        assert ask(client, "*IDN?") == f"{HEWLETT_PACKARD_IDENTITY}\n"
    server.stop()  # every handler thread has been told to finish before stderr is inspected
    time.sleep(0.1)
    assert "Traceback" not in capfd.readouterr().err


def test_a_query_after_the_server_stopped_gets_no_answer_and_no_server_noise(capfd):
    server = SimulatorServer(port=0)
    server.start()
    client = connect(server)
    assert ask(client, "*IDN?")
    server.stop()

    try:
        client.sendall(b"*IDN?\n")
        reply = read_line(client)
    except OSError:
        reply = ""

    assert reply == ""
    client.close()
    assert "Traceback" not in capfd.readouterr().err


def test_a_query_the_meter_has_no_answer_to_gets_no_reply_and_queues_an_error(server):
    with connect(server) as client:
        client.sendall(b"BOGUS?\n")

        assert ask(client, "*IDN?") == f"{HEWLETT_PACKARD_IDENTITY}\n"
        assert ask(client, "SYST:ERR?") == '-113,"Undefined header"\n'


def test_serve_forever_serves_on_the_callers_thread_after_bind():
    server = SimulatorServer(port=0)
    server.bind()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=TIMEOUT_S) as client:
            assert ask(client, "*IDN?") == f"{HEWLETT_PACKARD_IDENTITY}\n"
    finally:
        server.stop()
        thread.join(timeout=TIMEOUT_S)
    assert not thread.is_alive()


def test_serve_forever_before_bind_is_refused():
    with pytest.raises(RuntimeError, match="bind"):
        SimulatorServer(port=0).serve_forever()


def test_a_query_with_arguments_still_gets_exactly_one_reply(server):
    with connect(server) as client:
        client.sendall(b"*IDN? extra\n")

        assert read_line(client) == f"{HEWLETT_PACKARD_IDENTITY}\n"
        assert ask(client, "FUNC?") == '"VOLT"\n'


def test_a_client_that_never_ends_its_line_is_dropped_instead_of_buffered_forever(server):
    with connect(server) as client:
        client.sendall(b"A" * 100_000)

        assert client.recv(1) == b""


def closed_by_the_server(client: socket.socket) -> bool:
    """Whether the server hung up: an orderly close reads as b"", a connection still queued gets reset."""
    try:
        return client.recv(1) == b""
    except ConnectionResetError:
        return True


def test_a_connection_accepted_just_before_stop_is_closed_too():
    server = SimulatorServer(port=0)
    server.start()
    clients = [connect(server) for _ in range(20)]  # accepted, but their handler threads may not have run yet
    try:
        server.stop()

        assert all(closed_by_the_server(client) for client in clients)
    finally:
        for client in clients:
            client.close()


def test_main_reports_a_port_that_cannot_be_bound_and_exits_non_zero(capsys):
    with SimulatorServer(port=0) as taken:
        assert main(["--port", str(taken.port)]) == 1

    assert "cannot listen" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["-1", "65536", "lots"])
def test_main_rejects_a_port_that_is_not_a_port(port):
    with pytest.raises(SystemExit) as exit_info:
        main(["--port", port])

    assert exit_info.value.code == 2
