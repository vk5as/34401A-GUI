"""The single thread that ever touches the Meter's Transport (ADR-0002).

The GUI and CLI never call into the Transport or Driver themselves. They send the Worker requests
(`connect`, `start_continuous`, `pause`, `single`, `start_burst`, `cancel_burst`, `apply_setup`, `disconnect`,
`shutdown`) and learn what happened from the
events it puts on the queue it was given, which Tk drains with `after()`.

One Worker serves one Connection at a time but many in turn: `connect` opens one, `disconnect` (or a lost Connection)
returns the Meter to Local and closes it, and the same thread then waits for the next `connect`. Whoever holds the
Worker therefore never has to swap it for a new one.
"""

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial

from agilent34401a.burst import Burst, plan_burst, reading_offsets
from agilent34401a.driver import Driver, Identity, QueuedError, SystemInfo
from agilent34401a.errors import (
    BurstRefusedError,
    BurstTooLargeError,
    CalibrationBlockedError,
    MalformedReplyError,
    MeterError,
    RepeatedFailureError,
    TransportError,
    TransportTimeoutError,
)
from agilent34401a.math_operations import MathOperation, MeterStatistics
from agilent34401a.meter import Function, Reading, Setup, Terminals, reading_timeout
from agilent34401a.resync import Sentinel, resynchronise
from agilent34401a.transport import LocalControl, RemoteControl, Transport, supports_device_clear
from agilent34401a.trigger import TriggerSettings, TriggerSource

_LOG = logging.getLogger(__name__)

# Readings arrive faster than a Tk window can draw when the Simulator runs instantly; past this many
# undrained events the worker waits for the consumer instead of piling up memory.
_MAX_PENDING_EVENTS = 100
_BACKOFF_S = 0.01
_DEFAULT_SHUTDOWN_TIMEOUT_S = 5.0
DEFAULT_ERROR_CHECK_INTERVAL_S = 5.0
DEFAULT_BURST_POLL_INTERVAL_S = 0.05
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


def _local_now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class Connected:
    """The Meter answered `*IDN?` as a 34401A. `setup` is what it was already doing, read back unchanged."""

    identity: Identity
    setup: Setup
    terminals: Terminals = Terminals.FRONT
    supports_device_clear: bool = True
    """Whether the Connection can send the device clear that cancels a wait for an external trigger."""


@dataclass(frozen=True)
class ConnectionFailed:
    """The Connection could not be opened, or the device that answered is not a 34401A."""

    message: str


@dataclass(frozen=True)
class ReadingTaken:
    """One Reading arrived.

    `timestamp` is `time.monotonic()` when it did, for measuring intervals; `taken_at` is the wall-clock time then
    (with its UTC offset), for people and CSV files. `setup` is what it was taken under.
    """

    reading: Reading
    timestamp: float
    setup: Setup
    taken_at: datetime = field(default_factory=_local_now)


@dataclass(frozen=True)
class BurstStarted:
    """The Meter was told to take a Burst of `expected_readings` Readings. `waits_for_trigger` means external."""

    trigger: TriggerSettings
    expected_readings: int
    waits_for_trigger: bool


@dataclass(frozen=True)
class BurstProgressed:
    """`points` of the `expected_readings` are in Reading Memory (reported when the number changes)."""

    points: int
    expected_readings: int


@dataclass(frozen=True)
class BurstFinished:
    """The Burst is complete and its Readings were collected from Reading Memory.

    Each of `readings` was also reported as a `ReadingTaken`, just before this event, stamped from the start of the
    Burst because the Meter keeps no time stamps. `trigger` is what the Burst was asked for.
    """

    readings: tuple["ReadingTaken", ...]
    trigger: TriggerSettings


@dataclass(frozen=True)
class BurstCancelled:
    """A Burst was abandoned on request (or because the Connection was closing); the Meter was sent a device clear."""


@dataclass(frozen=True)
class BurstFailed:
    """A Burst could not be started or completed. The Meter's trigger settings were put back if that was possible."""

    message: str


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
class StatisticsRead:
    """The Meter's own Statistics (minimum, maximum, average, count). Follows each Reading while they are in effect."""

    statistics: MeterStatistics


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
class ConnectionLost:
    """The Connection dropped while it was in use (cable pulled, Meter powered off). It is followed by `Disconnected`."""

    message: str


@dataclass(frozen=True)
class RawReplied:
    """A raw command went to the Meter. `reply` is its answer, or None if it was not a query."""

    command: str
    reply: str | None
    errors: tuple[QueuedError, ...]


@dataclass(frozen=True)
class RawRefused:
    """A raw command was not sent because it would change the Meter's calibration (ADR-0006)."""

    command: str
    message: str


@dataclass(frozen=True)
class RawFailed:
    """A raw command could not be completed (no answer, a malformed one, or nothing to send).

    The Connection was resynchronised, and `errors` is what the Meter's error queue held afterwards.
    """

    command: str
    message: str
    errors: tuple[QueuedError, ...] = ()


@dataclass(frozen=True)
class WorkerFailed:
    """Something unexpected ended the Worker. It is always followed by `Disconnected`."""

    message: str


@dataclass(frozen=True)
class Disconnected:
    """The Worker has returned the Meter to Local and closed the Transport. Always the last event of a Connection."""


Event = (
    Connected
    | ConnectionFailed
    | ReadingTaken
    | BurstStarted
    | BurstProgressed
    | BurstFinished
    | BurstCancelled
    | BurstFailed
    | SetupChanged
    | TerminalsChanged
    | ErrorsReported
    | RawReplied
    | RawRefused
    | RawFailed
    | StatisticsRead
    | SystemRead
    | ResetDone
    | SelfTestFinished
    | LockoutChanged
    | AdminFailed
    | SetupFailed
    | ReadingFailed
    | ConnectionLost
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
class _Single:
    pass


@dataclass(frozen=True)
class _StartBurst:
    trigger: TriggerSettings


@dataclass(frozen=True)
class _CancelBurst:
    pass


@dataclass(frozen=True)
class _Shutdown:
    pass


@dataclass(frozen=True)
class _Disconnect:
    pass


@dataclass(frozen=True)
class _Connect:
    open_transport: Callable[[], Transport]


@dataclass(frozen=True)
class _Apply:
    setup: Setup


@dataclass(frozen=True)
class _Select:
    function: Function


@dataclass(frozen=True)
class _Raw:
    command: str
    allow_calibration: bool


@dataclass(frozen=True)
class _System:
    """A request about the Meter itself rather than its Setup; `what` names it in a failure message."""

    what: str
    act: Callable[[Driver], list[Event]]  # talks to the Meter and returns the events to report first
    refresh: bool = False  # read the system information again afterwards
    setup_follows: bool = False  # the Meter may now be in another Setup, so read it back
    then: Event | None = None  # reported last, once everything above has worked


_Request = (
    _Run
    | _Pause
    | _Single
    | _StartBurst
    | _CancelBurst
    | _Shutdown
    | _Disconnect
    | _Connect
    | _Apply
    | _Select
    | _System
    | _Raw
)


class Worker:
    """Owns the Connection on its own thread and reports through `events`.

    While Continuous runs, the Worker looks at the Meter's status byte every `error_check_interval_s` seconds and
    drains the error queue only if it says there is something in it (ADR-0005). `clock` (monotonic seconds) and
    `wall_clock` (the time stamped on each Reading) are replaceable for tests.

    A reply that is lost or garbled is reported and the Connection resynchronised (see `resync`); after
    `max_consecutive_failures` of those in a row, or as soon as the Meter does not answer the resynchronisation at all,
    the Connection is reported lost. `reading_timeout` says how long to wait for a Reading in a Setup.
    """

    def __init__(  # noqa: PLR0913 - the options are what tests replace
        self,
        open_transport: Callable[[], Transport] | None,
        events: "queue.Queue[Event]",
        *,
        error_check_interval_s: float = DEFAULT_ERROR_CHECK_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _local_now,
        burst_poll_interval_s: float = DEFAULT_BURST_POLL_INTERVAL_S,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        reading_timeout: Callable[[Setup], float] = reading_timeout,
    ) -> None:
        self._first_connection = open_transport
        self._events = events
        self.error_check_interval_s = error_check_interval_s
        self._clock = clock
        self._wall_clock = wall_clock
        self.burst_poll_interval_s = burst_poll_interval_s
        self._deferred: deque[_Request] = deque()  # requests that arrived while a Burst was being waited for
        self._max_consecutive_failures = max_consecutive_failures
        self._timeout_for = reading_timeout
        self._sentinel: Sentinel | None = None
        self._failures = 0  # replies lost or garbled since the last good one
        self._next_error_check = 0.0
        self._requests: queue.Queue[_Request] = queue.Queue()
        self._thread = threading.Thread(target=self._main, name="agilent34401a-worker", daemon=True)

    def start(self) -> None:
        """Begin serving requests, first opening the Connection this Worker was created with, if any."""
        if self._first_connection is not None:
            self.connect(self._first_connection)
        self._thread.start()

    def connect(self, open_transport: Callable[[], Transport]) -> None:
        """Open a Connection with `open_transport` (called on the Worker's thread), closing the current one first."""
        self._requests.put(_Connect(open_transport))

    def disconnect(self) -> None:
        """Return the Meter to Local and close the Connection, without waiting; `Disconnected` says when it is done."""
        self._requests.put(_Disconnect())

    def start_continuous(self) -> None:
        """Take Readings one after another until `pause` or `shutdown`."""
        self._requests.put(_Run())

    def pause(self) -> None:
        """Stop Continuous once the Reading in progress has finished."""
        self._requests.put(_Pause())

    def single(self) -> None:
        """Take exactly one Reading, with the Meter set to one immediately triggered Reading (ignored during Continuous)."""
        self._requests.put(_Single())

    def start_burst(self, trigger: TriggerSettings) -> None:
        """Have the Meter take a Burst into Reading Memory and collect it; progress and the result arrive as events.

        Continuous is paused. Raises `BurstTooLargeError`, before anything is sent, if the Burst would take more
        Readings than Reading Memory holds. The Meter's trigger settings are put back when the Burst ends.
        """
        trigger.check_fits_reading_memory()
        self._requests.put(_StartBurst(trigger))

    def cancel_burst(self) -> None:
        """Abandon the Burst being waited for with a device clear. Does nothing when no Burst is running."""
        self._requests.put(_CancelBurst())

    def apply_setup(self, setup: Setup) -> None:
        """Send `setup` to the Meter, then report the Setup it is actually in and any errors it queued."""
        self._requests.put(_Apply(setup))

    def select_function(self, function: Function) -> None:
        """Switch the Meter to `function`, keeping that Function's own Range and Integration Time."""
        self._requests.put(_Select(function))

    def send_raw(self, command: str, *, allow_calibration: bool = False) -> None:
        """Send a raw SCPI command or query between Readings, then report what the Meter answered.

        A calibration write is refused unless `allow_calibration` (ADR-0006). A command that is not a pure query
        may have changed the Meter's Setup, so it is followed by the Setup the Meter is now in.
        """
        self._requests.put(_Raw(command, allow_calibration))

    def read_system(self) -> None:
        """Read the SCPI version, calibration, beeper and display state; the answer is a `SystemRead`."""
        self._requests.put(_System("Reading the system information", lambda _driver: [], refresh=True))

    def reset_meter(self) -> None:
        """Send `*RST` (ADR-0004: only on the user's request), then report the Setup the Meter is in."""
        self._requests.put(_System("Reset", self._reset, setup_follows=True, then=ResetDone()))

    def run_self_test(self) -> None:
        """Run the Meter's self-test, which takes about ten seconds; the Worker serves nothing else meanwhile."""
        self._requests.put(_System("Self-test", self._self_test, setup_follows=True))

    def read_statistics(self) -> None:
        """Ask the Meter for its Statistics now; the answer is a `StatisticsRead`."""
        self._requests.put(
            _System("Reading the Statistics", lambda driver: [StatisticsRead(driver.fetch_statistics())])
        )

    def reset_statistics(self) -> None:
        """Start the Meter's Statistics again; the answer is a `StatisticsRead` of the empty Statistics."""
        self._requests.put(_System("Resetting the Statistics", self._reset_statistics))

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
        """Ask the Worker to close the Connection and exit; return whether it did within `timeout` seconds."""
        if self._thread.ident is None:
            return True  # never started, so there is no Transport to close
        self._requests.put(_Shutdown())
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _main(self) -> None:
        """Run the Worker's thread; whatever goes wrong in it is reported, never left to end the thread silently."""
        try:
            self._wait_for_connections()
        except Exception as error:  # noqa: BLE001 - a bug here must still reach the UI
            _LOG.exception("The Worker's thread failed")
            self._events.put(WorkerFailed(f"{type(error).__name__}: {error}"))
            self._events.put(Disconnected())

    def _wait_for_connections(self) -> None:
        """Wait for a Connection to open, serve it, and go back to waiting, until asked to exit."""
        request: _Request | None = None
        while True:
            if request is None:
                request = self._requests.get()
            match request:
                case _Shutdown():
                    return
                case _Connect(open_transport):
                    request = self._connection(open_transport)
                case _:
                    request = None  # nothing is connected, so a request meant for a Connection that has gone is dropped

    def _connection(self, open_transport: Callable[[], Transport]) -> _Request | None:
        """Run one Connection from open to close; return the request that ended it, if one did and is not for it."""
        transport: Transport | None = None
        ended_by: _Request | None = None
        try:
            try:
                transport = open_transport()
                if isinstance(transport, RemoteControl):
                    transport.go_to_remote()  # RS-232 only listens to a Meter that has been told it is Remote
                driver = Driver(transport)
                identity = driver.identify()
                self._sentinel = Sentinel("*IDN?", identity.raw)
                self._failures = 0
                setup = driver.read_setup()
                terminals = driver.read_terminals()
                transport.timeout = self._timeout_for(setup)
            except MeterError as error:
                self._events.put(ConnectionFailed(str(error)))
                return None
            self._deferred.clear()
            self._events.put(Connected(identity, setup, terminals, supports_device_clear(transport)))
            try:
                ended_by = self._serve(driver, transport)
            except TransportError as error:
                _LOG.warning("The Connection was lost: %s", error)
                self._events.put(ConnectionLost(str(error)))
        except Exception as error:  # noqa: BLE001 - the worker must never die silently; the UI reports it
            _LOG.exception("The Worker failed unexpectedly")
            self._events.put(WorkerFailed(f"{type(error).__name__}: {error}"))
        finally:
            if transport is not None:
                self._release(transport)
            self._events.put(Disconnected())
        return None if isinstance(ended_by, _Disconnect) else ended_by

    @staticmethod
    def _release(transport: Transport) -> None:
        """Give the Meter back to its front panel (ADR-0004), then close the Transport. Neither may fail the exit."""
        try:
            if isinstance(transport, LocalControl):
                transport.go_to_local()
        except Exception:  # noqa: BLE001 - closing matters more than why going to Local failed
            _LOG.warning("Could not return the Meter to Local", exc_info=True)
        try:
            transport.close()
        except Exception:  # noqa: BLE001 - nothing more can be done with a Transport that will not close
            _LOG.warning("Could not close the Transport", exc_info=True)

    def _serve(self, driver: Driver, transport: Transport) -> _Request:  # noqa: C901 - a case per kind of request
        """Serve requests until one ends the Connection, and return it."""
        running = False
        while True:
            congested = running and self._events.qsize() >= _MAX_PENDING_EVENTS
            request = self._next_request(running=running, congested=congested)
            match request:
                case _Shutdown() | _Disconnect() | _Connect():
                    return request
                case _Run():
                    if not running:
                        self._next_error_check = self._clock() + self.error_check_interval_s
                    running = True
                case _Pause():
                    running = False
                case _Single():
                    if not running:
                        self._take_reading(driver, transport)
                    continue
                case _StartBurst(trigger):
                    running = False
                    ended = self._run_burst(driver, transport, trigger)
                    if ended is not None:
                        return ended
                    continue
                case _CancelBurst():
                    pass  # no Burst is being waited for
                case _System() | _Apply() | _Select() | _Raw():
                    self._serve_command(driver, transport, request)
            if not running or (congested and request is None):
                continue  # paused, or waiting for the consumer to catch up
            self._take_reading(driver, transport)
            self._check_errors_if_due(driver, transport)

    def _serve_command(self, driver: Driver, transport: Transport, request: _System | _Apply | _Select | _Raw) -> None:
        match request:
            case _System():
                self._administer(driver, transport, request)
            case _Apply(setup):
                self._change_setup(driver, transport, partial(driver.apply, setup))
            case _Select(function):
                self._change_setup(driver, transport, partial(driver.select_function, function))
            case _Raw(command, allow_calibration):
                self._send_raw(driver, transport, command, allow_calibration=allow_calibration)

    def _next_request(self, *, running: bool, congested: bool) -> _Request | None:
        if self._deferred:
            return self._deferred.popleft()
        if not running:
            return self._requests.get()
        try:
            return self._requests.get(timeout=_BACKOFF_S) if congested else self._requests.get_nowait()
        except queue.Empty:
            return None

    def _take_reading(self, driver: Driver, transport: Transport) -> None:
        trigger_before = driver.setup.trigger
        try:
            reading = driver.read()
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("Lost a Reading, resynchronising the Connection: %s", error)
            self._events.put(ReadingFailed(str(error)))
            self._recover(transport, error)
            return
        self._failures = 0
        if driver.setup.trigger != trigger_before:
            # Single and Continuous need one immediately triggered Reading, so the Driver put the Meter back to that.
            self._events.put(SetupChanged(driver.setup))
        self._events.put(ReadingTaken(reading, time.monotonic(), driver.setup, self._wall_clock()))
        if reading.math is MathOperation.STATISTICS:
            self._report_statistics(driver, transport)

    def _report_statistics(self, driver: Driver, transport: Transport) -> None:
        """Follow a Reading with the Meter's Statistics, which that Reading is part of."""
        try:
            statistics = driver.fetch_statistics()
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("Lost the Statistics, resynchronising the Connection: %s", error)
            self._events.put(ReadingFailed(f"Statistics lost: {error}"))
            transport.clear()
            return
        self._events.put(StatisticsRead(statistics))

    def _run_burst(self, driver: Driver, transport: Transport, trigger: TriggerSettings) -> _Request | None:
        """Run one Burst from start to collection; return the request that ended the Connection meanwhile, if any."""
        if trigger.source is TriggerSource.EXTERNAL and not supports_device_clear(transport):
            message = (
                "This Connection cannot send a device clear, so a wait for an external trigger could not be cancelled"
            )
            self._events.put(BurstFailed(message))
            return None
        try:
            burst = driver.start_burst(trigger)
        except (BurstTooLargeError, BurstRefusedError) as error:
            self._events.put(BurstFailed(str(error)))
            return None
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("Could not start a Burst, resynchronising the Connection: %s", error)
            transport.clear()
            self._events.put(BurstFailed(f"The Burst could not be started: {error}"))
            return None
        self._events.put(BurstStarted(trigger, burst.expected_readings, trigger.source is TriggerSource.EXTERNAL))
        started_at, wall_started_at = time.monotonic(), self._wall_clock()
        cancelled, ended, failure, readings = self._complete_burst(driver, burst)
        if cancelled or failure is not None:
            transport.clear()  # a device clear: the Meter stops waiting for triggers and returns to idle
        errors = self._finish_burst(driver, transport, burst)
        if cancelled:
            self._events.put(BurstCancelled())
        elif failure is not None:
            self._events.put(BurstFailed(f"The Burst failed: {failure}"))
        else:
            self._report_burst(burst, readings, started_at, wall_started_at)
        if errors:
            self._events.put(ErrorsReported(tuple(errors)))
        return ended

    def _complete_burst(self, driver: Driver, burst: Burst) -> tuple[bool, _Request | None, str | None, list[Reading]]:
        """Wait for the Burst and fetch it; return whether it was cancelled, by which request, why it failed, and what it took."""
        try:
            plan = plan_burst(burst.setup)
            if plan.polls:
                cancelled, ended = self._poll_burst(driver, burst, plan.timeout_s)
            else:
                driver.wait_for_burst(burst, timeout=plan.timeout_s)
                cancelled, ended = False, None
            return cancelled, ended, None, [] if cancelled else driver.fetch_burst(burst)
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("A Burst failed, resynchronising the Connection: %s", error)
            return False, None, str(error), []

    def _report_burst(
        self, burst: Burst, readings: list[Reading], started_at: float, wall_started_at: datetime
    ) -> None:
        """Report the Readings of a Burst one by one, stamped from its start, and then that it has finished."""
        offsets = reading_offsets(burst.setup, len(readings))
        taken = tuple(
            ReadingTaken(reading, started_at + offset, burst.setup, wall_started_at + timedelta(seconds=offset))
            for reading, offset in zip(readings, offsets, strict=True)
        )
        for event in taken:
            self._events.put(event)
        self._events.put(BurstFinished(taken, burst.trigger))

    def _poll_burst(self, driver: Driver, burst: Burst, timeout_s: float | None) -> tuple[bool, _Request | None]:
        """Look at the status byte until the Burst completes; return whether it was cancelled and by which request."""
        deadline = None if timeout_s is None else self._clock() + timeout_s
        reported = 0
        while True:
            progress = driver.burst_progress(burst)
            if progress.points != reported:
                reported = progress.points
                self._events.put(BurstProgressed(progress.points, burst.expected_readings))
            if progress.complete:
                return False, None
            if deadline is not None and self._clock() > deadline:
                message = f"The Burst did not complete within {timeout_s:g} s"
                raise TransportTimeoutError(message)
            try:
                request = self._requests.get(timeout=self.burst_poll_interval_s)
            except queue.Empty:
                continue
            match request:
                case _CancelBurst():
                    return True, None
                case _Shutdown() | _Disconnect() | _Connect():
                    return True, request
                case _:
                    self._deferred.append(request)  # served once the Burst is over

    def _finish_burst(self, driver: Driver, transport: Transport, burst: Burst) -> list[QueuedError]:
        """Put the Meter's trigger settings back after a Burst, however it ended; its errors are returned."""
        try:
            errors = driver.finish_burst(burst)
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("Could not restore the trigger settings after a Burst: %s", error)
            transport.clear()
            return []
        transport.timeout = reading_timeout(driver.setup)
        return errors

    def _send_raw(self, driver: Driver, transport: Transport, command: str, *, allow_calibration: bool) -> None:
        try:
            result = driver.send_raw(command, allow_calibration=allow_calibration)
        except CalibrationBlockedError as error:
            _LOG.warning("Refused a raw calibration command: %r", command)
            self._events.put(RawRefused(command, str(error)))
            return
        except ValueError as error:
            self._events.put(RawFailed(command, str(error)))
            return
        except (MalformedReplyError, TransportTimeoutError) as error:
            _LOG.warning("A raw command failed, resynchronising the Connection: %s", error)
            self._recover(transport, error)
            self._events.put(RawFailed(command, str(error), self._drain_after_failure(driver, transport)))
            return
        self._failures = 0
        self._events.put(RawReplied(command, result.reply, result.errors))
        if result.changes_meter:  # it may have changed the Setup behind the application's back
            self._change_setup(driver, transport, list)

    def _drain_after_failure(self, driver: Driver, transport: Transport) -> tuple[QueuedError, ...]:
        """Empty the error queue of a Meter that did not answer, so its complaint is not blamed on a later command."""
        try:
            return tuple(driver.drain_errors())
        except (MalformedReplyError, TransportTimeoutError) as error:
            self._recover(transport, error, count=False)
            return ()

    def _recover(self, transport: Transport, error: Exception, *, count: bool = True) -> None:
        """Put the Connection back in step after `error`, or raise a `TransportError` if it cannot be saved.

        That is the case when the Meter does not answer the resynchronisation (it is gone, or silent: pyvisa-py cannot
        tell a dropped TCP connection from a Meter that says nothing), and when `count` failures in a row have
        reached the limit, however well each resynchronisation went.
        """
        if count:
            self._failures += 1
            if self._failures >= self._max_consecutive_failures:
                message = f"{self._failures} replies in a row were lost or garbled, the last: {error}"
                raise RepeatedFailureError(message) from error
        if self._sentinel is None:
            transport.clear()
        else:
            resynchronise(transport, self._sentinel)

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
            self._recover(transport, error)
            return
        self._failures = 0
        transport.timeout = self._timeout_for(actual)
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
            self._recover(transport, error)
            return
        self._failures = 0
        if errors:
            _LOG.warning("The Meter queued errors: %s", errors)
            self._events.put(ErrorsReported(tuple(errors)))

    def _administer(self, driver: Driver, transport: Transport, request: _System) -> None:
        """Do one System request and report what came of it; a failure resynchronises the Connection."""
        try:
            events = request.act(driver)
            if request.setup_follows:
                actual = driver.read_setup()
                transport.timeout = self._timeout_for(actual)
                events.append(SetupChanged(actual))
            if request.then is not None:
                events.append(request.then)
            if request.refresh:
                events.append(SystemRead(driver.read_system()))
        except (MalformedReplyError, TransportTimeoutError, ValueError) as error:
            _LOG.warning("%s failed, resynchronising the Connection: %s", request.what, error)
            self._events.put(AdminFailed(f"{request.what} failed: {error}"))
            self._recover(transport, error)
            return
        self._failures = 0
        for event in events:
            self._events.put(event)

    @staticmethod
    def _reset(driver: Driver) -> list[Event]:
        return _report(driver.reset())

    @staticmethod
    def _reset_statistics(driver: Driver) -> list[Event]:
        errors = driver.reset_statistics()
        return [*_report(errors), StatisticsRead(driver.fetch_statistics())]

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
