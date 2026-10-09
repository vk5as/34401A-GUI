"""Applying a Preset through the Worker: it goes in order, and everything the Meter would not take is reported."""

import queue

import pytest

from agilent34401a.driver import QueuedError
from agilent34401a.math_operations import MathOperation, MathSettings
from agilent34401a.meter import AcFilter, Autozero, Function, InputImpedance, Setup
from agilent34401a.preset import Preset
from agilent34401a.preset_apply import ApplyReport, PresetApplier
from agilent34401a.sim import Simulator
from agilent34401a.trigger import TriggerSettings, TriggerSource
from agilent34401a.worker import (
    Connected,
    ConnectionLost,
    ErrorsReported,
    Event,
    ReadingTaken,
    SetupChanged,
    SetupFailed,
    Worker,
)

TIMEOUT_S = 5.0

FULL_SETUP = (
    Setup.default(Function.DC_VOLTAGE)
    .with_range(10.0)
    .with_nplc(1)
    .with_autozero(Autozero.OFF)
    .with_input_impedance(InputImpedance.HIGH_IMPEDANCE)
    .with_trigger(TriggerSettings(TriggerSource.BUS, delay=0.25, sample_count=3, trigger_count=2))
    .with_math(MathSettings(MathOperation.NULL, null_offset=0.5))
)
FULL_PRESET = Preset("Everything", FULL_SETUP)
AC_PRESET = Preset("Mains", Setup.default(Function.AC_VOLTAGE).with_ac_filter(AcFilter.FAST))


class RefusingSimulator(Simulator):
    """A Simulator that records what it is sent, and refuses the commands that start with any of `refuse`."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.refuse: tuple[str, ...] = ()
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)
        if self.refuse and not command.endswith("?") and command.startswith(self.refuse):
            self._errors.append('-221,"Settings conflict"')
            return
        super().write(command)


class Session:
    """A Worker on a Simulator with a PresetApplier, and a way to run a Preset through them to the end."""

    def __init__(self, simulator: Simulator) -> None:
        self.events: queue.Queue[Event] = queue.Queue()
        self.worker = Worker(lambda: simulator, self.events)
        self.reports: list[ApplyReport] = []
        self.submitted = 0
        self.applier = PresetApplier(self._submit, self.reports.append)
        self.worker.start()
        assert isinstance(self.events.get(timeout=TIMEOUT_S), Connected)

    def _submit(self, setup: Setup) -> bool:
        self.submitted += 1
        self.worker.apply_setup(setup)
        return True

    def run(self, preset: Preset) -> list[ApplyReport]:
        """Apply `preset` and handle every event up to the first Reading that follows (a marker behind its events)."""
        self.reports.clear()
        assert self.applier.apply(preset)
        self.worker.single()
        self.pump_until_reading()
        return list(self.reports)

    def pump_until_reading(self) -> None:
        while True:
            event = self.events.get(timeout=TIMEOUT_S)
            self.applier.handle(event)
            if isinstance(event, ReadingTaken):
                return

    def close(self) -> None:
        self.worker.shutdown()


@pytest.fixture
def session():
    sessions: list[Session] = []

    def start(simulator: Simulator | None = None) -> Session:
        sessions.append(Session(simulator if simulator is not None else Simulator()))
        return sessions[-1]

    yield start
    for each in sessions:
        each.close()


def is_busy(applier: PresetApplier) -> bool:
    return applier.busy


def position(writes: list[str], prefix: str) -> int:
    return next(index for index, command in enumerate(writes) if command.startswith(prefix))


# --- applying in order ---------------------------------------------------------------------------------------------


def test_a_preset_is_sent_function_then_range_and_resolution_then_sense_options_then_trigger_then_math(session):
    simulator = RefusingSimulator()
    running = session(simulator)
    simulator.writes.clear()

    running.run(FULL_PRESET)

    writes = simulator.writes
    order = [
        position(writes, 'FUNC "VOLT:DC"'),
        position(writes, "VOLT:DC:RANG 10"),
        position(writes, "VOLT:DC:NPLC 1"),
        position(writes, "ZERO:AUTO OFF"),
        position(writes, "INP:IMP:AUTO ON"),
        position(writes, "TRIG:SOUR BUS"),
        position(writes, "CALC:NULL:OFFS 0.5"),
        position(writes, "CALC:FUNC NULL"),
    ]
    assert order == sorted(order)
    assert len(set(order)) == len(order)


def test_the_ac_filter_of_a_preset_is_sent_after_its_range_and_before_the_trigger(session):
    simulator = RefusingSimulator()
    running = session(simulator)
    simulator.writes.clear()

    running.run(Preset("Mains", AC_PRESET.setup.with_trigger(TriggerSettings(sample_count=2))))

    writes = simulator.writes
    assert position(writes, 'FUNC "VOLT:AC"') < position(writes, "VOLT:AC:RANG") < position(writes, "DET:BAND 200")
    assert position(writes, "DET:BAND 200") < position(writes, "SAMP:COUN 2")


# --- a Preset the Meter took completely ----------------------------------------------------------------------------


def test_a_preset_the_meter_took_is_reported_as_applied_and_the_meter_has_the_setup(session):
    running = session()

    reports = running.run(FULL_PRESET)

    assert len(reports) == 1
    report = reports[0]
    assert report.applied
    assert report.preset == "Everything"
    assert report.rejections == ()
    assert report.errors == ()
    assert report.failure is None
    assert report.summary == "Preset 'Everything' was applied."
    assert report.actual == FULL_SETUP


def test_autozero_once_in_a_preset_is_applied_and_is_not_reported_as_rejected(session):
    running = session()
    preset = Preset("Once", Setup.default(Function.DC_VOLTAGE).with_autozero(Autozero.ONCE))

    reports = running.run(preset)

    assert reports[0].applied
    assert reports[0].actual is not None
    assert reports[0].actual.autozero is Autozero.OFF


# --- a Preset the Meter did not take completely --------------------------------------------------------------------


def test_a_setting_the_meter_refused_is_listed_with_the_error_it_queued(session):
    simulator = RefusingSimulator()
    running = session(simulator)
    simulator.refuse = ("DET:BAND",)

    reports = running.run(AC_PRESET)

    final = reports[-1]
    assert not final.applied
    assert [str(rejection) for rejection in final.rejections] == [
        "AC Filter: asked for 200 Hz, the Meter has 20 Hz",
    ]
    assert [(error.code, error.message) for error in final.errors] == [(-221, "Settings conflict")]
    assert final.summary == "Preset 'Mains' was only partly applied: the Meter did not take 1 setting."
    assert final.details == (
        "AC Filter: asked for 200 Hz, the Meter has 20 Hz",
        "Meter error -221: Settings conflict",
    )


def test_several_refused_settings_are_all_listed(session):
    simulator = RefusingSimulator()
    running = session(simulator)
    simulator.refuse = ("VOLT:DC:RANG", "TRIG:SOUR", "CALC:NULL:OFFS")

    final = running.run(FULL_PRESET)[-1]

    assert [rejection.setting for rejection in final.rejections] == ["Range", "Trigger Source", "Null offset"]
    assert final.summary == "Preset 'Everything' was only partly applied: the Meter did not take 3 settings."


def test_a_function_the_meter_would_not_switch_to_is_reported(session):
    simulator = RefusingSimulator()
    running = session(simulator)
    simulator.refuse = ('FUNC "VOLT:AC"',)

    final = running.run(AC_PRESET)[-1]

    assert [str(rejection) for rejection in final.rejections] == ["Function: asked for AC V, the Meter is in DC V"]
    assert not final.applied


def test_a_meter_error_with_nothing_different_is_still_reported(session):
    simulator = RefusingSimulator()
    running = session(simulator)
    simulator.refuse = ("ZERO:AUTO",)  # Autozero is already On, which is what the Preset asks for
    preset = Preset("Default", Setup.default(Function.DC_VOLTAGE))

    final = running.run(preset)[-1]

    assert final.rejections == ()
    assert [error.code for error in final.errors] == [-221]
    assert not final.applied
    assert final.summary == "Preset 'Default' was applied, but the Meter queued 1 error."


def test_the_report_is_made_when_the_setup_is_known_and_made_again_when_the_errors_follow(session):
    simulator = RefusingSimulator()
    running = session(simulator)
    simulator.refuse = ("DET:BAND",)

    reports = running.run(AC_PRESET)

    assert [report.errors == () for report in reports] == [True, False]
    assert reports[0].rejections == reports[1].rejections


def test_errors_that_arrive_long_after_a_preset_are_not_added_to_its_report(session):
    running = session()
    reports = running.run(AC_PRESET)
    assert len(reports) == 1

    running.applier.handle(ErrorsReported((QueuedError(-113, "Undefined header"),)))

    assert len(running.reports) == 1


# --- a Preset that could not be confirmed --------------------------------------------------------------------------


def test_a_setup_change_that_failed_is_reported_as_a_failure_of_the_preset():
    reports: list[ApplyReport] = []
    applier = PresetApplier(lambda _setup: True, reports.append)
    applier.apply(AC_PRESET)

    applier.handle(SetupFailed("Timed out", requested=AC_PRESET.setup))

    assert len(reports) == 1
    assert not reports[0].applied
    assert reports[0].failure == "Timed out"
    assert reports[0].summary == "Preset 'Mains' could not be confirmed: Timed out"


def test_a_connection_lost_while_a_preset_is_applied_is_reported_as_a_failure_of_the_preset():
    reports: list[ApplyReport] = []
    applier = PresetApplier(lambda _setup: True, reports.append)
    applier.apply(AC_PRESET)

    applier.handle(ConnectionLost("cable pulled"))

    assert reports[0].failure is not None
    assert "cable pulled" in reports[0].failure


def test_a_preset_cannot_be_applied_when_the_window_will_not_send_it():
    reports: list[ApplyReport] = []
    applier = PresetApplier(lambda _setup: False, reports.append)

    assert not applier.apply(AC_PRESET)
    assert not applier.busy
    applier.handle(SetupChanged(AC_PRESET.setup))
    assert reports == []


def test_a_second_preset_waits_until_the_first_has_been_answered():
    reports: list[ApplyReport] = []
    applier = PresetApplier(lambda _setup: True, reports.append)
    assert applier.apply(AC_PRESET)

    assert is_busy(applier)
    assert not applier.apply(FULL_PRESET)
    applier.handle(SetupChanged(AC_PRESET.setup, requested=AC_PRESET.setup))
    assert not is_busy(applier)
    assert applier.apply(FULL_PRESET)


def test_a_setup_change_that_was_not_the_answer_to_the_preset_is_not_taken_for_it(session):
    running = session()
    running.worker.send_raw("VOLT:DC:NPLC 10")  # its SetupChanged reaches the window before the Preset's does
    running.reports.clear()
    assert running.applier.apply(AC_PRESET)
    running.worker.single()

    running.pump_until_reading()

    assert len(running.reports) == 1
    assert running.reports[0].applied
    assert running.reports[0].actual == AC_PRESET.setup


def test_a_setup_failure_that_was_not_the_presets_is_not_taken_for_its_failure():
    reports: list[ApplyReport] = []
    applier = PresetApplier(lambda _setup: True, reports.append)
    applier.apply(AC_PRESET)

    applier.handle(SetupFailed("Timed out", requested=FULL_SETUP))  # some other request's failure

    assert reports == []
    assert applier.busy


def test_a_setup_change_nobody_asked_for_by_a_preset_is_not_a_report():
    reports: list[ApplyReport] = []
    applier = PresetApplier(lambda _setup: True, reports.append)

    applier.handle(SetupChanged(AC_PRESET.setup))

    assert reports == []
