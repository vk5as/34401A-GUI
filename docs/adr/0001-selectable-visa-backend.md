# Selectable VISA Backend

The goal was to depend only on pyvisa-py, but the target adapter is a Keysight 82357B USB-GPIB, which pyvisa-py cannot drive on Windows. On Windows it is only reachable through the Keysight IO Libraries Suite; on Linux, pyvisa-py can use it through linux-gpib. So the Backend is selectable per Connection: Auto (vendor VISA if installed, otherwise pyvisa-py), vendor VISA (`@ivi`) or pyvisa-py (`@py`). pyvisa-py and pyserial remain required dependencies so RS-232 works with no vendor software; Keysight IO Libraries is a documented separate install, not a pip dependency.

## Considered Options

- **pyvisa-py only:** rejected because GPIB on Windows with the 82357B would need `gpib-ctypes` plus Keysight's NI-488 compatibility layer, which is fragile.
- **Vendor VISA only:** rejected because it would force a large proprietary install just to use RS-232, and it doesn't fit the Linux development setup.
