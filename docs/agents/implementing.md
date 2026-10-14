# Implementing a ticket

Read this before changing code. Domain terms are in `GLOSSARY.md`; the reasons behind the architecture are in `docs/adr/`. Use the glossary's words in code, UI text, tests and commit messages.

## Checks

Run these before every commit. CI runs the same ones on every push, on Linux 3.10 to 3.14 and Windows:

```bash
.venv/bin/black src tests scripts && .venv/bin/ruff check src tests scripts && .venv/bin/mypy \
  && .venv/bin/bandit -c pyproject.toml -r src -q
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pre-commit run --all-files     # adds vermin (Python 3.10) and the repo's own rules
xvfb-run -a .venv/bin/pytest -q --no-cov                              # about 4 minutes; GUI tests need a display
xvfb-run -a .venv/bin/pytest -q && .venv/bin/coverage report --omit="*/agilent34401a/gui/*" --fail-under=85
```

In a git worktree use the main checkout's venv (`/home/dev/projects/34401A-GUI/.venv/bin/...`) from inside the worktree: pytest imports the worktree's own `src/`. Bandit ignores `noqa`, so `src/` uses no `assert`. Each test has a 180 s timeout; a test that hits it is a real hang.

## Threads and Tk

- The Worker thread is the only code that touches a Transport (ADR-0002). The GUI and CLI send it requests and drain its typed events.
- Tk runs on the main thread only, and every Tk object is finalised there (ADR-0008). A tab or dialog keeps no window callbacks that outlive it, and a helper thread never holds a reference to a widget.
- Thread tests synchronise on something observable: an Event, a hooked Simulator, or a marker request whose answer arrives after everything before it. A fixed sleep proves nothing.
- Run every new GUI or thread test several times before committing.

## Portability

CI runs the tests on Windows, where behaviour differs:

- pyvisa-py connects a TCP socket at open on Windows and at first use on Linux; a test accepts the failure from either.
- Assertions hold on any OS, so they never check OS error text or POSIX paths.
- GUI tests assert what the code asked for (`focus_lastfor`, `winfo_manager`), use widgets the window really shows, and get their window from the `make_window` and `tk_root` fixtures. A pre-commit hook enforces the mechanical part.
- Unclosed sockets and files fail the suite (`filterwarnings = error`), so close them in `finally`.

## Test seams

The primary seam is the Transport: test everything above it against `Simulator()`, which is instant in tests. Copy the style of the existing helpers:

| Need | Helper |
|---|---|
| A Worker with a hookable Simulator | `HookedSimulator`, `started`, `next_event` in `tests/test_worker.py` |
| A window driven like a user | `make_window`, `tk_root` (`tests/conftest.py`); `pump`, `pump_for`, `choose`, `settle`, `CountingSimulator` (`tests/test_gui_app.py`) |
| Exactly what the Driver sends | `RecordingTransport`, `AnswersInOrder` in `tests/test_driver.py` |
| The real pyvisa stack | `SimulatorServer` in `tests/test_integration.py` |
| A refused TCP port | the `unused_port` fixture |

The suite points the platform config folder at a temporary one for every test, so tests never touch the user's settings or Presets.

## Extension points

Add features through these, in a new module where possible, with small additive edits to shared files:

- **GUI tab:** build it in its own module under `gui/`, as a child of `window.notebook`; register it with one line in `MainWindow._build_tabs()` via `add_tab`.
- **GUI hooks:** `add_menu_command` (File, View, Help), `register_shortcut`, `on_theme_changed`, `add_event_handler`, `add_reading_listener`.
- **CLI subcommand:** `_add_<name>_command` with `add_command` and `add_connection_options`, registered with one line in `main()`. Its handler calls `run_on_meter`, which connects, identifies, closes and turns a `MeterError` into exit code 1.
- **Worker request:** a handler that loses a reply recovers with `self._recover(transport, error)`, which resynchronises and counts failures, and resets the failure count on success.
- **Settings:** `Settings` and `config_dir()`; add a field only for a genuinely new setting.

## Commits

Imperative subject; the body says why. A commit that finishes an issue ends with `Closes #N`.
