"""The socket Simulator: the shared Meter model served over TCP (ADR-0003).

Reach it with the VISA resource `TCPIP::<host>::<port>::SOCKET`, so tests and demos exercise the real pyvisa
stack. Run it standalone with `python -m agilent34401a.sim_server` or `agilent34401a-sim`.
"""

import argparse
import contextlib
import math
import socket
import socketserver
import sys
import threading
from collections.abc import Sequence

from agilent34401a import __version__
from agilent34401a.sim import AGILENT_IDENTITY, HEWLETT_PACKARD_IDENTITY, Simulator

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 5025  # the usual SCPI-over-socket port
_MAX_PORT = 65535
_POLL_INTERVAL_S = 0.02  # how quickly serve_forever notices stop(); the default 0.5 s makes every shutdown slow
_MAX_LINE_BYTES = 4096  # no SCPI command is anywhere near this long, so a longer line is a client gone wrong
_IDENTITIES = {"hp": HEWLETT_PACKARD_IDENTITY, "agilent": AGILENT_IDENTITY}


class _Handler(socketserver.StreamRequestHandler):
    """Serves one client: a line in, and for a query one line back."""

    server: "_Server"

    def handle(self) -> None:
        try:
            while True:
                raw = self.rfile.readline(_MAX_LINE_BYTES + 1)
                if not raw:
                    return  # the client closed the connection
                if len(raw) > _MAX_LINE_BYTES and not raw.endswith(b"\n"):
                    return  # a line that never ends: drop the client rather than buffer it
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                reply = self.server.process(line)
                if reply is not None:
                    self.wfile.write(f"{reply}\n".encode("ascii", errors="replace"))
                    self.wfile.flush()
        except OSError:
            return  # the client went away mid-conversation, which is its right

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.forget(self.request)


class _Server(socketserver.ThreadingTCPServer):
    # Reusing an address is what lets a restarted server bind at once, but on Windows it would let a second
    # process share the port instead.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def __init__(self, address: tuple[str, int], simulator: Simulator) -> None:
        super().__init__(address, _Handler)
        self._simulator = simulator
        self._meter_lock = threading.Lock()
        self._clients_lock = threading.Lock()
        self._clients: set[socket.socket] = set()

    def process(self, line: str) -> str | None:
        """Run one line on the shared Meter and return its reply, or None if it does not produce one."""
        with self._meter_lock:
            self._simulator.write(line)
            return self._simulator.read() if self._simulator.has_reply else None

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        # Registered here, on the accepting thread, so stop() can never miss a client whose handler has not started.
        request, address = super().get_request()
        with self._clients_lock:
            self._clients.add(request)
        return request, address

    def forget(self, client: socket.socket) -> None:
        with self._clients_lock:
            self._clients.discard(client)

    def close_clients(self) -> None:
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)  # wakes the handler blocked reading, which then ends


class SimulatorServer:
    """A Simulator listening on a TCP port. One Meter is shared by every client, as with real hardware.

    The server takes over the Simulator it is given: it sets that Simulator's timeout to infinity, because a real
    Meter takes as long as it takes and giving up is the client's business.
    """

    def __init__(
        self, simulator: Simulator | None = None, *, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT
    ) -> None:
        self._simulator = simulator if simulator is not None else Simulator()
        self._simulator.timeout = math.inf
        self._host = host
        self._requested_port = port
        self._lifecycle = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._serving = threading.Event()  # shutdown() blocks forever on a server that is not serving

    @property
    def port(self) -> int:
        if self._server is None:
            message = "The Simulator server is not started, so it has no port yet"
            raise RuntimeError(message)
        return int(self._server.server_address[1])

    @property
    def resource_name(self) -> str:
        """The VISA resource that reaches this server."""
        return f"TCPIP::{self._host}::{self.port}::SOCKET"

    def bind(self) -> None:
        """Claim the port without serving yet, so `port` and `resource_name` are known."""
        with self._lifecycle:
            self._bind()

    def start(self) -> None:
        """Begin serving on a background thread."""
        with self._lifecycle:
            server = self._bind()
            self._serving.set()  # before the thread runs, so a stop() straight after start() still waits for it
            self._thread = threading.Thread(
                target=self._serve, args=(server,), name="agilent34401a-sim-server", daemon=True
            )
            self._thread.start()

    def serve_forever(self) -> None:
        """Serve on the calling thread until `stop` is called from another. Call `bind` first."""
        server = self._server
        if server is None:
            message = "Call bind() before serve_forever()"
            raise RuntimeError(message)
        self._serving.set()
        self._serve(server)

    def stop(self) -> None:
        """Stop serving and close every open connection. Harmless if already stopped or never started."""
        with self._lifecycle:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
        if server is None:
            return
        if self._serving.is_set():
            server.shutdown()
        server.close_clients()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> "SimulatorServer":  # noqa: PYI034 - Self needs Python 3.11
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _bind(self) -> _Server:
        if self._server is not None:
            message = "The Simulator server is already started"
            raise RuntimeError(message)
        self._server = _Server((self._host, self._requested_port), self._simulator)
        return self._server

    def _serve(self, server: _Server) -> None:
        try:
            server.serve_forever(poll_interval=_POLL_INTERVAL_S)
        finally:
            self._serving.clear()


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= _MAX_PORT:
        message = f"{text!r} is not a port number (0 to {_MAX_PORT})"
        raise argparse.ArgumentTypeError(message)
    return port


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agilent34401a-sim",
        description="Serve the 34401A Simulator over TCP, reachable as a VISA TCPIP::host::port::SOCKET resource.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--host", default=_DEFAULT_HOST, help="address to listen on (default: %(default)s)")
    parser.add_argument(
        "--port", type=_port, default=_DEFAULT_PORT, help="port to listen on, 0 for any (default: %(default)s)"
    )
    parser.add_argument("--identity", choices=sorted(_IDENTITIES), default="hp", help="which firmware to pretend to be")
    parser.add_argument("--time-scale", type=float, help="1 is real time, 0 is instant (default: real time)")
    args = parser.parse_args(argv)
    if args.time_scale is not None and args.time_scale < 0:
        parser.error("--time-scale cannot be negative")
    simulator = Simulator(identity=_IDENTITIES[args.identity], time_scale=args.time_scale)
    server = SimulatorServer(simulator, host=args.host, port=args.port)
    try:
        server.bind()  # claim the port first, so the banner can name the real one
    except OSError as error:
        sys.stderr.write(f"agilent34401a-sim: error: cannot listen on {args.host}:{args.port}: {error}\n")
        return 1
    try:
        sys.stdout.write(f"Simulator listening on {server.resource_name}\n")
        sys.stdout.flush()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
