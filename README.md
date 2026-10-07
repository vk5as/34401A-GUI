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

To exercise the real pyvisa stack without a Meter, run the Simulator as a TCP server and point the CLI at it:

```bash
agilent34401a-sim --port 5025 &
agilent34401a-cli read --backend py --resource "TCPIP::127.0.0.1::5025::SOCKET"
```

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
