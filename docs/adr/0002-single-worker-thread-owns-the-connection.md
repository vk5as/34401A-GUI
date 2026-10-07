# A Single Worker Thread Owns the Connection

All traffic to the Meter goes through one background thread. The GUI and CLI send it typed requests through a queue, and results come back as events on a second queue, which Tk reads with `after()`. Readings can take seconds, for example 6½ digits at 100 NPLC, or a 9600-baud RS-232 link. A Burst waiting on an external trigger can block indefinitely. So no other thread may ever touch the VISA session. This mirrors the cpx400dp-gui design.

## Consequences

- **Timeouts:** each operation's timeout is calculated from the current Setup (Integration Time, Autozero, Sample Count and Trigger Count), not fixed.
- **Cancellation:** long operations (Burst, external-trigger waits, Probe) can be cancelled with a device clear over GPIB, or a break over RS-232.
- **Bad replies:** a malformed reply is logged and the Connection resynchronised. The worker must never die silently (see cpx400dp-gui issue #1).
