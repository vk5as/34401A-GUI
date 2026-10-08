"""Applying a Preset: finding out which of its settings the Meter did not take.

The Meter has no way to say "that setting was refused" other than its error queue, and some refusals leave no error at
all, so a Preset is checked the other way round: the Setup asked for is compared, setting by setting, with the Setup
the Meter reports once it has been sent. Whatever differs was rejected, and is reported, never swallowed.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from agilent34401a.driver import QueuedError
from agilent34401a.math_operations import MathSettings
from agilent34401a.meter import Autozero, Setup, format_range
from agilent34401a.preset import Preset
from agilent34401a.trigger import TriggerSettings
from agilent34401a.worker import (
    ConnectionFailed,
    ConnectionLost,
    Disconnected,
    ErrorsReported,
    Event,
    SetupChanged,
    SetupFailed,
    TerminalsChanged,
    WorkerFailed,
)

# Settings are sent with "%g", which keeps six significant digits, and the Meter rounds what it is given.
_RELATIVE_TOLERANCE = 1e-5

_Show = Callable[[Any], str]


def _label(value: object) -> str:
    return "none" if value is None else str(getattr(value, "label", value))


@dataclass(frozen=True)
class Rejection:
    """One setting of a Preset that the Meter does not have: what was asked for and what it has instead."""

    setting: str
    wanted: str
    actual: str

    def __str__(self) -> str:
        verb = "is in" if self.setting == "Function" else "has"
        return f"{self.setting}: asked for {self.wanted}, the Meter {verb} {self.actual}"


class _Comparison:
    """Collects the settings that differ, in the order they are compared."""

    def __init__(self) -> None:
        self.found: list[Rejection] = []

    def check(self, setting: str, asked: object, has: object, show: _Show = _label) -> None:
        if not _same(asked, has):
            self.found.append(Rejection(setting, show(asked), show(has)))


def _same(asked: object, has: object) -> bool:
    if isinstance(asked, float) and isinstance(has, float):
        return math.isclose(asked, has, rel_tol=_RELATIVE_TOLERANCE, abs_tol=0.0)
    return asked == has


def rejected_settings(wanted: Setup, actual: Setup) -> tuple[Rejection, ...]:
    """Return every setting of `wanted` that `actual`, the Setup the Meter reported after it, does not have.

    They come in the order the settings are sent. If the Meter is in another Function nothing else is comparable, so
    that is all there is to report.
    """
    function = wanted.function
    if actual.function is not function:
        return (Rejection("Function", function.label, actual.function.label),)
    comparison = _Comparison()
    check = comparison.check
    if function.ranges:
        check(
            "Range",
            wanted.range,
            actual.range,
            lambda value: "Autorange" if value is None else format_range(function, value),
        )
    check("Resolution", wanted.resolution, actual.resolution)
    check("Integration Time", wanted.nplc, actual.nplc, lambda value: f"{value:g} NPLC")
    check("Gate Time", wanted.gate_time, actual.gate_time)
    check("AC Filter", wanted.ac_filter, actual.ac_filter)
    # Once takes one offset measurement and leaves Autozero off, so the Meter never reports it back as Once.
    expected_autozero = Autozero.OFF if wanted.autozero is Autozero.ONCE else wanted.autozero
    if not _same(expected_autozero, actual.autozero):
        comparison.found.append(Rejection("Autozero", _label(wanted.autozero), _label(actual.autozero)))
    check("Input Impedance", wanted.input_impedance, actual.input_impedance)
    _compare_trigger(comparison, wanted.trigger, actual.trigger)
    if function.has_math:
        _compare_math(comparison, wanted.math, actual.math)
    return tuple(comparison.found)


def _compare_trigger(comparison: _Comparison, wanted: TriggerSettings, actual: TriggerSettings) -> None:
    check = comparison.check
    check("Trigger Source", wanted.source, actual.source)
    check("Trigger Delay", wanted.delay, actual.delay, lambda value: "automatic" if value is None else f"{value:g} s")
    check("Sample Count", wanted.sample_count, actual.sample_count, str)
    check(
        "Trigger Count",
        wanted.trigger_count,
        actual.trigger_count,
        lambda value: "infinite" if value is None else str(value),
    )


def _compare_math(comparison: _Comparison, wanted: MathSettings, actual: MathSettings) -> None:
    check = comparison.check
    check("Math Operation", wanted.operation, actual.operation, lambda value: "none" if value is None else value.label)
    check("Null offset", wanted.null_offset, actual.null_offset, lambda value: f"{value:g}")
    check("dB reference", wanted.db_reference, actual.db_reference, lambda value: f"{value:g} dBm")
    check(
        "dBm Reference Resistance",
        wanted.dbm_reference_resistance,
        actual.dbm_reference_resistance,
        lambda value: f"{value:g} Ω",
    )
    check("Limit Test lower bound", wanted.limit_lower, actual.limit_lower, lambda value: f"{value:g}")
    check("Limit Test upper bound", wanted.limit_upper, actual.limit_upper, lambda value: f"{value:g}")


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


@dataclass(frozen=True)
class ApplyReport:
    """How applying a Preset went: what the Meter did not take, what it complained of, or why it is not known.

    `actual` is the Setup the Meter reported afterwards. `failure` is set instead when it could not be found out.
    """

    preset: str
    rejections: tuple[Rejection, ...] = ()
    errors: tuple[QueuedError, ...] = ()
    failure: str | None = None
    actual: Setup | None = None

    @property
    def applied(self) -> bool:
        """Whether the Meter took every setting and queued no error."""
        return self.failure is None and not self.rejections and not self.errors

    @property
    def summary(self) -> str:
        name = f"Preset '{self.preset}'"
        if self.failure is not None:
            return f"{name} could not be confirmed: {self.failure}"
        if self.rejections:
            return f"{name} was only partly applied: the Meter did not take {_count(len(self.rejections), 'setting')}."
        if self.errors:
            return f"{name} was applied, but the Meter queued {_count(len(self.errors), 'error')}."
        return f"{name} was applied."

    @property
    def details(self) -> tuple[str, ...]:
        """One line for each rejected setting and each error the Meter queued."""
        return (
            *(str(rejection) for rejection in self.rejections),
            *(f"Meter error {error.code}: {error.message}" for error in self.errors),
        )


class PresetApplier:
    """Sends a Preset's Setup and works out how it went from the Worker's events. Feed it every event with `handle`.

    `submit` sends a Setup to the Worker and says whether it did (it does not while the Meter is busy with another
    change, or when there is no Connection). `on_report` is called with the `ApplyReport` as soon as the Meter has
    said which Setup it is in, and once more, with the errors added, if its error queue then turns out to have held
    some: the Worker reports those right behind the Setup, so they belong to the same Preset.
    """

    def __init__(self, submit: Callable[[Setup], bool], on_report: Callable[[ApplyReport], None]) -> None:
        self._submit = submit
        self._on_report = on_report
        self._pending: Preset | None = None
        self._latest: ApplyReport | None = None  # the report that errors reported right now would be added to

    @property
    def busy(self) -> bool:
        """Whether a Preset has been sent and the Meter has not yet answered."""
        return self._pending is not None

    def apply(self, preset: Preset) -> bool:
        """Send `preset`'s Setup. Returns False, having sent nothing, if one is still being applied or it was refused."""
        if self._pending is not None or not self._submit(preset.setup):
            return False
        self._pending = preset
        self._latest = None
        return True

    def handle(self, event: Event) -> None:
        match event:
            case SetupChanged(actual):
                pending, self._pending = self._pending, None
                if pending is None:
                    self._latest = None
                    return
                self._report(ApplyReport(pending.name, rejected_settings(pending.setup, actual), actual=actual))
            case ErrorsReported(errors):
                if self._latest is not None:
                    self._report(replace(self._latest, errors=errors))
                self._latest = None
            case SetupFailed(message):
                self._fail(message)
            case ConnectionLost(message) | ConnectionFailed(message) | WorkerFailed(message):
                self._fail(f"the Connection ended ({message})")
            case Disconnected():
                self._fail("the Connection ended")
            case TerminalsChanged():
                pass  # reported between the Setup and its errors
            case _:
                self._latest = None  # anything else means the errors that went with the Setup have been and gone

    def _report(self, report: ApplyReport) -> None:
        self._latest = report
        self._on_report(report)

    def _fail(self, message: str) -> None:
        pending, self._pending = self._pending, None
        self._latest = None
        if pending is not None:
            self._on_report(ApplyReport(pending.name, failure=message))
