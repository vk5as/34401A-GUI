# Coding standards

The reviewer applies these. They are judgement calls; the mechanical rules live in Ruff, mypy, Bandit, vermin and the pre-commit hooks, and need no review.

## Vocabulary

Code, UI text, tests and docs use `GLOSSARY.md`'s terms. Check every new name and user-facing string against each entry's _Avoid_ list, reading the context: "Sample Count" is a glossary term while "sample" for a Reading is not, and "device clear" is SCPI, not a name for the Meter. A concept the glossary lacks is a gap to raise, not a word to invent.

## One source of truth

- Per-Function and per-Math-Operation knowledge (SCPI names, setting prefixes, units, ranges) lives once, in the Meter model, and the Driver, Simulator and CLI read it from there. A new table keyed by `Function` that repeats one elsewhere is a finding.
- A default (History length, error-check interval, timeouts) is defined once and imported.
- Test doubles live in one shared place; a new `RecordingSimulator`-style class that repeats an existing one is a finding.

## Worker and Driver

- A Worker handler that loses a reply recovers through `_recover` (resynchronise and count the failure), not a bare `clear()`.
- Anything that changes the Meter's Setup tells the UI: the Worker reads the Setup back and emits `SetupChanged`. Nothing changes the Meter implicitly on connect or disconnect (ADR-0004).
- Every Meter error reaches the error log and the status bar, whatever path produced it.

## GUI

- A tab or dialog reacts to the Connection events the same way the others do; copy-pasted event plumbing that drifts between tabs is a finding.
- A control the current Function or Connection cannot use is disabled, with a reason the user can see. The UI never offers something that always fails.
- New Tk objects follow ADR-0008: nothing that holds them is kept alive by a helper thread or a cycle that outlives the widget.

## Size

A module that changes for several unrelated reasons is a finding. `worker.py`, `driver.py`, `sim.py` and `gui/main_window.py` are already large: new features go in their own modules with a small hook in the shared file.
