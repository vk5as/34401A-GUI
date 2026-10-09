"""Probe: find the RS-232 settings at which the Meter answers.

Probe opens its own temporary Transports, one per attempt, so it must only run while no Connection is open on the
port (ADR-0002: one thread touches a Transport at a time). It tries every baud rate with each of the Meter's Framings
and asks `*IDN?`; a 34401A's answer is the way in. `ProbeJob` runs it on a thread of its own for the GUI.
"""

import contextlib
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from agilent34401a.backend import Backend
from agilent34401a.driver import Driver, Identity
from agilent34401a.errors import MeterError
from agilent34401a.serial_config import BAUD_RATES, METER_FRAMINGS, FlowControl, SerialSettings
from agilent34401a.transport import LocalControl, RemoteControl, Transport

_LOG = logging.getLogger(__name__)

OpenSerialPort = Callable[[SerialSettings], Transport]
"""Opens the serial port with the given settings; it raises a `TransportError` when the port cannot be opened."""

NOT_FOUND_GUIDANCE = (
    "Check that the cable is a null-modem (crossed) cable, that the Meter's I/O menu is set to RS-232 rather than "
    "GPIB, that this is the right port and no other program is using it, and that the Flow Control matches the "
    "Meter's Flow Control setting."
)

_REPLY_BITS = 400  # *IDN?'s reply is some 40 characters of ten bits each
_REPLY_MARGIN_S = 0.4
_JOIN_TIMEOUT_S = 5.0


def probe_candidates(base: SerialSettings, *, include_flow_control: bool = False) -> list[SerialSettings]:
    """List the settings to try, likeliest first, all sharing `base`'s port, terminator and DTR/RTS lines.

    Every baud rate is tried from 9600 down, each with the Meter's Framings (8N1, its factory setting, then 7E1 and
    7O1). Flow Control stays `base`'s unless `include_flow_control`, which adds the others once `base`'s is exhausted.
    """
    flows = [base.flow_control]
    if include_flow_control:
        flows += [flow for flow in FlowControl if flow is not base.flow_control]
    return [
        replace(base, baud=baud, framing=framing, flow_control=flow)
        for flow in flows
        for baud in sorted(BAUD_RATES, reverse=True)
        for framing in METER_FRAMINGS
    ]


def reply_timeout(baud: int) -> float:
    """Seconds to wait for `*IDN?` to be answered at `baud`: the time its reply takes to arrive, plus a margin."""
    return _REPLY_BITS / baud + _REPLY_MARGIN_S


@dataclass(frozen=True)
class ProbeProgress:
    """Probe is about to try attempt number `attempt` of `total` (counting from 1) with `settings`."""

    attempt: int
    total: int
    settings: SerialSettings

    @property
    def message(self) -> str:
        """Say what is being tried, such as `Trying 9600 baud 8N1 (1/18)`; Flow Control only when there is some."""
        settings = self.settings
        flow = "" if settings.flow_control is FlowControl.NONE else f", {settings.flow_control.label} Flow Control"
        return f"Trying {settings.baud} baud {settings.framing.label}{flow} ({self.attempt}/{self.total})"


@dataclass(frozen=True)
class ProbeResult:
    """How a Probe ended: the settings and identity of the Meter it found, or why it found none."""

    found: SerialSettings | None
    identity: Identity | None
    tried: int
    total: int
    cancelled: bool = False
    problem: str = ""
    included_flow_control: bool = False

    @property
    def message(self) -> str:
        """What to tell the user: where the Meter was found, or what happened and what to check."""
        if self.found is not None:
            return f"Found the Meter at {self.found.describe()}."
        if self.cancelled:
            return "Probe cancelled."
        if self.problem:
            return self.problem
        advice = [NOT_FOUND_GUIDANCE]
        if not self.included_flow_control:
            advice.append("Include Flow Control in the Probe to try the other Flow Controls as well.")
        return f"No Meter answered at any of the {self.tried} settings tried. {' '.join(advice)}"


def run_probe(  # noqa: PLR0913 - every option after the port settings is keyword-only and optional
    open_serial: OpenSerialPort,
    base: SerialSettings,
    *,
    include_flow_control: bool = False,
    progress: Callable[[ProbeProgress], None] | None = None,
    cancel: threading.Event | None = None,
    timeout_for: Callable[[int], float] = reply_timeout,
) -> ProbeResult:
    """Try each candidate until the Meter answers `*IDN?` as a 34401A, and say how it went.

    `progress` hears of every attempt before it is made. Setting `cancel` stops Probe before its next attempt, which
    is at most one reply timeout (under two seconds at 300 baud) away. A port that cannot be opened with the first
    attempt's settings ends Probe at once; one that refuses a later Framing just skips it. `timeout_for` gives the
    seconds to wait for a reply at a baud rate. The Meter is left in Local, with the port closed.
    """
    candidates = probe_candidates(base, include_flow_control=include_flow_control)
    total = len(candidates)
    tried = 0

    def result(
        found: SerialSettings | None = None,
        identity: Identity | None = None,
        *,
        cancelled: bool = False,
        problem: str = "",
    ) -> ProbeResult:
        return ProbeResult(
            found,
            identity,
            tried,
            total,
            cancelled=cancelled,
            problem=problem,
            included_flow_control=include_flow_control,
        )

    for attempt, candidate in enumerate(candidates, start=1):
        if cancel is not None and cancel.is_set():
            return result(cancelled=True)
        if progress is not None:
            progress(ProbeProgress(attempt, total, candidate))
            if cancel is not None and cancel.is_set():  # cancelled while the listener was told
                return result(cancelled=True)
        tried = attempt
        try:
            transport = open_serial(candidate)
        except MeterError as error:
            if attempt == 1:  # the usual Framing could not be opened, so it is the port that is wrong
                return result(problem=str(error))
            _LOG.debug("Could not open %s: %s", candidate.describe(), error)  # a Framing this port refuses
            continue
        identity = _ask(transport, candidate, timeout_for(candidate.baud))
        if identity is not None:
            return result(candidate, identity)
    if cancel is not None and cancel.is_set():
        return result(cancelled=True)
    return result()


def _ask(transport: Transport, settings: SerialSettings, timeout: float) -> Identity | None:
    """Ask `*IDN?` over `transport` and close it; return the identity if a 34401A answered."""
    try:
        transport.timeout = timeout
        if isinstance(transport, RemoteControl):
            transport.go_to_remote()
        try:
            identity = Driver(transport).identify()
        except MeterError as error:  # silence, garbage or another kind of device: not our Meter at these settings
            _LOG.debug("No Meter at %s: %s", settings.describe(), error)
            return None
    except MeterError as error:
        _LOG.debug("Could not try %s: %s", settings.describe(), error)
        return None
    else:
        if isinstance(transport, LocalControl):
            with contextlib.suppress(MeterError):
                transport.go_to_local()
        return identity
    finally:
        with contextlib.suppress(MeterError):
            transport.close()


def start_probe(base: SerialSettings, backend: Backend, include_flow_control: bool) -> "ProbeJob":  # noqa: FBT001
    """Make a Probe of the real serial port `base` names, opened through `backend`; the caller starts it."""
    # Imported here so that nothing needs pyvisa until a real Probe is wanted.
    from agilent34401a.connection import ConnectionSettings, open_transport  # noqa: PLC0415

    def open_port(candidate: SerialSettings) -> Transport:
        return open_transport(ConnectionSettings(backend=backend, serial=candidate))

    return ProbeJob(open_port, base, include_flow_control=include_flow_control)


class ProbeJob:
    """A Probe running on its own thread. Watch `events`; the last one is always a `ProbeResult`."""

    def __init__(
        self, open_serial: OpenSerialPort, base: SerialSettings, *, include_flow_control: bool = False
    ) -> None:
        self.events: queue.Queue[ProbeProgress | ProbeResult] = queue.Queue()
        self._open_serial = open_serial
        self._base = base
        self._include_flow_control = include_flow_control
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name="agilent34401a-probe", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        """Stop before the next attempt; the `ProbeResult` still arrives, marked as cancelled."""
        self._cancel.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float = _JOIN_TIMEOUT_S) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            result = run_probe(
                self._open_serial,
                self._base,
                include_flow_control=self._include_flow_control,
                progress=self.events.put,
                cancel=self._cancel,
            )
        except Exception as error:  # noqa: BLE001 - a Probe that dies silently would leave the dialog waiting forever
            _LOG.exception("The Probe failed unexpectedly")
            total = len(probe_candidates(self._base, include_flow_control=self._include_flow_control))
            result = ProbeResult(
                found=None,
                identity=None,
                tried=0,
                total=total,
                problem=f"Probe failed: {type(error).__name__}: {error}",
            )
        self.events.put(result)
