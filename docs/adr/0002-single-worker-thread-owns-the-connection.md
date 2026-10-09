# A Single Worker Thread Owns the Connection

All traffic to the Meter goes through one background thread. The GUI and CLI send it typed requests through a queue, and results come back as events on a second queue, which Tk reads with `after()`. Readings can take seconds, for example 6½ digits at 100 NPLC, or a 9600-baud RS-232 link. A Burst waiting on an external trigger can block indefinitely. So no other thread may ever touch the VISA session. This mirrors the cpx400dp-gui design.

## Consequences

- **Timeouts:** each operation's timeout is calculated from the current Setup (Integration Time, Autozero, Sample Count and Trigger Count), not fixed.
- **Cancellation:** long operations (Burst, external-trigger waits, Probe) can be cancelled with a device clear over GPIB, or a break over RS-232.
- **Bad replies:** a malformed reply is logged and the Connection resynchronised. The worker must never die silently (see cpx400dp-gui issue #1).
  Resynchronising is a clear followed by a sentinel query (`*IDN?`, whose answer was learned on connect): replies are read and discarded until that answer arrives, tolerating a few timeouts for a late one, because a device clear cannot be relied on to drop a reply that is still on its way (a TCP socket has none at all). A Meter that does not answer the sentinel, or several bad replies in a row, is reported as `ConnectionLost`; pyvisa-py cannot tell a dropped TCP connection from a silent Meter, so this is also how a dropped Connection is found. After `ConnectionLost` or `WorkerFailed` the Worker stays alive and serves the next `connect`.
- **Probe:** a `ProbeJob` opens its own temporary Transports on its own thread, which looks like a second owner of the Meter but is not, because it only runs while no Connection is open: the window refuses to start a Probe while its Worker holds a Connection (`MainWindow._can_probe`, which the connection dialog asks), and `agilent34401a-cli probe` is a separate process with no Connection of its own. A serial port can be opened once anyway, so a Probe during a Connection would fail rather than interleave with it.
