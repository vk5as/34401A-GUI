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

from agilent34401a.driver import Driver, Identity, QueuedError, SystemInfo
from agilent34401a.errors import MalformedReplyError, MeterError, TransportTimeoutError
from agilent34401a.meter import Function, Reading, Setup, Terminals, reading_timeout
from agilent34401a.transport import Transport

_LOG = logging.getLogger(__name__)

# Readings arrive faster than a Tk window can draw when the Simulator runs instantly; past this many
# undrained events the worker waits for the consumer instead of piling up memory.
_MAX_PENDING_EVENTS = 100
_BACKOFF_S = 0.01
_DEFAULT_SHUTDOWN_TIMEOUT_S = 5.0
DEFAULT_ERROR_CHECK_INTERVAL_S = 5.0


@dataclass(frozen=True)
class Connected:
    """The Meter answered `*IDN?` as a 34401A. `setup` is what it was already doing, read back unchanged."""

    identity: Identity
    setup: Setup
    terminals: Terminals = Terminals.FRONT


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
class TerminalsChanged:
    """The Meter's front/rear switch was found in a different position than before (checked after Setup changes)."""

    terminals: Terminals


@dataclass(frozen=True)
class ErrorsReported:
    """The Meter's error queue held these entries after a Setup change (ADR-0005)."""

    errors: tuple[QueuedError, ...]


@dataclass(frozen=True)
class SystemRead:
    """The Meter's system information, read after a request for it or after a change to the beeper or display."""

    info: SystemInfo


@dataclass(frozen=True)
class ResetDone:
    """The Meter was reset on request (`*RST`). A `SetupChanged` with the Setup it is now in comes first."""


@dataclass(frozen=True)
class SelfTestFinished:
    """The Meter's self-test ran to the end; `passed` says how it went. Any errors it queued follow."""

    passed: bool


@dataclass(frozen=True)
class LockoutChanged:
    """The front panel's Local key was disabled (Lockout) or enabled again."""

    locked: bool


@dataclass(frozen=True)
class AdminFailed:
    """A System request (or the periodic error check) could not be completed; the Connection was resynchronised."""

    message: str


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
    | TerminalsChanged
    | ErrorsReported
    | SystemRead
    | ResetDone
    | SelfTestFinished
    | LockoutChanged
    | AdminFailed
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


@dataclass(frozen=True)
class _System:
    """A request about the Meter itself rather than its Setup; `what` names it in a failure message."""

    what: str
    act: Callable[[Driver], list[Event]]  # talks to the Meter and returns the events to report first
    refresh: bool = False  # read the system information again afterwards
    setup_follows: bool = False  # the Meter may now be in another Setup, so read it back
    then: Event | None = None  # reported last, once everything above has worked


_Request = _Run | _Pause | _Shutdown | _Apply | _Select | _System


class Worker:
    """Owns one Connection on its own thread and reports through `events`.

    While Continuous runs, the Worker looks at the Meter's status byte every `error_check_interval_s` seconds and
    drains the error queue only if it says there is something in it (ADR-0005). `clock` is replaceable for tests.
    """

    def __init__(
        self,
        open_transport: Callable[[], Transport],
        events: "queue.Queue[Event]",
        *,
        error_check_interval_s: float = DEFAULT_ERROR_CHECK_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._open_transport = open_transport
        self._events = events
        self.error_check_interval_s = error_check_interval_s
        self._clock = clock
        self._next_error_check = 0.0
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

    def read_system(self) -> None:
        """Read the SCPI version, calibration, beeper and display state; the answer is a `SystemRead`."""
        self._requests.put(_System("Reading the system information", lambda _driver: [], refresh=True))

    def reset_meter(self) -> None:
        """Send `*RST` (ADR-0004: only on the user's request), then report the Setup the Meter is in."""
        self._requests.put(_System("Reset", self._reset, setup_follows=True, then=ResetDone()))

    def run_self_test(self) -> None:
        """Run the Meter's self-test, which takes about ten seconds; the Worker serves nothing else meanwhile."""
        self._requests.put(_System("Self-test", self._self_test, setup_follows=True))

    def check_errors(self) -> None:
        """Drain the Meter's error queue now, whatever its status byte says."""
        self._requests.put(_System("Checking for errors", lambda driver: _report(driver.drain_errors())))

    def beep(self) -> None:
        """Sound the Meter's beeper once."""
        self._requests.put(_System("Beep", lambda driver: _report(driver.beep()), refresh=True))

    def set_beeper(self, *, enabled: bool) -> None:
        """Turn the beeper that sounds on errors on or off."""
        self._requests.put(
            _System("Setting the beeper", lambda driver: _report(driver.set_beeper(enabled=enabled)), refresh=True)
        )

    def show_display_text(self, text: str | None) -> None:
        """Show a message on the Meter's display, or hand the display back with None."""
        self._requests.put(
            _System("Showing the message", lambda driver: _report(driver.set_display_text(text)), refresh=True)
        )

    def set_display(self, *, on: bool) -> None:
        """Turn the Meter's display on or off."""
        self._requests.put(
            _System("Switching the display", lambda driver: _report(driver.set_display(on=on)), refresh=True)
        )

    def set_lockout(self, *, locked: bool) -> None:
        """Disable (Lockout) or enable the front panel's Local key."""
        self._requests.put(_System("Front panel lockout", partial(self._lockout, locked=locked)))

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
                terminals = driver.read_terminals()
                transport.timeout = reading_timeout(setup)
            except MeterError as error:
                self._events.put(ConnectionFailed(str(error)))
                return
            self._events.put(Connected(identity, setup, terminals))
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
                    if not running:
                        self._next_error_check = self._clock() + self.error_check_interval_s
                    running = True
                case _Pause():
                    running = False
                case _System():
                    self._administer(driver, transport, request)
                case _Apply(setup):
                    self._change_setup(driver, transport, partial(driver.apply, setup))
                case _Select(function):
                    self._change_setup(driver, transport, partial(driver.select_function, function))
            if not running or (congested and request is None):
                continue  # paused, or waiting for the consumer to catch up
            self._take_reading(driver, transport)
            self._check_errors_if_due(driver, transport)

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
            before = driver.terminals
            terminals = driver.read_terminals()
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("Could not change the Setup, resynchronising the Connection: %s", error)
            self._events.put(SetupFailed(str(error)))
            transport.clear()
            return
        transport.timeout = reading_timeout(actual)
        self._events.put(SetupChanged(actual))
        if terminals is not before:
            self._events.put(TerminalsChanged(terminals))
        if errors:
            _LOG.warning("The Meter queued errors after a Setup change: %s", errors)
            self._events.put(ErrorsReported(tuple(errors)))

    def _check_errors_if_due(self, driver: Driver, transport: Transport) -> None:
        """While Readings are being taken, look at the status byte every interval and drain only if it is set."""
        now = self._clock()
        if now < self._next_error_check:
            return
        self._next_error_check = now + self.error_check_interval_s
        try:
            errors = driver.errors_if_flagged()
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("Could not check the Meter for errors, resynchronising the Connection: %s", error)
            self._events.put(AdminFailed(f"Checking for errors failed: {error}"))
            transport.clear()
            return
        if errors:
            _LOG.warning("The Meter queued errors: %s", errors)
            self._events.put(ErrorsReported(tuple(errors)))

    def _administer(self, driver: Driver, transport: Transport, request: _System) -> None:
        """Do one System request and report what came of it; a failure resynchronises the Connection."""
        try:
            events = request.act(driver)
            if request.setup_follows:
                actual = driver.read_setup()
                transport.timeout = reading_timeout(actual)
                events.append(SetupChanged(actual))
            if request.then is not None:
                events.append(request.then)
            if request.refresh:
                events.append(SystemRead(driver.read_system()))
        except (MalformedReplyError, TransportTimeoutError, ValueError) as error:
            _LOG.warning("%s failed, resynchronising the Connection: %s", request.what, error)
            self._events.put(AdminFailed(f"{request.what} failed: {error}"))
            transport.clear()
            return
        for event in events:
            self._events.put(event)

    @staticmethod
    def _reset(driver: Driver) -> list[Event]:
        return _report(driver.reset())

    @staticmethod
    def _self_test(driver: Driver) -> list[Event]:
        passed = driver.self_test()
        return [SelfTestFinished(passed=passed), *_report(driver.drain_errors())]

    @staticmethod
    def _lockout(driver: Driver, *, locked: bool) -> list[Event]:
        errors = driver.lock_front_panel() if locked else driver.unlock_front_panel()
        return [*_report(errors), LockoutChanged(locked=locked)]


def _report(errors: list[QueuedError]) -> list[Event]:
    """Turn what the Meter's error queue held into the events to report: none when it held nothing."""
    return [ErrorsReported(tuple(errors))] if errors else []
