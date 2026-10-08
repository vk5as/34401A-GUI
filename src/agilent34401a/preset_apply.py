"""Applying a Preset: finding out which of its settings the Meter did not take.

The Meter has no way to say "that setting was refused" other than its error queue, and some refusals leave no error at
all, so a Preset is checked the other way round: the Setup asked for is compared, setting by setting, with the Setup
the Meter reports once it has been sent. Whatever differs was rejected, and is reported, never swallowed.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agilent34401a.math_operations import MathSettings
from agilent34401a.meter import Autozero, Setup, format_range
from agilent34401a.trigger import TriggerSettings

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
