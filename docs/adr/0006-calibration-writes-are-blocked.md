# Calibration Writes Are Blocked

The application never exposes the 34401A's calibration-changing commands (`CAL:SEC`, `CAL:VAL`, `CAL`, `CAL:STR` writes). The System tab shows the calibration count and message read-only. The SCPI console refuses `CAL…` write commands unless the user ticks an explicit override. A stray calibration command can invalidate a traceable calibration, which cannot be undone in software, so the console is deliberately not a transparent pass-through.
