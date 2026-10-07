"""The single thread that ever touches the Meter's Transport (ADR-0002).

The GUI and CLI never call into the Transport or Driver themselves. They send the Worker requests
(`start_continuous`, `pause`, `apply_setup`, `shutdown`) and learn what happened from the events it puts on
the queue it was given, which Tk drains with `after()`.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial

from agilent34401a.driver import Driver, Identity, QueuedError
from agilent34401a.errors import MalformedReplyError, MeterError, TransportTimeoutError
from agilent34401a.meter import Function, Reading, Setup, reading_timeout
from agilent34401a.transport import Transport

_LOG = logging.getLogger(__name__)

# Readings arrive faster than a Tk window can draw when the Simulator runs instantly; past this many
# undrained events the worker waits for the consumer instead of piling up memory.
_MAX_PENDING_EVENTS = 100
_BACKOFF_S = 0.01
_DEFAULT_SHUTDOWN_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Connected:
    """The Meter answered `*IDN?` as a 34401A. `setup` is what it was already doing, read back unchanged."""

    identity: Identity
    setup: Setup


@dataclass(frozen=True)
class ConnectionFailed:
    """The Connection could not be opened, or the device that answered is not a 34401A."""

    message: str


@dataclass(frozen=True)
class ReadingTaken:
    """One Reading arrived. `timestamp` is `time.monotonic()` when it did, `setup` what it was taken under."""

    reading: Reading
    timestamp: float
    setup: Setup


@dataclass(frozen=True)
class SetupChanged:
    """The Meter's Setup, read back after a change; it differs from the one asked for if the Meter refused part."""

    setup: Setup


@dataclass(frozen=True)
class ErrorsReported:
    """The Meter's error queue held these entries after a Setup change (ADR-0005)."""

    errors: tuple[QueuedError, ...]


@dataclass(frozen=True)
class SetupFailed:
    """A Setup change could not be completed or confirmed; the Connection was resynchronised."""

    message: str


@dataclass(frozen=True)
class ReadingFailed:
    """One Reading was lost (malformed reply or timeout); the Connection was resynchronised and Continuous goes on."""

    message: str


@dataclass(frozen=True)
class WorkerFailed:
    """Something unexpected ended the Worker. It is always followed by `Disconnected`."""

    message: str


@dataclass(frozen=True)
class Disconnected:
    """The Worker has closed the Transport and its thread is about to exit. Always the last event."""


Event = (
    Connected
    | ConnectionFailed
    | ReadingTaken
    | SetupChanged
    | ErrorsReported
    | SetupFailed
    | ReadingFailed
    | WorkerFailed
    | Disconnected
)


@dataclass(frozen=True)
class _Run:
    pass


@dataclass(frozen=True)
class _Pause:
    pass


@dataclass(frozen=True)
class _Shutdown:
    pass


@dataclass(frozen=True)
class _Apply:
    setup: Setup


@dataclass(frozen=True)
class _Select:
    function: Function


_Request = _Run | _Pause | _Shutdown | _Apply | _Select


class Worker:
    """Owns one Connection on its own thread and reports through `events`."""

    def __init__(self, open_transport: Callable[[], Transport], events: "queue.Queue[Event]") -> None:
        self._open_transport = open_transport
        self._events = events
        self._requests: queue.Queue[_Request] = queue.Queue()
        self._thread = threading.Thread(target=self._main, name="agilent34401a-worker", daemon=True)

    def start(self) -> None:
        """Open the Connection and begin serving requests."""
        self._thread.start()

    def start_continuous(self) -> None:
        """Take Readings one after another until `pause` or `shutdown`."""
        self._requests.put(_Run())

    def pause(self) -> None:
        """Stop Continuous once the Reading in progress has finished."""
        self._requests.put(_Pause())

    def apply_setup(self, setup: Setup) -> None:
        """Send `setup` to the Meter, then report the Setup it is actually in and any errors it queued."""
        self._requests.put(_Apply(setup))

    def select_function(self, function: Function) -> None:
        """Switch the Meter to `function`, keeping that Function's own Range and Integration Time."""
        self._requests.put(_Select(function))

    def is_alive(self) -> bool:
        """Whether the Worker's thread is still running."""
        return self._thread.is_alive()

    def shutdown(self, timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT_S) -> bool:
        """Ask the Worker to close the Transport and exit; return whether it did within `timeout` seconds."""
        if self._thread.ident is None:
            return True  # never started, so there is no Transport to close
        self._requests.put(_Shutdown())
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _main(self) -> None:
        transport: Transport | None = None
        try:
            try:
                transport = self._open_transport()
                driver = Driver(transport)
                identity = driver.identify()
                setup = driver.read_setup()
                transport.timeout = reading_timeout(setup)
            except MeterError as error:
                self._events.put(ConnectionFailed(str(error)))
                return
            self._events.put(Connected(identity, setup))
            self._serve(driver, transport)
        except Exception as error:  # noqa: BLE001 - the worker must never die silently; the UI reports it
            _LOG.exception("The Worker failed unexpectedly")
            self._events.put(WorkerFailed(f"{type(error).__name__}: {error}"))
        finally:
            if transport is not None:
                with suppress(MeterError):
                    transport.close()
            self._events.put(Disconnected())

    def _serve(self, driver: Driver, transport: Transport) -> None:
        running = False
        while True:
            congested = running and self._events.qsize() >= _MAX_PENDING_EVENTS
            request = self._next_request(running=running, congested=congested)
            match request:
                case _Shutdown():
                    return
                case _Run():
                    running = True
                case _Pause():
                    running = False
                case _Apply(setup):
                    self._change_setup(driver, transport, partial(driver.apply, setup))
                case _Select(function):
                    self._change_setup(driver, transport, partial(driver.select_function, function))
            if not running or (congested and request is None):
                continue  # paused, or waiting for the consumer to catch up
            self._take_reading(driver, transport)

    def _next_request(self, *, running: bool, congested: bool) -> _Request | None:
        if not running:
            return self._requests.get()
        try:
            return self._requests.get(timeout=_BACKOFF_S) if congested else self._requests.get_nowait()
        except queue.Empty:
            return None

    def _take_reading(self, driver: Driver, transport: Transport) -> None:
        try:
            reading = driver.read()
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("Lost a Reading, resynchronising the Connection: %s", error)
            self._events.put(ReadingFailed(str(error)))
            transport.clear()
            return
        self._events.put(ReadingTaken(reading, time.monotonic(), driver.setup))

    def _change_setup(self, driver: Driver, transport: Transport, change: Callable[[], list[QueuedError]]) -> None:
        """Make a change to the Meter's Setup, then report the Setup it ended up in and any errors it queued."""
        try:
            errors = change()
            actual = driver.read_setup()
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("Could not change the Setup, resynchronising the Connection: %s", error)
            self._events.put(SetupFailed(str(error)))
            transport.clear()
            return
        transport.timeout = reading_timeout(actual)
        self._events.put(SetupChanged(actual))
        if errors:
            _LOG.warning("The Meter queued errors after a Setup change: %s", errors)
            self._events.put(ErrorsReported(tuple(errors)))
