# Agilent 34401A Remote Control

A Tk desktop application and command-line tool for remotely operating an Agilent/HP 34401A
digital multimeter over GPIB or RS-232, with a built-in simulator for development and testing.

> This project is under active development and is not yet usable. See the
> [spec](https://github.com/vk5as/34401A-GUI/issues/1) for the plan.

## Connecting

`agilent34401a-cli read` takes one Reading. By default it talks to GPIB board 0, address 22, through the
first VISA Backend that loads (Keysight/NI VISA if installed, otherwise pyvisa-py):

```bash
agilent34401a-cli read                                    # GPIB0::22::INSTR, Backend auto
agilent34401a-cli read --gpib-address 5 --backend py      # pyvisa-py only (needs linux-gpib or gpib-ctypes for GPIB)
agilent34401a-cli read --resource "TCPIP::192.0.2.1::5025::SOCKET"   # any raw VISA resource
agilent34401a-cli read --simulate                         # the built-in Simulator, no Meter needed
```

The same Connection options work for the administration commands. Each exits with 0 on success and 1 on failure:

```bash
agilent34401a-cli idn --simulate        # print the Meter's identity and firmware revision
agilent34401a-cli errors --simulate     # print and clear the Meter's error queue
agilent34401a-cli selftest --simulate   # run the self-test (about 10 s on a real Meter); 1 if it fails
agilent34401a-cli reset --simulate      # *RST: the only command that resets the Meter
```

`raw` sends one raw SCPI command or query and prints the reply, like the SCPI console tab (Ctrl+K). Commands that
would change the Meter's calibration (`CAL:SEC`, `CAL:VAL`, `CAL`, `CAL:STR` writes) are refused with exit code 2
unless you pass `--allow-calibration`; read-only queries such as `CAL:COUN?` need no flag:

```bash
agilent34401a-cli raw --simulate "*IDN?"
agilent34401a-cli raw --simulate "VOLT:DC:NPLC 10"        # a command: no reply, exit 1 if the Meter complains
```

`log` takes Readings with the Meter's current Setup and writes them as CSV to standard output, or to a file with
`-o`. Give `-n` (a number of Readings), `--duration` (seconds) or both; it stops at whichever comes first, and
keeps the rows already written if it is interrupted or fails:

```bash
agilent34401a-cli log --simulate -n 100 -o capture.csv
agilent34401a-cli log --simulate --duration 60 > capture.csv
```

The window does the same: the Record button (or File → Record to CSV…, or Ctrl+L) streams every Reading to a file
and the status bar shows `REC` and the file until you stop; File → Export History as CSV… saves the Readings
the Chart is showing. The columns are `timestamp_iso, elapsed_s, function, range, value, unit, raw, math_mode,
limit_result`. `value` is in the base unit (volts, ohms, ...), `OVLD` for an Overload, and `raw` is the Meter's own
text. The file is UTF-8 with LF line endings; Excel needs "From Text/CSV" with UTF-8 to show the Ω unit.

To exercise the real pyvisa stack without a Meter, run the Simulator as a TCP server and point the CLI at it:

```bash
agilent34401a-sim --port 5025 &
agilent34401a-cli read --backend py --resource "TCPIP::127.0.0.1::5025::SOCKET"
```

### RS-232

Choose "Meter (RS-232)" in the connection dialog (File → Connect…), or give the CLI a `--serial-port`. Pyvisa-py
and pyserial are installed with the package, so no vendor software is needed. Every parameter is configurable;
the defaults are the Meter's factory settings:

```bash
agilent34401a-cli read --serial-port COM3                 # 9600 baud, 8 data bits, no parity, 1 stop bit, no Flow Control
agilent34401a-cli read --serial-port /dev/ttyUSB0 --baud 4800 --data-bits 7 --parity even --flow-control dtrdsr
```

The options are `--baud` (300 to 9600), `--data-bits` (7 or 8), `--parity` (none, even, odd), `--stop-bits` (1 or 2),
`--flow-control` (none, xonxoff, rtscts, dtrdsr), `--terminator` (lf, cr, crlf), and `--dtr`/`--rts` (on or off)
to hold those lines for an unusual cable. The Meter is put in Remote with `SYST:REM` when the Connection opens and
returned to Local with `SYST:LOC` when it closes. Lockout over RS-232 is `SYST:RWL`.

If you do not know the Meter's settings, use Probe: the Probe button in the dialog (with a progress bar and
Cancel), or `agilent34401a-cli probe --serial-port COM3`. It tries every baud rate, 9600 first, with each of the
Meter's three Framings (8N1, 7E1, 7O1) until the Meter answers `*IDN?`; tick "Include Flow Control" (or pass
`--include-flow-control`) to try every Flow Control as well, which takes four times as long. Probe needs the port to
itself, so disconnect first. When nothing answers it says what to check:

- the cable must be a null-modem (crossed) cable, not a straight-through one;
- the Meter's I/O menu must be set to RS-232 rather than GPIB, and its baud rate and parity set under that menu;
- the Flow Control must match the Meter's handshake, which is why Probe can include it.

Linux caveat: pyserial does not implement DTR/DSR Flow Control in hardware on Linux; it only asserts DTR, and
never waits for DSR. Windows implements it. On Linux, DTR/DSR therefore behaves like no Flow Control with DTR held
asserted, so use a slower baud rate if characters are lost.

## Single and Burst

**Single** (the button next to Run, or the Space key) takes exactly one Reading and pauses Continuous. The
**Trigger** tab sets up a **Burst**: the Trigger Source (immediate, bus or external), the Trigger Delay (automatic, or
fixed from 0 to 3600 s), the Sample Count and the Trigger Count. Start Burst has the Meter take that many Readings into
its Reading Memory on its own and then collects them; they appear in the readout, the chart and the History, and
File → Export Burst as CSV… saves them (the Meter keeps no time stamps, so each Reading's time is worked out from the
Setup).

- Reading Memory holds 512 Readings, so a Burst of Sample Count x Trigger Count above 512 (or with an infinite
  Trigger Count) is refused before anything is sent.
- Bus triggers are sent by the application. An external trigger may never arrive, so the application polls the
  Meter's status byte instead of waiting, and Cancel sends a device clear.
- A Connection that cannot send a device clear (a raw TCP socket, such as the `agilent34401a-sim` server) cannot
  cancel a wait for an external trigger, so the External source is disabled there, with a tooltip saying why.
- The Meter's trigger settings are put back when the Burst ends, so Run and Single keep working.

## Development

`tkinter` is a system package, not a PyPI one. On Debian/Ubuntu: `sudo apt install python3-tk xvfb`.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pre-commit install --install-hooks
```

### Code quality

```bash
.venv/bin/black --check src tests scripts    # formatting
.venv/bin/ruff check src tests scripts       # linting (all rules enabled)
.venv/bin/mypy                               # strict type checking
.venv/bin/bandit -c pyproject.toml -r src    # security scan
.venv/bin/pip-audit .                        # runtime dependency vulnerabilities
.venv/bin/pytest                             # tests (use xvfb-run -a on a headless box)
.venv/bin/python scripts/ci_smoke_test.py    # builds the GUI against the Simulator and shuts it down (xvfb-run -a if headless)
.venv/bin/coverage report --omit="*/agilent34401a/gui/*" --fail-under=85   # coverage gate (GUI excluded)
```

The coverage gate covers everything except the `gui` package; GUI coverage is still shown in the
`pytest` report but isn't gated.

## Releasing

Releases are cut by hand from the **Release** workflow (Actions, Release, Run workflow); nothing is released by a
push or a tag. To release:

1. Bump `version` in `pyproject.toml` (the only place the version lives) and merge it to `main`.
2. Run the Release workflow on `main`. It refuses to start if the tag `v<version>` already exists.
3. It runs the static analysis and tests (Linux and Windows), builds the wheel and sdist, runs `twine check` and
   `check-wheel-contents`, then installs the built wheel into a clean virtual environment on Windows with Python
   3.11, 3.12, 3.13 and 3.14. There it constructs the GUI against the Simulator and runs `--help` for
   `agilent34401a-cli` and `agilent34401a-sim`.
4. Only if every one of those passed does it create the release `v<version>`, with the wheel and sdist attached and
   generated release notes. Run from any other branch, it executes the gates as a dry run and creates nothing.

The tag name comes from `scripts/release_info.py` (`tag`, `version` and `check-dist` subcommands), so it can't
drift from `pyproject.toml`.

### PyPI (prepared, not enabled)

`release.yml` contains a `publish-pypi` job using PyPI Trusted Publishing (OpenID Connect, no API token). It is
switched off with `if: false`, and it is the only job with `id-token: write`. To enable it, register
`vk5as/34401A-GUI`, workflow `release.yml`, environment `pypi` as a trusted publisher for `agilent34401a` on
pypi.org, create a `pypi` environment in the repository settings, and change `if: false`; the comments above the
job list the steps.

### Freezing

The package is kept compatible with freezers such as PyInstaller: it never builds paths from `__file__`, and package
data (`py.typed`) is reached through `importlib.resources`. `tests/test_freeze_compatibility.py` guards this.
