# Agilent 34401A Remote Control

A desktop application and CLI for operating an Agilent/HP 34401A 6½-digit digital multimeter remotely over GPIB or RS-232, with a simulated meter for development and testing.

## Language

### The instrument

**Meter**:
The physical Agilent/HP 34401A being controlled. Firmware identifies as either `HEWLETT-PACKARD` or `Agilent Technologies`; both are the same Meter.
_Avoid_: DMM, instrument, device, multimeter (in code and UI)

**Function**:
The quantity the Meter measures: DC voltage, AC voltage, DC current, AC current, 2-wire resistance, 4-wire resistance, frequency, period, continuity, diode, or DC voltage ratio.
_Avoid_: Mode, measurement type

**Range**:
The full-scale span the Meter measures within for the current Function, either fixed or chosen automatically (**Autorange**).
_Avoid_: Scale

**Resolution**:
The number of displayed digits (4½, 5½ or 6½); tied to Integration Time.
_Avoid_: Precision, digits setting

**Integration Time**:
How long the Meter integrates each Reading, expressed in power-line cycles (NPLC: 0.02 to 100).
_Avoid_: Aperture (for DC functions), speed

**Gate Time**:
The counting window for frequency and period Functions (10 ms, 100 ms, 1 s).
_Avoid_: Aperture

**AC Filter**:
The lowest signal frequency the Meter's AC detector is tuned for (3 Hz slow, 20 Hz medium, 200 Hz fast).
_Avoid_: Bandwidth, detector band

**Autozero**:
Whether the Meter takes an offset measurement alongside each Reading (on, off, or once).

**Input Impedance**:
Whether DC voltage inputs on the lower Ranges present 10 MΩ or more than 10 GΩ to the circuit.

**Terminals**:
Which set of input jacks is active (front or rear), selected by the physical switch on the Meter and only readable remotely.

**Setup**:
The complete set of Meter settings (Function, Range, Resolution, Integration Time, trigger and Math settings) that determines what a Reading means.
_Avoid_: Config, state, configuration

**Preset**:
A named Setup saved by the application, which can be re-applied to the Meter in one step. The Meter has no setup memory of its own, so Presets exist only in the application.
_Avoid_: Profile, saved state

### Readings

**Reading**:
One measured value returned by the Meter, with its Function and unit.
_Avoid_: Sample, measurement, value

**Overload**:
A Reading that exceeded the Range, reported by the Meter as ±9.9E+37 and shown as OVLD.
_Avoid_: Overflow, out of range

**Raw Reading**:
The exact text the Meter returned for a Reading, before any formatting.

**Reading Memory**:
The Meter's internal store of up to 512 Readings, filled during a Burst.
_Avoid_: Buffer (reserved for the History)

**History**:
The application's rolling record of recent Readings that feeds the chart and statistics.
_Avoid_: Buffer, log

**Break Marker**:
A point in the History where the Function or unit changed, so Readings on either side are not comparable.

### Taking readings

**Continuous**:
The application repeatedly asks the Meter for a Reading and shows each one as it arrives.
_Avoid_: Free-run, polling mode, live mode

**Single**:
The application triggers exactly one Reading.

**Burst**:
The Meter takes a configured number of Readings into Reading Memory on its own, and the application collects them all once it finishes.
_Avoid_: Batch, block capture

**Trigger Source**:
What starts a Reading on the Meter: immediately, a bus command, or the external trigger input.

**Trigger Delay**:
The wait between a trigger and the start of the Reading, either automatic or fixed.

**Sample Count**:
How many Readings the Meter takes per trigger.

**Trigger Count**:
How many triggers the Meter accepts before returning to idle.

### Math

**Math Operation**:
A calculation the Meter applies to Readings: Null, dB, dBm, Statistics, or Limit Test. Only one is active at a time.

**Null**:
Subtracting a stored offset from each Reading.
_Avoid_: Relative, zero

**dBm Reference Resistance**:
The load impedance (50 Ω to 8 kΩ) assumed when expressing a Reading in dBm.

**Statistics**:
The Meter's running minimum, maximum, average and count of Readings.
_Avoid_: Min/Max (as a name for the whole feature)

**Limit Test**:
Comparing each Reading against an upper and lower bound and flagging it HI or LO.

### Connection

**Connection**:
An open session between the application and one Meter, over either GPIB or RS-232.
_Avoid_: Link, session (in UI)

**Backend**:
The VISA implementation carrying a Connection: the vendor VISA library (Keysight/NI) or pyvisa-py.
_Avoid_: Driver, library

**Framing**:
The RS-232 character format: data bits, parity and stop bits together.

**Flow Control**:
The RS-232 handshaking method: none, XON/XOFF, RTS/CTS or DTR/DSR.
_Avoid_: Handshake

**Probe**:
Automatically finding the RS-232 baud rate and Framing (and optionally Flow Control) at which the Meter answers.
_Avoid_: Scan (reserved for listing available resources), autodetect

**Remote**:
The Meter is under the application's control and its front panel shows REM.

**Local**:
The Meter's front panel is in control again.

**Lockout**:
Remote, with the front panel's Local key disabled as well.

### Simulation

**Simulator**:
A software model of the Meter that behaves like the real one, including timing, Ranges, Reading Memory and its error queue.
_Avoid_: Mock, fake, emulator

**Applied Signal**:
The input the Simulator pretends is connected to its terminals for each Function: a value plus noise, drift or a sine component.
