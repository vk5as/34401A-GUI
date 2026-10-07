# The Simulator Ships Inside the Package

Unlike cpx400dp-gui, whose simulator lives in a separate `tools/` script, the Simulator is part of the installed package (`agilent34401a.sim`). A user who installed with pip can run the GUI or CLI with `--simulate`, and screenshots, demos and GUI tests need no hardware.

There is one Meter model, available in two forms:
- **In-process transport:** fast, deterministic unit and worker tests, and the `--simulate` flag.
- **TCP socket server:** reached through pyvisa-py's `TCPIP::host::port::SOCKET` resource, so integration tests exercise the real pyvisa stack. It also supports fault injection: slow replies, dropped connections and garbage bytes.

## Considered Options

- **pyvisa-sim:** rejected because static YAML responses can't model Autorange, Integration Time timing, Reading Memory limits or the error queue, and it adds another dependency.
